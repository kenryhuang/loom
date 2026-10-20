"""Evaluation output ownership and crash-recoverable permanent deletion."""

from __future__ import annotations

import json
import logging

from loom.service.contracts import ServiceError, canonical

log = logging.getLogger(__name__)


class EvaluationArtifacts:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS evaluation_artifacts(
                evaluation_id TEXT NOT NULL, session_id TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(evaluation_id,digest))""")
            db.execute("""CREATE TABLE IF NOT EXISTS evaluation_deletions(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, analysis_id TEXT NOT NULL, digests TEXT NOT NULL)""")
            for row in db.execute("SELECT body FROM semantic_jobs").fetchall():
                body = json.loads(row[0])
                if body.get("result"):
                    self.track(db, body, body["result"])
            pending = [row[0] for row in db.execute("SELECT id FROM evaluation_deletions")]
        for identifier in pending:
            try:
                self.finish(identifier)
            except OSError:
                log.warning("Evaluation %s file cleanup is pending; retry deletion", identifier)

    def track(self, db, body, ref):
        db.execute("INSERT OR IGNORE INTO evaluation_artifacts VALUES(?,?,?)", (body["id"], body["session_id"], ref["sha256"]))

    @staticmethod
    def referenced(db, sid, digest):
        # Preserve explicit references outside evaluation output ownership too.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("sessions", "events", "semantic_jobs", "session_analyses", "diagnosis_jobs", "operations"):
            if table not in tables:
                continue
            key = "id" if table == "sessions" else "session_id"
            if db.execute(f"SELECT 1 FROM {table} WHERE {key}=? AND instr(body,?)>0 LIMIT 1", (sid, digest)).fetchone():
                return True
        return False

    def prepare(self, db, body):
        sid, identifier = body["session_id"], body["id"]
        jobs = {row["id"]: json.loads(row["body"]) for row in db.execute("SELECT id,body FROM semantic_jobs WHERE session_id=?", (sid,))}
        for job in jobs.values():
            if job.get("result"):
                self.track(db, job, job["result"])
        # Older resumes discarded result refs. Recover ownership where the payload
        # records it; remaining unreferenced evaluation outputs are legacy orphans.
        candidates = set()
        for row in db.execute("SELECT digest,ref FROM artifacts WHERE session_id=?", (sid,)).fetchall():
            ref = json.loads(row["ref"])
            if ref.get("kind") != "trajectory_evaluation":
                continue
            digest = row["digest"]
            candidates.add(digest)
            if not db.execute("SELECT 1 FROM evaluation_artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone():
                try:
                    payload = json.loads(self.store.artifacts.read(digest))
                except ServiceError as exc:
                    if exc.status != 404:
                        raise
                    continue
                owner = jobs.get(payload.get("analysis_id")) if isinstance(payload, dict) else None
                if owner:
                    self.track(db, owner, ref)
        candidates.update(row[0] for row in db.execute("SELECT digest FROM evaluation_artifacts WHERE evaluation_id=?", (identifier,)))
        db.execute("DELETE FROM semantic_jobs WHERE id=?", (identifier,))
        db.execute("DELETE FROM evaluation_artifacts WHERE evaluation_id=?", (identifier,))
        removed = []
        for digest in sorted(candidates):
            if db.execute("SELECT 1 FROM evaluation_artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone():
                continue
            if self.referenced(db, sid, digest):
                continue
            db.execute("DELETE FROM artifacts WHERE session_id=? AND digest=?", (sid, digest))
            removed.append(digest)
        # Commit metadata removal before unlinking. A crash or failed unlink can
        # be retried at startup or through the same scoped delete endpoint.
        db.execute("INSERT INTO evaluation_deletions VALUES(?,?,?,?)", (identifier, sid, body["analysis_id"], canonical(removed)))

    def finish(self, identifier):
        # All publishers hold the store transaction lock, so a concurrent publish
        # cannot observe a file just before cleanup unlinks it and then register it.
        with self.store.transaction() as db:
            row = db.execute("SELECT digests FROM evaluation_deletions WHERE id=?", (identifier,)).fetchone()
            if not row:
                return
            for digest in json.loads(row[0]):
                if db.execute("SELECT 1 FROM artifacts WHERE digest=?", (digest,)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM evaluation_artifacts WHERE digest=?", (digest,)).fetchone():
                    continue
                self.store.artifacts.delete(digest)
            db.execute("DELETE FROM evaluation_deletions WHERE id=?", (identifier,))
