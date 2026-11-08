"""Bounded, versioned reuse of the library's scoped read model."""

import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing

from loom.memory.store import MemoryError, serialized


class MemoryLibraryCache:
    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_entries=4):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.size = 0
        self.lock = threading.Lock()

    def records(self, memory):
        # Serialize misses so concurrent filters share one scan. Only the
        # library uses this lock; execution retrieval never waits on it.
        with self.lock:
            try:
                identity = memory.database.stat()
                with closing(sqlite3.connect(memory.database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone():
                        return memory._records(scan_limit=None)
                    revision = db.execute("SELECT value FROM memory_versions WHERE id='effective'").fetchone()
            except (sqlite3.Error, OSError) as exc:
                raise MemoryError('memory.store_unavailable', f'Cannot read memory source: {exc}') from exc
            if revision is None:
                return memory._records(scan_limit=None)
            key = (memory.database, identity.st_dev, identity.st_ino, memory.workspace, memory.session_id)
            prior = self.entries.get(key)
            if prior and prior[0] == revision[0]:
                self.entries.move_to_end(key)
                return prior[1]
            if prior:
                self.size -= self.entries.pop(key)[2]
            result = memory._records(scan_limit=None)
            # Capture the revision before reading. A concurrent publication or
            # annotation makes the next lookup miss, never labels old rows new.
            size = len(serialized(result).encode())
            if size <= self.max_bytes and self.max_entries > 0:
                while self.entries and (self.size + size > self.max_bytes or len(self.entries) >= self.max_entries):
                    self.size -= self.entries.popitem(last=False)[1][2]
                self.entries[key] = (revision[0], result, size)
                self.size += size
            return result
