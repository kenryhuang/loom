import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from loom.memory.ingestion import sync_job
from loom.memory.repository import Repository
from loom.memory.store import MemoryStore
from loom.service.store import SessionStore
from tests.memory.helpers import seed


def test_library_uses_v2_without_reading_evaluations_or_sessions(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path / 'project')
    seed(store, tmp_path / 'other', evaluation='private')
    original = Repository.connect

    @contextmanager
    def connect(self, **kwargs):
        with original(self, **kwargs) as db:
            def authorize(action, table, *_):
                if action == sqlite3.SQLITE_READ and table in {'semantic_jobs', 'sessions', 'memory_annotations', 'memory_versions'}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            yield db

    monkeypatch.setattr(Repository, 'connect', connect)
    memory = MemoryStore(store.path, workspace=tmp_path / 'project')
    first = memory.browse()
    first['records'][0]['source']['annotation_revision'] = 99
    assert 'annotation_revision' not in memory.browse()['records'][0]['source']
    assert memory.browse(offset=20)['total'] == 1
    assert not memory.browse(review='reviewed')['records']
    assert memory.search('planets')['records']
    assert memory.read('eval-memory', 'memory:planet')['historical']
    assert MemoryStore(store.path, workspace=tmp_path / 'other').browse()['records'][0]['source']['evaluation_id'] == 'private'
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(r['total'] == 1 for r in pool.map(lambda _: memory.browse(), range(4)))


def test_library_reflects_incremental_publication_and_source_deletion(tmp_path):
    store = SessionStore(tmp_path / 'data')
    sid = seed(store, tmp_path)
    memory = MemoryStore(store.path, workspace=tmp_path)
    assert memory.browse()['total'] == 1
    with store.transaction() as db:
        job = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        job['checkpoint']['memory']['records']['memory:planet']['summary'] = 'New evidence'
        db.execute("UPDATE semantic_jobs SET body=? WHERE id='eval-memory'", (json.dumps(job),))
        sync_job(db, job)
    assert memory.browse(query='evidence')['records'][0]['summary'] == 'New evidence'
    store.update(sid, lambda state, emit, db: state.update(deleted_at='now'))
    assert memory.browse()['total'] == 0
