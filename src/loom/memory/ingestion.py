"""Incremental publication of a single evaluation into the Memory V2 authority."""

from loom.memory.repository import Repository, digest, signature
from loom.memory.store import bound_workspace, serialized
from loom.service.session_storage import Session


def sync_job(db, job):
    sid = job["session_id"]
    head = db.execute("SELECT deleted_at FROM session_heads WHERE id=?", (sid,)).fetchone()
    if not head:
        return
    if head[0]:
        Repository(".").invalidate(db, evaluation_id=job["id"])
        return
    scope = job.get("memory_scope")
    workspace = scope.get("workspace") if isinstance(scope, dict) else bound_workspace(Session(db, sid, "root")["task"])
    ingest_job(db, job, workspace)


def ingest_job(db, job, workspace, *, mode="runtime_v2", project=None, fingerprint_extra=()):
    """Shared write primitive; historical conversion is supplied only by the offline importer."""
    sid, evaluation = job["session_id"], job["id"]
    repo = Repository(".")
    ns = repo.namespace(db, workspace, sid, mode=mode)
    memory = job.get("checkpoint", {}).get("memory", {})
    source_digest = memory.get("identity", {}).get("source")
    records = memory.get("records", {})
    fingerprint = digest([workspace, source_digest, records, *fingerprint_extra])
    prior = db.execute("SELECT digest FROM memory_source_cursors WHERE evaluation_id=?", (evaluation,)).fetchone()
    if prior and prior[0] == fingerprint:
        return
    candidates = []
    for key, record in records.items():
        if not isinstance(record, dict) or record.get("kind") not in {"facts", "lessons", "workflows"}:
            continue
        if record.get("publication_state", "published") != "published":
            continue
        if (
            record.get("id") != key
            or record.get("run_id") != job.get("run_id")
            or not source_digest
            or record.get("source_digest") != source_digest
            or not record.get("evidence_refs")
            or not record.get("citations")
            or not all(isinstance(record.get(k), str) for k in ("title", "summary", "scope"))
            or len(serialized(record)) > 64000
        ):
            db.execute("INSERT OR REPLACE INTO memory_migration_issues VALUES(?,?,?)", (evaluation, key, "Invalid identity, content or provenance"))
            continue
        source = dict(
            session_id=sid,
            run_id=job.get("run_id"),
            analysis_id=job.get("analysis_id"),
            evaluation_id=evaluation,
            record_id=key,
            source_digest=source_digest,
            content_digest=digest(record),
            created_at=job.get("created_at"),
            evaluation_state=job.get("state"),
        )
        projected = project(record, source) if project else [dict(record=record, source=source)]
        # Import all denial tombstones BEFORE any accepted knowledge is exposed.
        if not projected:
            db.execute("INSERT OR IGNORE INTO memory_tombstones VALUES(?,?,?)", (ns, signature(record), "legacy human rejection"))
        candidates.append((record, source, projected))
    current = {(s["record_id"], s["content_digest"]) for _, s, _ in candidates}
    old = db.execute(
        "SELECT id,record_id,revision FROM memory_sources WHERE evaluation_id=? AND available=1 AND revision NOT LIKE 'human:%'", (evaluation,)
    ).fetchall()
    changed = []
    for src in old:
        if (src["record_id"], src["revision"]) not in current:
            changed.extend(
                r[0]
                for r in db.execute(
                    """SELECT s.version_id FROM memory_supports s
                JOIN memory_observations o ON o.id=s.observation_id WHERE o.source_id=?""",
                    (src["id"],),
                )
            )
            db.execute("UPDATE memory_sources SET available=0 WHERE id=?", (src["id"],))
    repo.refresh_support(db, changed)
    for original, source, projected in candidates:
        value = projected[0]["record"] if projected else original
        added = repo.ingest(db, ns, value, source, denied=not projected)
        if added and projected:
            from loom.memory.consolidation import enqueue

            enqueue(db, ns, added, job, shadow=mode == "shadow")
        # Historical reads must return extraction text, never a rewritten projection.
        db.execute(
            "UPDATE memory_observations SET body=? WHERE source_id IN (SELECT id FROM memory_sources WHERE evaluation_id=? AND record_id=? AND revision=?)",
            (serialized(original), evaluation, source["record_id"], source["content_digest"]),
        )
    repo.refresh_support(db, changed)
    if changed:
        repo.bump(db, ns, changed)
    db.execute("INSERT OR REPLACE INTO memory_source_cursors VALUES(?,?)", (evaluation, fingerprint))
