import json

import pytest

from loom.service.contracts import ServiceError, canonical
from loom.service.semantic import SessionSemantic
from loom.service.startup_indexes import prepare
from loom.service.store import SessionStore
from loom.service.trajectory import SessionTrajectory


def test_semantic_restart_only_reads_unfinished_jobs_and_preserves_checkpoints(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    trajectory = SessionTrajectory(store)
    initial = SessionSemantic(trajectory)
    initial.close()
    archived = canonical(dict(id='archived', session_id='s', state='completed', checkpoint={'large': 'ARCHIVED_BODY' * 10000},
                              result={'sha256': 'already-published'}))
    with store.transaction() as db:
        db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', ('archived', 's', 'archived', archived))
        for state in ('queued', 'running'):
            body = dict(id=state, session_id='s', state=state, checkpoint={'accepted': [1, 2]}, usage={'tokens': 77})
            db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', (state, 's', state, canonical(body)))
    loads = json.loads

    def no_archived(value, *args, **kwargs):
        assert 'ARCHIVED_BODY' not in value, 'Startup decoded a finished evaluation'
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', no_archived)
    restored = SessionSemantic(trajectory)
    try:
        with store.read() as db:
            assert db.execute("SELECT body FROM semantic_jobs WHERE id='archived'").fetchone()[0] == archived
            for identifier in ('queued', 'running'):
                body = loads(db.execute('SELECT body FROM semantic_jobs WHERE id=?', (identifier,)).fetchone()[0])
                assert body['state'] == 'interrupted'
                assert body['checkpoint'] == {'accepted': [1, 2]}
                assert body['usage'] == {'tokens': 77}
            assert not db.execute('SELECT 1 FROM evaluation_artifacts').fetchone()  # No startup ownership backfill.
            plan = db.execute("EXPLAIN QUERY PLAN SELECT id,body FROM semantic_jobs "
                              "WHERE json_extract(body,'$.state') IN ('queued','running')").fetchall()
            assert any('semantic_jobs_active' in row[3] for row in plan)
    finally:
        restored.close()
        trajectory.close()


def test_existing_history_requires_explicit_index_preparation(tmp_path):
    store = SessionStore(tmp_path)
    trajectory = SessionTrajectory(store)
    with store.transaction() as db:
        db.execute('CREATE TABLE semantic_jobs(id TEXT PRIMARY KEY,session_id TEXT,cache_key TEXT,body TEXT)')
        db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', ('old', 's', 'old', canonical(dict(state='completed'))))
    with pytest.raises(ServiceError, match='startup_indexes'):
        SessionSemantic(trajectory)
    with store.read() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs_active'").fetchone()
    assert prepare(store.path) == ['semantic_jobs_active']
    restored = SessionSemantic(trajectory)
    restored.close()
    trajectory.close()
    assert prepare(store.path) == ['semantic_jobs_active']


def test_recovery_index_tracks_all_publishers_including_replacement(tmp_path):
    store = SessionStore(tmp_path)
    trajectory = SessionTrajectory(store)
    semantic = SessionSemantic(trajectory)
    semantic.close()
    with store.transaction() as db:
        for state in ('completed', 'running', 'failed', 'queued'):
            db.execute('INSERT OR REPLACE INTO semantic_jobs VALUES(?,?,?,?)', ('job', 's', 'key', canonical(dict(state=state))))
            rows = db.execute("SELECT id FROM semantic_jobs INDEXED BY semantic_jobs_active "
                              "WHERE json_extract(body,'$.state') IN ('queued','running')").fetchall()
            assert bool(rows) == (state in {'queued', 'running'})
        db.execute('DELETE FROM semantic_jobs')
        assert not db.execute("SELECT id FROM semantic_jobs INDEXED BY semantic_jobs_active "
                              "WHERE json_extract(body,'$.state') IN ('queued','running')").fetchall()
    trajectory.close()


def test_prepare_does_not_create_missing_database(tmp_path):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(ValueError, match='does not exist'):
        prepare(path)
    assert not path.exists()

