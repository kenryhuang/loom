"""Single service-owned proposal worker with persistent, budgeted jobs."""

import asyncio
import json
import logging
import threading

from loom.core.sqlite import is_busy
from loom.memory.consolidation import Consolidator
from loom.memory.repository import now


class MemoryWorker:
    def __init__(self, semantic):
        self.semantic = semantic
        self.consolidator = Consolidator(semantic.store.path)
        self.closed = threading.Event()
        self.loop = self.task = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="loom-memory")
        self.thread.start()

    def close(self):
        self.closed.set()
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=2)

    def run(self):
        while not self.closed.is_set():
            row = None
            try:
                self.consolidator.recover()
                with self.consolidator.repo.connect() as db:
                    row = db.execute(
                        "SELECT id,input FROM memory_jobs WHERE state='pending' OR (state='deferred' AND lease_until<=?) ORDER BY rowid LIMIT 1", (now(),)
                    ).fetchone()
                if row:
                    value = json.loads(row["input"])
                    if self.semantic.provider_factory:
                        provider = self.semantic.provider_factory(value.get("model"))
                    elif self.semantic.config_path:
                        from loom.evaluation.provider import create_evaluation_provider
                        from loom.tasks.config import load_task_config

                        config = load_task_config(self.semantic.config_path).unwrap()
                        provider = create_evaluation_provider(config, model_name=value.get("model")).unwrap()
                    else:
                        self.closed.wait(2)
                        continue
                    self.loop = asyncio.new_event_loop()
                    self.task = self.loop.create_task(self.consolidator.run(row["id"], provider))
                    try:
                        self.loop.run_until_complete(self.task)
                    except asyncio.CancelledError:
                        pass
                    finally:
                        self.loop.close()
                        self.loop = self.task = None
            except Exception as exc:
                if not is_busy(exc):
                    logging.getLogger(__name__).exception("Memory worker iteration failed")
                    if row:
                        try:
                            with self.consolidator.repo.connect(write=True) as db:
                                db.execute("UPDATE memory_jobs SET state='failed',error=? WHERE id=? AND state='pending'", (str(exc), row["id"]))
                        except Exception:
                            logging.getLogger(__name__).exception("Could not persist memory worker failure; worker will continue")
            self.closed.wait(1)
