"""SQLite concurrency contracts: WAL journaling, lock-free reads, supervisor resilience."""

import sqlite3
import threading

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.store import AgentStore
from loom.service.controller import LoomService
from loom.service.store import SessionStore
from tests.service.fakes import provider_factory
from tests.service.test_controller import create, wait_state


def run_in_thread(fn, *, seconds=3):
    """Run fn in a thread; join up to `seconds`. Returns (outcome, thread)."""
    outcome = {}

    def target():
        try:
            outcome["value"] = fn()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return outcome, thread


@pytest.fixture
def held_write_lock():
    """Hold BEGIN IMMEDIATE on a database until the test releases it."""
    lockers = []

    def hold(path):
        db = sqlite3.connect(path, timeout=5)
        db.execute("BEGIN IMMEDIATE")
        lockers.append(db)
        return db

    yield hold
    for db in lockers:
        db.rollback()
        db.close()


def test_service_database_uses_wal_journal_mode(tmp_path):
    store = SessionStore(tmp_path / "service")
    with sqlite3.connect(store.path) as db:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_list_sessions_proceeds_while_write_lock_is_held(tmp_path, held_write_lock):
    store = SessionStore(tmp_path / "service")
    store.create("create-1", {"objective": "Work", "workspace": str(tmp_path)})
    held_write_lock(store.path)
    outcome, thread = run_in_thread(store.list_sessions, seconds=3)
    blocked = thread.is_alive()
    thread.join(15)  # Let a blocked thread hit its busy timeout before assertions.
    assert not blocked, "reads must not wait behind a held write lock"
    assert "error" not in outcome
    assert len(outcome["value"]) == 1


def test_session_reads_proceed_while_write_lock_is_held(tmp_path, held_write_lock):
    store = SessionStore(tmp_path / "service")
    sid = store.create("create-1", {"objective": "Work", "workspace": str(tmp_path)})["session_id"]
    held_write_lock(store.path)
    outcome, thread = run_in_thread(lambda: (store.snapshot(sid), store.events(sid)), seconds=3)
    blocked = thread.is_alive()
    thread.join(15)
    assert not blocked, "session snapshot/events must not wait behind a held write lock"
    assert "error" not in outcome
    snapshot, events = outcome["value"]
    assert snapshot["session_id"] == sid
    assert events


def test_budget_summaries_proceed_while_write_lock_is_held(tmp_path, held_write_lock):
    sessions = SessionStore(tmp_path / "service")
    AgentStore(sessions)  # Creates the agent_* tables.
    ledger = BudgetLedger(sessions.path)
    held_write_lock(sessions.path)
    outcome, thread = run_in_thread(lambda: ledger.summary("agent_absent"), seconds=3)
    blocked = thread.is_alive()
    thread.join(15)
    assert not blocked, "budget reads must not wait behind a held write lock"
    assert "error" not in outcome
    assert outcome["value"] == []


def test_supervisor_survives_transient_store_errors(tmp_path, monkeypatch):
    service = LoomService(tmp_path / "data", provider_factory=provider_factory).start()
    try:
        original = service.store.list_sessions
        failures = {"count": 0}

        def flaky():
            if failures["count"] < 5:
                failures["count"] += 1
                raise sqlite3.OperationalError("database is locked")
            return original()

        monkeypatch.setattr(service.store, "list_sessions", flaky)
        sid = create(service, tmp_path / "workspace")
        wait_state(service, sid, "idle", timeout=10)
        assert failures["count"] == 5
    finally:
        service.close()
