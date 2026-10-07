"""Durable indexing/sync jobs with bounded concurrency, progress and cancellation."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

from loom.core import now_iso
from loom.service.contracts import ServiceError, canonical, new_id, object_value, text


class KnowledgeJobs:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="loom-knowledge")
        self.closed = False
        self.cancels = {}
        self.stopping = threading.Event()
        with store.connect() as db:
            for row in db.execute("SELECT id,body FROM jobs").fetchall():
                job = json.loads(row[1])
                if job["state"] in {"queued", "running"}:
                    job.update(state="failed", stage="interrupted", error="Service restarted. Retry the import or website sync; the previous index is intact.")
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (canonical(job), row[0]))
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name="loom-web-sync")
        self.scheduler.start()

    def get(self, kb_id, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=? AND kb_id=?", (job_id, kb_id)).fetchone()
            if not row:
                raise ServiceError("Indexing job not found", 404)
            return json.loads(row[0])

    def start(self, kb_id, payload):
        base = self.store.get(kb_id)
        payload = object_value(payload)
        if base["engine"] == "yakdb_local":
            from loom.knowledge.yakdb_local import document_payload

            document_payload(payload)
        else:
            if set(payload) != {"name", "content"}:
                raise ServiceError("Document requires name and content")
            text(payload["name"], "name", max_length=300)
            text(payload["content"], "content", max_length=500000)
        return self._start(kb_id, "document", payload["name"], payload)

    def sync(self, kb_id, source_id):
        from loom.knowledge.web_sources import source

        spec = source(self.store, kb_id, source_id)
        if not spec:
            raise ServiceError("Website source not found", 404)
        return self._start(kb_id, "website", spec["url"], {"source_id": source_id})

    def remove(self, kb_id, document_id):
        base = self.store.get(kb_id)
        doc = next((d for d in base["documents"] if d["id"] == document_id), None)
        if not doc:
            raise ServiceError("Document not found", 404)
        return self._start(kb_id, "delete", doc["name"], {"document_id": document_id})

    def _start(self, kb_id, kind, document, payload):
        with self.lock:
            with self.store.connect() as db:
                if self.closed:
                    raise ServiceError("Knowledge service is shutting down", 503)
                active = [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.state') IN ('queued','running')")]
                if len(active) >= 8 or any(job["knowledge_base_id"] == kb_id for job in active):
                    raise ServiceError("Indexing is already pending; wait for the current job", 409)
                job = {"id": new_id("index"), "knowledge_base_id": kb_id, "kind": kind, "document": document, "state": "queued",
                       "created_at": now_iso(), "stage": "queued", "progress": [], **({"source_id": payload["source_id"]} if kind == "website" else {})}
                db.execute("INSERT INTO jobs VALUES(?,?,?)", (job["id"], kb_id, canonical(job)))
                if kind == "website":
                    row = db.execute("SELECT body FROM web_sources WHERE id=?", (payload["source_id"],)).fetchone()
                    spec = json.loads(row[0])
                    spec.update(last_attempt_at=now_iso(), last_job_id=job["id"])
                    db.execute("UPDATE web_sources SET body=? WHERE id=?", (canonical(spec), spec["id"]))
            cancelled = threading.Event()
            self.cancels[job["id"]] = cancelled
            self.pool.submit(self._run, job, payload, cancelled)
        return job

    def cancel(self, kb_id, job_id):
        with self.lock:
            job = self.get(kb_id, job_id)
            event = self.cancels.get(job_id)
            if event:
                event.set()
            return {**job, "cancel_requested": event is not None}

    def _save(self, job):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET body=? WHERE id=?", (canonical(job), job["id"]))

    def _run(self, job, payload, cancelled):
        job = dict(job)
        started = time.monotonic()
        job.update(state="running", started_at=now_iso())
        self._save(job)

        def progress(**value):
            job.update(value, elapsed_seconds=round(time.monotonic() - started, 1), updated_at=now_iso())
            job["progress"] = [*job.get("progress", [])[-59:], {"at": now_iso(), **value}]
            self._save(job)

        try:
            if cancelled.is_set():
                raise ServiceError("Knowledge job cancelled", 409)
            if job["kind"] == "website":
                from loom.knowledge.web_sources import sync

                result = sync(self.store, job["knowledge_base_id"], payload["source_id"], progress=progress, cancelled=cancelled.is_set)
            elif job["kind"] == "delete":
                result = self.store.remove_document(job["knowledge_base_id"], payload["document_id"], progress=progress, cancelled=cancelled.is_set)
            else:
                result = self.store.index(job["knowledge_base_id"], payload, progress=progress, cancelled=cancelled.is_set)
            job.update(state="completed", stage="partial" if job["kind"] == "website" and not result.get("complete", True) else "completed",
                       result=result)
        except Exception as exc:
            job.update(state="cancelled" if cancelled.is_set() else "failed",
                       error=str(exc) if isinstance(exc, ServiceError) else f"Knowledge job failed ({type(exc).__name__})")
        job.update(finished_at=now_iso(), elapsed_seconds=round(time.monotonic() - started, 1))
        self._save(job)
        with self.lock:
            self.cancels.pop(job["id"], None)

    def _schedule(self):
        while not self.stopping.wait(30):
            with self.store.connect() as db:
                entries = [json.loads(row[0]) for row in db.execute("SELECT body FROM web_sources")]
            for source in entries:
                if not source.get("enabled") or not source.get("interval_hours"):
                    continue
                last = source.get("last_attempt_at") or source.get("last_sync_at")
                elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() if last else float("inf")
                if elapsed < source["interval_hours"] * 3600:
                    continue
                with suppress(ServiceError):  # Busy jobs are retried on the next scheduler pass.
                    self.sync(source["knowledge_base_id"], source["id"])

    def close(self):
        with self.lock:
            self.closed = True
            self.stopping.set()
            for event in self.cancels.values():
                event.set()
        self.scheduler.join(timeout=2)
        self.pool.shutdown(wait=True)
