import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from loom.memory.consolidation import Consolidator
from loom.memory.worker import MemoryWorker
from loom.service.store import SessionStore


def test_empty_recovery_does_not_acquire_write_lock(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    consolidator = Consolidator(store.path)
    connect = consolidator.repo.connect

    @contextmanager
    def read_only(*, write=False):
        assert not write, 'idle recovery must not compete with budget or session writes'
        with connect() as db:
            yield db

    monkeypatch.setattr(consolidator.repo, 'connect', read_only)
    consolidator.recover()


def test_worker_survives_failure_handler_lock_and_does_not_reuse_stale_row():
    class Closed:
        iterations = 0

        def is_set(self):
            return self.iterations >= 3

        def wait(self, seconds):
            self.iterations += 1

    class Repo:
        writes = 0

        @contextmanager
        def connect(self, *, write=False):
            if write:
                self.writes += 1
                raise sqlite3.OperationalError('database is locked')
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {'id': 'job-a', 'input': '{}'}))

    repo, closed = Repo(), Closed()

    def recover():
        if closed.iterations > 0:
            raise RuntimeError('recovery failed before selecting a job')

    def provider(model):
        raise RuntimeError('configuration failed')

    worker = object.__new__(MemoryWorker)
    worker.closed = closed
    worker.consolidator = SimpleNamespace(repo=repo, recover=recover)
    worker.semantic = SimpleNamespace(provider_factory=provider)
    worker.run()
    assert closed.iterations == 3
    assert repo.writes == 1, 'later recovery failures must not mark the previous iteration\'s job failed'
