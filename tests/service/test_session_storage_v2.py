import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from loom.core.sqlite import write_transaction
from loom.service import session_storage as storage
from loom.service.contracts import ServiceError, canonical
from loom.service.store import SessionStore


def create(store, name='one'):
    return store.create(name, {'objective': name})['session_id']


def test_hot_update_does_not_read_or_write_cold_fields(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    sid = create(store)
    store.update(sid, lambda s, e, db: s.update(context={'payload': 'x' * 1_000_000},
        run={'state': 'running', 'active_seconds': 1, 'streams': {'call': 'y' * 300_000}, 'checkpoint': {'ref': 'checkpoint'}}))
    statements = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, 'connect', connect)
    store.update(sid, lambda s, e, db: s['run'].update(active_seconds=2))
    assert not any("SELECT value" in q and ("name='context'" in q or "name='streams'" in q or "name='messages'" in q) for q in statements)
    writes = [q for q in statements if q.startswith('INSERT INTO session_fields')]
    assert any("'active_seconds','2'" in q for q in writes)
    assert not any("'context'" in q or "'streams'" in q for q in writes)
    assert not any('session_messages' in q for q in statements)
    restored = store.snapshot(sid)
    assert restored['context']['payload'] == 'x' * 1_000_000
    assert restored['run']['streams']['call'] == 'y' * 300_000
    assert restored['run']['active_seconds'] == 2


def test_nested_mutations_null_removal_and_rollback(tmp_path):
    store = SessionStore(tmp_path)
    sid = create(store)
    def change(s, e, db):
        s['run'] = {'state': 'running', 'nested': {'a': [1, 2]}, 'gone': True}
        s['optional'] = False
    store.update(sid, change)
    def mutate(s, e, db):
        s['run']['nested']['a'].append(3)
        s['run'].pop('gone')
        s['optional'] = None
    store.update(sid, mutate)
    state = store.snapshot(sid)
    assert state['run']['nested'] == {'a': [1, 2, 3]}
    assert 'gone' not in state['run'] and state['optional'] is None
    with pytest.raises(RuntimeError), store.transaction():
        store.update(sid, lambda s, e, db: s.update(context='rollback', run=None))
        raise RuntimeError()
    assert store.snapshot(sid) == state
    store.update(sid, lambda s, e, db: s.update(run=None))
    assert store.snapshot(sid)['run'] is None


