"""Per-session subscriptions, independent of SQLite's writer lock."""

import threading
from contextlib import contextmanager


class Notifications:
    def __init__(self):
        self.lock = threading.Lock()
        self.listeners = {}

    @contextmanager
    def subscribe(self, sid):
        with self.lock:
            condition = self.listeners.setdefault(sid, [threading.Condition(), 0])
            condition[1] += 1
        try:
            yield condition[0]
        finally:
            with self.lock:
                condition[1] -= 1
                if not condition[1]:
                    self.listeners.pop(sid, None)

    def notify(self, sid):
        with self.lock:
            entry = self.listeners.get(sid)
        if entry:
            with entry[0]:
                entry[0].notify_all()
