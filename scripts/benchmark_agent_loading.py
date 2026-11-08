"""Read-only Agent loading benchmark; does not initialize or start a service.

Example:
  .venv/bin/python scripts/benchmark_agent_loading.py runs/service/service.sqlite
  .venv/bin/python scripts/benchmark_agent_loading.py snapshot.sqlite --source /tmp/old/src

Use the same consistent database snapshot for before/after comparisons. Output
contains timings, counts and sizes, never task contents. Live DB measurements
can vary as background tasks update it. This is a read/serialization benchmark,
not an HTTP or browser latency benchmark.
"""

import argparse
import json
import math
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "src")
    parser.add_argument("--repeats", type=int, default=12)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from loom.autonomy.budget import BudgetLedger
    from loom.autonomy.store import AgentStore
    from loom.service.autonomy import AutonomyService
    from loom.service.contracts import canonical
    from loom.service.store import SessionStore

    path = args.database.resolve()

    @contextmanager
    def read(_self):
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    # Constructors run migrations/recovery; a benchmark must never run them.
    sessions = SessionStore.__new__(SessionStore)
    sessions.path = path
    sessions.read = read.__get__(sessions)
    BudgetLedger.read = read
    store = AgentStore.__new__(AgentStore)
    store.sessions = sessions
    fixed_now = datetime.now(UTC)
    store.clock = lambda: fixed_now
    facade = AutonomyService.__new__(AutonomyService)
    facade.store = store
    facade.service = SimpleNamespace(autonomy_enabled=True, active={}, capacity=1)
    facade.supervisor = SimpleNamespace(last_error=None)
    with sessions.read() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM agents ORDER BY rowid")]
        admission_index = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='sessions_admission'").fetchone())

    def measure(fn, *, serialize=True):
        fn()  # Warm imports and page cache equally for both versions.
        elapsed, cpu = [], []
        for _ in range(args.repeats):
            start, cpu_start = time.perf_counter(), time.process_time()
            result = fn()
            if serialize:
                body = canonical(result).encode()
            elapsed.append((time.perf_counter() - start) * 1000)
            cpu.append((time.process_time() - cpu_start) * 1000)
        if not serialize:
            body = canonical(result).encode()
        return dict(
            p50_ms=round(statistics.median(elapsed), 2),
            p95_ms=round(sorted(elapsed)[math.ceil(len(elapsed) * 0.95) - 1], 2),
            cpu_p50_ms=round(statistics.median(cpu), 2),
            bytes=len(body),
        )

    result = dict(repeats=args.repeats, agents=len(ids), admission_index=admission_index, measurements={})
    measurements = result["measurements"]
    measurements["scheduler"] = measure(getattr(sessions, "scheduling_sessions", sessions.list_sessions), serialize=False)
    if hasattr(sessions, "session_page"):

        def listing():
            return sessions.session_page(limit=200)
    else:

        def listing():
            states = sessions.list_sessions_by_activity()
            return dict(
                sessions=[{k: s[k] for k in ("session_id", "title", "task", "event_cursor")} for s in states[:200]],
                next_offset=200 if len(states) > 200 else None,
            )

    measurements["session_list"] = measure(listing)
    measurements["agent_list"] = measure(lambda: facade.dispatch("GET", [], {}))
    for index, aid in enumerate(ids):
        for view in ("overview", "goals", "work", "profile", "knowledge", "learning"):
            measurements[f"agent_{index}/{view}"] = measure(lambda aid=aid, view=view: facade.snapshot(aid, view=view))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
