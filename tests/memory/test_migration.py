import json
import sqlite3

import pytest

from loom.memory import migration
from loom.memory.ingestion import sync_job
from loom.memory.store import MemoryStore
from loom.service.controller import LoomService
from loom.service.store import SessionStore
from tests.memory.helpers import seed


def test_offline_import_resumes_without_rereading_completed_jobs(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path, evaluation='a', publish=False)
    seed(store, tmp_path, evaluation='b', publish=False)
    original = migration.import_job
    calls = []

    def interrupted(db, job, **kwargs):
        calls.append(job['id'])
        if job['id'] == 'b':
            raise RuntimeError('simulated interruption')
        return original(db, job, **kwargs)

    monkeypatch.setattr(migration, 'import_job', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        migration.migrate_database(store.path)
    with pytest.raises(RuntimeError, match='offline migration'):
        LoomService(store.directory)
    assert calls == ['a', 'b']
    calls.clear()

    def resumed(db, job, **kwargs):
        calls.append(job['id'])
        return original(db, job, **kwargs)

    monkeypatch.setattr(migration, 'import_job', resumed)
    report = migration.migrate_database(store.path)
    assert calls == ['b']
    assert report['observations'] == 2
    assert all(n['mode'] == 'runtime_v2' and n['checkpoint'] == 'complete' for n in report['namespaces'])
    with store.read() as db:
        assert {r[0] for r in db.execute('SELECT state FROM memory_jobs')} == {'shadow_pending'}


def test_completed_import_and_service_start_never_read_job_bodies(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path, publish=False)
    backup = tmp_path / 'before.sqlite'
    report = migration.migrate_database(store.path, backup=backup)
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT COUNT(*) FROM memory_observations').fetchone()[0] == 0
    connect = sqlite3.connect
    seen = []

    def guarded(*args, **kwargs):
        db = connect(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'semantic_jobs' and column == 'body':
                seen.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, 'connect', guarded)
    assert migration.migrate_database(store.path, backup=backup) == report
    monkeypatch.setattr(migration, 'migrate_database', lambda *a, **k: pytest.fail('startup invoked migration'))
    service = LoomService(store.directory)
    service.close()
    assert not seen


def test_legacy_data_requires_explicit_import(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path, publish=False)
    with pytest.raises(RuntimeError, match='offline migration'):
        LoomService(store.directory)
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_observations').fetchone()[0] == 0
    migration.migrate_database(store.path)
    service = LoomService(store.directory)
    assert MemoryStore(store.path, workspace=tmp_path).search('planets')['records']
    service.close()


def test_import_refuses_running_service_and_nonexistent_database(tmp_path):
    service = LoomService(tmp_path / 'data')
    try:
        with pytest.raises(ValueError, match='Stop the Loom service'):
            migration.migrate_database(service.store.path, backup=tmp_path / 'backup.sqlite')
        assert not (tmp_path / 'backup.sqlite').exists()
    finally:
        service.close()
    with pytest.raises(ValueError, match='does not exist'):
        migration.migrate_database(tmp_path / 'missing.sqlite')
    assert not (tmp_path / 'missing.sqlite').exists()


def test_incremental_publication_never_reads_old_storage_or_session_context(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path, publish=False, frozen_scope=False)
    with store.transaction() as db:
        job = json.loads(db.execute('SELECT body FROM semantic_jobs').fetchone()[0])
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table in {'sessions', 'semantic_jobs', 'memory_annotations', 'memory_suppressions'}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        queries = []
        db.set_trace_callback(queries.append)
        sync_job(db, job)
        assert not any("name='context'" in q or 'session_messages' in q for q in queries)
        db.set_authorizer(None)
    assert MemoryStore(store.path, workspace=tmp_path).browse()['total'] == 1


def test_unfinished_import_cannot_be_read_via_legacy_mode(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    with store.transaction() as db:
        db.execute("UPDATE memory_namespaces SET mode='legacy'")
    from loom.memory.store import MemoryError

    with pytest.raises(MemoryError, match='offline migration'):
        MemoryStore(store.path, workspace=tmp_path).search('planets')


def test_old_memory_schema_is_upgraded_only_by_explicit_command(tmp_path):
    store = SessionStore(tmp_path / 'data')
    with store.transaction() as db:
        db.execute('ALTER TABLE memory_index_state DROP COLUMN last_outbox')
    with pytest.raises(RuntimeError, match='offline upgrade'):
        LoomService(store.directory)
    with store.read() as db:
        assert 'last_outbox' not in {r[1] for r in db.execute('PRAGMA table_info(memory_index_state)')}
    migration.migrate_database(store.path)
    service = LoomService(store.directory)
    service.close()


def test_import_does_not_overwrite_backup(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path, publish=False)
    backup = tmp_path / 'existing.sqlite'
    backup.write_bytes(b'preserve existing backup')
    with pytest.raises(FileExistsError):
        migration.migrate_database(store.path, backup=backup)
    assert backup.read_bytes() == b'preserve existing backup'
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_observations').fetchone()[0] == 0
