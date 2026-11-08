"""Resumable legacy projection. Reads never run migrations or model inference.

Run: python -m loom.memory.migration --database /path/service.sqlite --backup /path/backup.sqlite
"""

import argparse
import json
import sqlite3
from pathlib import Path

from loom.memory.repository import Repository, digest, signature
from loom.memory.schema import exists, migrate
from loom.memory.store import bound_workspace, serialized


def sync_job(db, job, *, mode="runtime_v2"):
    if not exists(db):
        return
    sid, evaluation = job["session_id"], job["id"]
    session = db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
    if not session:
        return
    session = json.loads(session[0])
    repo = Repository(".")
    if session.get("deleted_at"):
        repo.invalidate(db, evaluation_id=evaluation)
        return
    workspace = job.get("memory_scope", {}).get("workspace") if isinstance(job.get("memory_scope"), dict) else bound_workspace(session["task"])
    ns = repo.namespace(db, workspace, sid, mode=mode)
    memory = job.get("checkpoint", {}).get("memory", {})
    source_digest = memory.get("identity", {}).get("source")
    records = memory.get("records", {})
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    annotations, suppressions = [], set()
    if source_digest and "memory_annotations" in tables:
        annotations = [
            json.loads(r[0])
            for r in db.execute("SELECT body FROM memory_annotations WHERE json_extract(body,'$.source.source_digest')=? ORDER BY rowid", (source_digest,))
        ]
        suppressions = {
            r[0] for r in db.execute("SELECT lineage FROM memory_suppressions WHERE json_extract(body,'$.source.source_digest')=?", (source_digest,))
        }
    fingerprint = digest([workspace, source_digest, records, annotations, sorted(suppressions)])
    prior = db.execute("SELECT digest FROM memory_source_cursors WHERE evaluation_id=?", (evaluation,)).fetchone()
    if prior and prior[0] == fingerprint:
        return
    from loom.memory.annotations import effective

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
        projected = effective([dict(record=record, source=source)], annotations, suppressions)
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
    # Reviews are durable records, including rejected sources that have no visible item.
    for annotation in annotations:
        matches = db.execute(
            """SELECT DISTINCT p.version_id FROM memory_projection p JOIN memory_supports s ON s.version_id=p.version_id
            JOIN memory_observations o ON o.id=s.observation_id JOIN memory_sources src ON src.id=o.source_id
            WHERE p.namespace_id=? AND src.evaluation_id=? AND src.record_id=? AND src.revision=?""",
            (ns, annotation["source"]["evaluation_id"], annotation["source"]["record_id"], annotation["source"]["content_digest"]),
        ).fetchall()
        for match in matches:
            review_id = annotation["id"] + ":" + match[0]
            db.execute(
                "INSERT OR IGNORE INTO memory_reviews VALUES(?,?,?,?,?,?,?)",
                (review_id, ns, match[0], annotation["kind"], annotation["actor"], serialized(annotation), annotation["created_at"]),
            )


def migrate_database(database, *, mode="runtime_v2", backup=None, prepare_only=False):
    repo = Repository(database)
    if backup:
        destination = Path(backup)
        if destination.exists():
            raise ValueError("Backup already exists; choose a new path")
        with sqlite3.connect(repo.path.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
    with repo.connect(write=True) as db:
        migrate(db)
        present = db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone()
        ids = [r[0] for r in db.execute("SELECT id FROM semantic_jobs ORDER BY id")] if present else []
    # Commit each source and its cursor together: restarts replay only incomplete work.
    for identifier in ids:
        with repo.connect(write=True) as db:
            row = db.execute("SELECT body FROM semantic_jobs WHERE id=?", (identifier,)).fetchone()
            if row:
                sync_job(db, json.loads(row[0]), mode="shadow")
    with repo.connect(write=True) as db:
        # Final source reconciliation and cutover share a transaction with writers.
        stale = db.execute("SELECT DISTINCT evaluation_id FROM memory_sources WHERE available=1").fetchall()
        for (identifier,) in stale:
            if not present or not db.execute("SELECT 1 FROM semantic_jobs WHERE id=?", (identifier,)).fetchone():
                repo.invalidate(db, evaluation_id=identifier)
        db.execute(
            "UPDATE memory_namespaces SET mode=?,checkpoint=? WHERE checkpoint IS NULL OR checkpoint='prepared'",
            ("shadow" if prepare_only else mode, "prepared" if prepare_only else "complete"),
        )
        from loom.memory.dependencies import migrate_skills

        migrate_skills(db)
        return report(db)


def report(db):
    return dict(
        namespaces=[dict(r) for r in db.execute("SELECT * FROM memory_namespaces")],
        observations=db.execute("SELECT COUNT(*) FROM memory_observations").fetchone()[0],
        knowledge=db.execute("SELECT COUNT(*) FROM memory_items WHERE canonical_redirect IS NULL").fetchone()[0],
        versions=db.execute("SELECT COUNT(*) FROM memory_item_versions").fetchone()[0],
        rejected=db.execute("SELECT COUNT(*) FROM memory_tombstones").fetchone()[0],
        issues=[dict(r) for r in db.execute("SELECT * FROM memory_migration_issues")],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup")
    parser.add_argument("--prepare-only", action="store_true", help="Backfill without switching reads; startup reconciles and activates")
    parser.add_argument("--mode", choices=["shadow", "library_v2", "runtime_v2"], default="runtime_v2")
    args = parser.parse_args()
    print(json.dumps(migrate_database(args.database, mode=args.mode, backup=args.backup, prepare_only=args.prepare_only), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


def set_mode(database, namespace, mode):
    if mode not in {"legacy", "shadow", "library_v2", "runtime_v2"}:
        raise ValueError("Invalid memory read mode")
    repo = Repository(database)
    with repo.connect(write=True) as db:
        row = db.execute("SELECT * FROM memory_namespaces WHERE id=?", (namespace,)).fetchone()
        if not row or row["checkpoint"] != "complete":
            raise ValueError("Namespace must finish migration before changing its read mode")
        db.execute("UPDATE memory_namespaces SET mode=? WHERE id=?", (mode, namespace))
        repo.event(db, namespace, "READ_MODE", dict(before=row["mode"], after=mode))
        repo.bump(db, namespace)