def test_cursor_and_message_pages_use_indexes(tmp_path):
    store = SessionStore(tmp_path)
    sids = [create(store, str(i)) for i in range(5)]
    pages, cursor = [], None
    while True:
        page = store.session_page(cursor=cursor, limit=2)
        pages.extend(s['session_id'] for s in page['sessions'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert pages == [s['session_id'] for s in store.list_sessions_by_activity()]
    assert len(set(pages)) == 5
    for cursor in ('invalid', '', 'a' * 2048):
        with pytest.raises(ServiceError):
            store.session_page(cursor=cursor)
    sid = sids[0]
    for i in range(4):
        store.submit(sid, {'command_id': f'm{i}', 'type': 'submit_message', 'payload': {'content': str(i)}})
    last = store.messages(sid, limit=2)
    previous = store.messages(sid, before=last['next_before'], limit=3)
    assert previous['messages'] + last['messages'] == store.snapshot(sid)['messages']
    assert store.snapshot(sid, messages_limit=2, context=False)['messages'] == last['messages']
    with store.read() as db:
        queries = [
            ("SELECT id FROM session_heads WHERE deleted_at IS NULL AND (last_activity_at,ordinal)<('z',1000) "
             "ORDER BY last_activity_at DESC,ordinal DESC LIMIT 10", 'sessions_activity'),
            (f'SELECT id FROM session_heads WHERE {storage.ADMISSION} ORDER BY ordinal DESC', 'sessions_admission'),
            ("SELECT body FROM operations WHERE session_id='x'", 'operations_session'),
            (f"SELECT body FROM events WHERE session_id='x' AND {storage.PROCESS_PREDICATE} ORDER BY seq", 'events_processes'),
        ]
        for query, index in queries:
            plan = [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + query)]
            assert any(index in p for p in plan), plan
            assert not any('TEMP B-TREE' in p for p in plan), plan


def legacy_database(path, state):
    path.mkdir()
    with sqlite3.connect(path / 'service.sqlite') as db:
        db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE events(session_id TEXT,seq INTEGER,body TEXT,PRIMARY KEY(session_id,seq));
            CREATE TABLE commands(id TEXT PRIMARY KEY,session_id TEXT,input TEXT,receipt TEXT,state TEXT);
            CREATE TABLE operations(id TEXT PRIMARY KEY,session_id TEXT,body TEXT);
            CREATE TABLE artifacts(session_id TEXT,digest TEXT,ref TEXT,PRIMARY KEY(session_id,digest));
            CREATE TABLE worker_records(attempt_id TEXT,record_id TEXT,response TEXT,PRIMARY KEY(attempt_id,record_id));''')
        db.execute('INSERT INTO sessions VALUES(?,?)', (state['session_id'], canonical(state)))


def test_legacy_backup_migration_and_compatibility_round_trip(tmp_path):
    source = SessionStore(tmp_path / 'source')
    sid = create(source)
    state = source.snapshot(sid)
    state.update(context={'中文': [True, False, None, {}, []]}, deleted_at='deleted',
                 run={'state': 'completed', 'boolean': True, 'streams': {}, 'checkpoint': None})
    legacy_database(tmp_path / 'legacy', state)
    store = SessionStore(tmp_path / 'legacy')
    with store.read() as db:
        assert storage.load(db, sid, deleted=True) == state
        assert json.loads(db.execute('SELECT body FROM sessions WHERE id=?', (sid,)).fetchone()[0]) == state
    backup = tmp_path / 'legacy' / 'service.before-session-v2.sqlite'
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT type FROM sqlite_master WHERE name='sessions'").fetchone()[0] == 'table'
        assert json.loads(db.execute('SELECT body FROM sessions').fetchone()[0]) == state
    with store.transaction() as db:
        db.execute("UPDATE sessions SET body=json_set(body,'$.title','updated') WHERE id=?", (sid,))
    state['title'] = 'updated'
    with SessionStore(tmp_path / 'legacy').read() as db:
        assert storage.load(db, sid, deleted=True) == state
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_migration_failure_rolls_back_schema_and_can_retry(tmp_path, monkeypatch):
    source = SessionStore(tmp_path / 'source')
    state = source.snapshot(create(source))
    legacy_database(tmp_path / 'legacy', state)
    original = storage.compatibility_view
    def fail(db):
        raise RuntimeError('interrupted migration')
    monkeypatch.setattr(storage, 'compatibility_view', fail)
    with pytest.raises(RuntimeError, match='interrupted'):
        SessionStore(tmp_path / 'legacy')
    with sqlite3.connect(tmp_path / 'legacy' / 'service.sqlite') as db:
        assert json.loads(db.execute('SELECT body FROM sessions').fetchone()[0]) == state
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='session_heads'").fetchone()
    monkeypatch.setattr(storage, 'compatibility_view', original)
    assert SessionStore(tmp_path / 'legacy').snapshot(state['session_id']) == state


def test_notifications_are_session_scoped_and_only_after_outer_commit(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    first, second = create(store), create(store, 'two')
    calls = []
    monkeypatch.setattr(store.notifications, 'notify', calls.append)
    with write_transaction(store.path):
        store.update(first, lambda s, emit, db: emit('test', {}))
        store.update(first, lambda s, emit, db: emit('second event in same commit', {}))
        assert calls == []
        with pytest.raises(ValueError), store.transaction():
            store.update(second, lambda s, emit, db: emit('test', {}))
            raise ValueError()
    assert calls == [first]
    calls.clear()
    with pytest.raises(ValueError), store.transaction():
        store.update(second, lambda s, emit, db: emit('test', {}))
        raise ValueError()
    assert calls == []
    store.update(first, lambda s, emit, db: s.update(epoch=s['epoch'] + 1))
    assert calls == []


def test_artifact_preparation_is_outside_writer_and_install_survives_cleanup(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    sid = create(store)
    import loom.service.artifacts as module
    original = module.os.fsync
    def fsync(fd):
        with ThreadPoolExecutor(1) as pool:
            # Would block if the publishing thread held the database mutex.
            pool.submit(lambda: store.update(sid, lambda s, e, db: s.update(epoch=2))).result(timeout=2)
        return original(fd)
    monkeypatch.setattr(module.os, 'fsync', fsync)
    ref = store.save_artifact(sid, {'large': 'x' * 10000}, 'test')
    with store.artifacts.prepare({'large': 'x' * 10000}, 'test') as prepared, store.transaction() as db:
        store.artifacts.delete(ref['sha256'])
        store.register_artifact(db, sid, prepared)
    assert json.loads(store.read_artifact(sid, ref['sha256'])) == {'large': 'x' * 10000}
    assert not list(store.artifacts.root.glob('tmp*'))


def test_reads_do_not_wait_for_writer(tmp_path):
    store = SessionStore(tmp_path)
    sid = create(store)
    with ThreadPoolExecutor(1) as pool, store.transaction():
        page = pool.submit(store.session_page).result(timeout=2)
        assert page['sessions'][0]['session_id'] == sid
        assert pool.submit(store.events, sid).result(timeout=2)


def test_supervisor_checkpoint_preparation_does_not_block_other_session_commands(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    import loom.service.artifacts as module
    from loom.runtime.checkpoints import encode
    from loom.service.controller import LoomService

    service = LoomService(tmp_path)
    sid = create(service.store)
    other = service.create('idle', {'title': 'idle'})['session_id']
    descriptor = service.prepare_attempt(sid)
    record = dict(type='publish_artifact', epoch=descriptor['epoch'], attempt_id=descriptor['run']['attempt_id'],
                  record_id='artifact', payload=encode({'value': {'large': 'x' * 10000}, 'kind': 'test'}))
    pending, responses = [record], []
    connection = SimpleNamespace(poll=lambda: bool(pending), recv=lambda: pending.pop(), send=responses.append)
    service.active[sid] = SimpleNamespace(connection=connection, process=SimpleNamespace(is_alive=lambda: True),
                                          accounted=time.monotonic(), terminal_since=None, workspace=None)
    original = module.os.fsync
    commands = []
    def fsync(fd):
        with ThreadPoolExecutor(1) as pool:
            commands.append(pool.submit(service.submit, other, dict(command_id='control', type='set_token_budget',
                                        payload={'max_tokens': 1000})).result(timeout=2))
        return original(fd)
    monkeypatch.setattr(module.os, 'fsync', fsync)
    try:
        service._tick()
        assert commands and responses and 'value' in responses[0]
        assert service.snapshot(other)['task']['limits']['max_tokens'] == 1000
    finally:
        service.active.clear()
        service.close()


def test_running_legacy_supervisor_is_not_upgraded(tmp_path):
    import fcntl

    source = SessionStore(tmp_path / 'source')
    state = source.snapshot(create(source))
    directory = tmp_path / 'legacy'
    legacy_database(directory, state)
    with (directory / 'supervisor.lock').open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='Stop the running supervisor'):
            SessionStore(directory)
    assert not (directory / 'service.before-session-v2.sqlite').exists()
    with sqlite3.connect(directory / 'service.sqlite') as db:
        assert db.execute("SELECT type FROM sqlite_master WHERE name='sessions'").fetchone()[0] == 'table'
