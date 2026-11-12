"""One write queue per database, shared by all in-process repositories.

WAL readers use their own connections. Nested writers use savepoints on the
current connection instead of waiting for their own SQLite lock.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class _Writer:
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()


_writers = {}
_guard = threading.Lock()


def _writer(database):
    key = str(Path(database).resolve())
    with _guard:
        if key not in _writers:
            _writers[key] = _Writer()
        return _writers[key]


def write_lock(database):
    return _writer(database).lock


def after_commit(database, callback, *, key=None):
    """Run only after the outer writer commits; savepoint rollback discards it."""
    writer = _writer(database)
    if getattr(writer.local, "connection", None) is None:
        callback()
    else:
        writer.local.callbacks.append((key, callback))


def is_busy(exc):
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", 0) & 255
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or str(exc) in {
        "database is locked", "database table is locked",
    }


@contextmanager
def write_transaction(database):
    writer = _writer(database)
    with writer.lock:
        current = getattr(writer.local, "connection", None)
        if current is not None:
            callback_start = len(writer.local.callbacks)
            writer.local.sequence += 1
            savepoint = f"loom_write_{writer.local.sequence}"
            current.execute(f"SAVEPOINT {savepoint}")
            try:
                yield current
                current.execute(f"RELEASE {savepoint}")
            except BaseException:
                del writer.local.callbacks[callback_start:]
                current.execute(f"ROLLBACK TO {savepoint}")
                current.execute(f"RELEASE {savepoint}")
                raise
            return
        db = sqlite3.connect(database, timeout=10)
        db.row_factory = sqlite3.Row
        callbacks = []
        try:
            db.execute("BEGIN IMMEDIATE")
            writer.local.connection, writer.local.sequence = db, 0
            writer.local.callbacks = []
            yield db
            db.commit()
            callbacks = writer.local.callbacks
        except BaseException:
            db.rollback()
            raise
        finally:
            writer.local.connection = None
            writer.local.callbacks = []
            db.close()
    notified = set()
    for key, callback in callbacks:
        if key is None or key not in notified:
            callback()
            notified.add(key)
