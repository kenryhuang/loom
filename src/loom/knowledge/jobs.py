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
        self.actions = {}
        self.stopping = threading.Event()
        with store.connect() as db:
            for row in db.execute("SELECT id,body FROM jobs").fetchall():
                job = json.loads(row[1])
                if job["state"] in {"queued", "running"}:
                    finished = now_iso()
                    resumable = db.execute('SELECT 1 FROM job_inputs WHERE id=?', (row[0],)).fetchone()
                    job.update(state="paused" if resumable else "failed", stage="interrupted", failed_stage=job.get("stage"),
                               finished_at=finished, updated_at=finished,
                               error="Service restarted. Resume to continue the saved import." if resumable
                               else "Service restarted. Start a new import; this legacy job has no saved input.")
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (canonical(job), row[0]))
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name="loom-web-sync")
        self.scheduler.start()

    def get(self, kb_id, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=? AND kb_id=?", (job_id, kb_id)).fetchone()
            if not row:
                raise ServiceError("Indexing job not found", 404)
            job = json.loads(row[0])
            job['resumable'] = db.execute('SELECT 1 FROM job_inputs WHERE id=?', (job_id,)).fetchone() is not None
            event = self.cancels.get(job_id)
            if event is not None:
                job["cancel_requested"] = event.is_set()
                job["requested_action"] = self.actions.get(job_id)
            return job

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

    def import_local(self, kb_id, payload):
        from loom.knowledge.local_sources import paths

        manifest = paths(self.store, kb_id, payload)
        return self._start(kb_id, 'local', manifest['root'], manifest)

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
                active = [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.state') IN ('queued','running','paused')")]
                if len(active) >= 8 or any(job["knowledge_base_id"] == kb_id for job in active):
                    raise ServiceError("Indexing is already pending; wait for the current job", 409)
                job = {"id": new_id("index"), "knowledge_base_id": kb_id, "kind": kind, "document": document, "state": "queued",
                       "created_at": now_iso(), "stage": "queued", "progress": [], **({"source_id": payload["source_id"]} if kind == "website" else {})}
                db.execute("INSERT INTO jobs VALUES(?,?,?)", (job["id"], kb_id, canonical(job)))
                db.execute("INSERT INTO job_inputs VALUES(?,?)", (job["id"], canonical(payload)))
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
        return self.control(kb_id, job_id, 'terminate')

    def control(self, kb_id, job_id, action):
        with self.lock:
            job = self.get(kb_id, job_id)
            event = self.cancels.get(job_id)
            if action == 'clear':
                if event or job['state'] in {'queued', 'running', 'paused'}:
                    raise ServiceError('Terminate or finish this job before clearing its history', 409)
                with self.store.connect() as db:
                    db.execute('DELETE FROM job_inputs WHERE id=?', (job_id,))
                    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
                return {'cleared': job_id}
            if action == 'resume':
                if self.closed or event or job['state'] not in {'paused', 'failed'}:
                    raise ServiceError('Only stopped paused/failed jobs can resume', 409)
                with self.store.connect() as db:
                    if db.execute("SELECT 1 FROM jobs WHERE kb_id=? AND id!=? AND json_extract(body,'$.state') IN ('queued','running','paused')",
                                  (kb_id, job_id)).fetchone():
                        raise ServiceError('Another import is pending for this knowledge base', 409)
                    row = db.execute('SELECT body FROM job_inputs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    raise ServiceError('This older job has no saved input; start a new import', 409)
                job.update(state='queued', stage='queued', error=None, cancel_requested=False)
                self._save(job)
                event = threading.Event()
                self.cancels[job_id] = event
                self.actions.pop(job_id, None)
                self.pool.submit(self._run, job, json.loads(row[0]), event)
            elif action in {'pause', 'terminate'}:
                if event:
                    self.actions[job_id] = action
                    event.set()
                elif action == 'terminate' and job['state'] in {'paused', 'failed'}:
                    job.update(state='cancelled', stage='cancelled', finished_at=now_iso())
                    self._save(job)
            else:
                raise ServiceError('Unknown import job action')
            return self.get(kb_id, job_id)

    def delete_base(self, kb_id):
        import shutil

        with self.lock:
            self.store.get(kb_id)
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE kb_id=? AND json_extract(body,'$.state') IN ('queued','running','paused')", (kb_id,)).fetchone():
                    raise ServiceError('Terminate active or paused imports before deleting the knowledge base', 409)
                for row in db.execute('SELECT id FROM documents WHERE kb_id=?', (kb_id,)).fetchall():
                    self.store._remove_chunks(db, row[0])
                db.execute('DELETE FROM job_inputs WHERE id IN (SELECT id FROM jobs WHERE kb_id=?)', (kb_id,))
                for table in ('documents', 'local_sources', 'jobs', 'web_sources', 'web_pages'):
                    db.execute(f'DELETE FROM {table} WHERE kb_id=?', (kb_id,))
                db.execute('DELETE FROM bases WHERE id=?', (kb_id,))
            for engine in ('yakdb', 'lightrag', 'website_snapshots'):
                shutil.rmtree(self.store.directory / engine / kb_id, ignore_errors=True)
            return {'deleted': kb_id}

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
            elif job["kind"] == "local":
                from loom.knowledge.local_sources import run

                def checkpoint(value):
                    with self.store.connect() as db:
                        db.execute('UPDATE job_inputs SET body=? WHERE id=?', (canonical(value), job['id']))
                result = run(self.store, job['knowledge_base_id'], payload, progress, cancelled.is_set, checkpoint)
            elif job["kind"] == "delete":
                result = self.store.remove_document(job["knowledge_base_id"], payload["document_id"], progress=progress, cancelled=cancelled.is_set)
            else:
                result = self.store.index(job["knowledge_base_id"], payload, progress=progress, cancelled=cancelled.is_set)
            job.update(state="completed", stage="partial" if job["kind"] == "website" and not result.get("complete", True) else "completed",
                       result=result)
        except Exception as exc:
            stage = job.get("stage", "indexing")
            job.update(state="cancelled" if cancelled.is_set() else "failed", failed_stage=stage.removesuffix("_failed"),
                       stage="cancelled" if cancelled.is_set() else stage if stage.endswith("_failed") else "failed",
                       error=str(exc) if isinstance(exc, ServiceError) else f"Knowledge job failed ({type(exc).__name__})")
        if job['state'] == 'cancelled' and self.actions.get(job['id']) == 'pause':
            job.update(state='paused', stage='paused', error=None)
        finished = now_iso()
        job.update(finished_at=finished, updated_at=finished, elapsed_seconds=round(time.monotonic() - started, 1), cancel_requested=cancelled.is_set())
        job["progress"] = [*job.get("progress", [])[-59:], {"at": finished, "stage": job["stage"], "state": job["state"],
                                                           **({"detail": job["error"]} if job.get("error") else {})}]
        self._save(job)
        with self.lock:
            self.cancels.pop(job["id"], None)
            self.actions.pop(job["id"], None)

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
            for jid, event in self.cancels.items():
                self.actions[jid] = "pause"
                event.set()
        self.scheduler.join(timeout=2)
        self.pool.shutdown(wait=True)
