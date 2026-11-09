import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.core.sqlite import is_busy
from loom.memory.repository import Repository
from loom.service.store import SessionStore
from loom.skills.store import SkillStore


def test_all_repositories_queue_before_sqlite_connect(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    skills = SkillStore(store.path)
    repositories = [BudgetLedger(store.path).transaction, lambda: Repository(store.path).connect(write=True), skills.transaction,
                    SessionStore(tmp_path).transaction]
    original = sqlite3.connect
    connected = threading.Event()

    def connect(*args, **kwargs):
        connected.set()
        kwargs['timeout'] = .01  # SQL-level contention would fail before release.
        return original(*args, **kwargs)

    def write(transaction):
        with transaction() as db:
            db.execute("INSERT INTO skill_dirty_sessions VALUES(?)", (threading.current_thread().name,))

    with ThreadPoolExecutor(max_workers=4) as pool:
        with store.transaction() as db:
            monkeypatch.setattr(sqlite3, 'connect', connect)
            futures = [pool.submit(write, transaction) for transaction in repositories]
            assert not connected.wait(.1), 'writers must queue before opening competing SQLite connections'
            assert db.execute('SELECT COUNT(*) FROM skill_dirty_sessions').fetchone()[0] == 0
        for future in futures:
            future.result(timeout=3)
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM skill_dirty_sessions').fetchone()[0] == 4


def test_nested_repository_writes_share_transaction_and_rollback(tmp_path):
    store = SessionStore(tmp_path)
    repo, ledger = Repository(store.path), BudgetLedger(store.path)
    with pytest.raises(RuntimeError, match='outer'), store.transaction() as outer:
        with ledger.transaction() as inner:
            assert inner is outer
            inner.execute("INSERT INTO skill_dirty_sessions VALUES('budget')")
        with pytest.raises(ValueError), repo.connect(write=True) as inner:
            inner.execute("INSERT INTO skill_dirty_sessions VALUES('rolled-back')")
            raise ValueError('inner')
        assert [r[0] for r in outer.execute('SELECT session_id FROM skill_dirty_sessions')] == ['budget']
        raise RuntimeError('outer')
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM skill_dirty_sessions').fetchone()[0] == 0
    with repo.connect(write=True) as db:
        db.execute("INSERT INTO skill_dirty_sessions VALUES('committed')")


def test_storage_errors_are_classified_narrowly():
    assert is_busy(sqlite3.OperationalError('database is locked'))
    assert not is_busy(sqlite3.OperationalError('disk I/O error'))
    assert not is_busy(RuntimeError('database is locked'))
