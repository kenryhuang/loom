"""Bounded background indexing, with durable status and atomic document publication."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from loom.core import now_iso
from loom.service.contracts import ServiceError, canonical, new_id, object_value, text


class KnowledgeJobs:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="loom-knowledge")
        self.closed = False
        with store.connect() as db:
            for row in db.execute("SELECT id,body FROM jobs").fetchall():
                job = json.loads(row[1])
                if job["state"] in {"queued", "running"}:
                    job.update(state="failed", error="Service restarted during indexing. Reimport the document; the previous index is intact.")
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (canonical(job), row[0]))

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
        with self.lock:
            with self.store.connect() as db:
                if self.closed:
                    raise ServiceError("Knowledge service is shutting down", 503)
                active = [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.state') IN ('queued','running')")]
                if len(active) >= 8 or any(job["knowledge_base_id"] == kb_id for job in active):
                    raise ServiceError("Indexing is already pending; wait for the current job", 409)
                job = {"id": new_id("index"), "knowledge_base_id": kb_id, "document": payload["name"], "state": "queued", "created_at": now_iso()}
                db.execute("INSERT INTO jobs VALUES(?,?,?)", (job["id"], kb_id, canonical(job)))
            self.pool.submit(self._run, job, payload)
        return job

    def _save(self, job):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET body=? WHERE id=?", (canonical(job), job["id"]))

    def _run(self, job, payload):
        job = dict(job)
        job["state"] = "running"
        self._save(job)
        try:
            job["result"] = self.store.index(job["knowledge_base_id"], payload)
            job["state"] = "completed"
        except Exception as exc:
            job.update(state="failed", error=str(exc) if isinstance(exc, ServiceError) else "Document indexing failed")
        job["finished_at"] = now_iso()
        self._save(job)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
