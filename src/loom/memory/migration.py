"""Offline, resumable one-time import. Stop the service before running this command.

Run: python -m loom.memory.migration --database /path/service.sqlite --backup /path/backup.sqlite
"""

import argparse
import fcntl
import json
from pathlib import Path

from loom.memory.repository import Repository, digest
from loom.memory.schema import exists, migrate
from loom.memory.store import bound_workspace, serialized


def effective(rows, annotations, suppressions):
    result = []
    for row in rows:
        record, source = dict(row["record"]), dict(row["source"])
        lineage = digest([source["source_digest"], record["kind"], record["title"], record["summary"], record.get("citations")])
        if lineage in suppressions:
            continue
        applied = []
        hidden = False
        for annotation in annotations:
            ref = annotation["source"]
            if ref["source_digest"] != source["source_digest"] or ref["content_digest"] != source["content_digest"]:
                continue
            kind = annotation["kind"]
            if kind in {"reject", "retire", "restore"}:
                hidden = kind != "restore"
            elif kind == "correct":
                record.update(summary=annotation["value"], basis="human_asserted", validation="human_asserted")
            elif kind == "scope":
                record["scope"] += "; Human limitation: " + annotation["value"]
            elif kind == "pin":
                record["pinned"] = True
            elif kind == "tag":
                record.setdefault("tags", []).append(annotation["value"])
            elif kind == "confirm":
                record["human_confirmed"] = True
            if kind in {"confirm", "correct"}:
                record["human_review"] = {key: annotation[key] for key in ("id", "kind", "actor", "agent_id", "created_at")}
            applied.append(annotation["id"])
        if not hidden:
            source["annotation_ids"] = applied
            source["annotation_revision"] = max((a["revision"] for a in annotations if a["id"] in applied), default=0)
            result.append(dict(record=record, source=source))
    return result


def import_job(db, job, *, mode="runtime_v2"):
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
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    annotations, suppressions = [], set()
    if source_digest and "memory_annotations" in tables:
        annotations = [
            json.loads(r[0])
            for r in db.execute("SELECT body FROM memory_annotations WHERE json_extract(body,'$.source.source_digest')=? ORDER BY rowid", (source_digest,))
        ]
    if source_digest and "memory_suppressions" in tables:
        suppressions = {
            r[0] for r in db.execute("SELECT lineage FROM memory_suppressions WHERE json_extract(body,'$.source.source_digest')=?", (source_digest,))
        }
    from loom.memory.ingestion import ingest_job

    ingest_job(db, job, workspace, mode=mode,
               project=lambda record, source: effective([dict(record=record, source=source)], annotations, suppressions),
               fingerprint_extra=(annotations, sorted(suppressions)))
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


def migrate_database(database, *, backup=None):
    """Offline, resumable one-time conversion. Never called by the service."""
    repo = Repository(database)
    if not repo.path.is_file():
        raise ValueError("Service database does not exist")
    with (repo.path.parent / "supervisor.lock").open("a+") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Stop the Loom service before migrating its database") from exc
        return _migrate_database(repo, backup=backup)


def _migrate_database(repo, *, backup):
    with repo.connect() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_import_state'").fetchone()
        if table and db.execute("SELECT 1 FROM memory_import_state WHERE id='complete'").fetchone():
            return report(db)
    if backup:
        from loom.service.session_migration import backup_database

        backup_database(repo.path, Path(backup))
    with repo.connect(write=True) as db:
        migrate(db)
        db.execute("CREATE TABLE IF NOT EXISTS memory_import_state(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS memory_import_jobs(id TEXT PRIMARY KEY)")
        present = db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone()
        ids = [r[0] for r in db.execute("SELECT id FROM semantic_jobs WHERE id NOT IN (SELECT id FROM memory_import_jobs) ORDER BY id")] if present else []
    # Each converted evaluation and completion marker commit together. A retry
    # skips completed jobs BEFORE fetching their potentially huge JSON bodies.
    for identifier in ids:
        with repo.connect(write=True) as db:
            row = db.execute("SELECT body FROM semantic_jobs WHERE id=?", (identifier,)).fetchone()
            if row:
                import_job(db, json.loads(row[0]), mode="shadow")
            db.execute("INSERT OR IGNORE INTO memory_import_jobs VALUES(?)", (identifier,))
    with repo.connect(write=True) as db:
        stale = db.execute("SELECT DISTINCT evaluation_id FROM memory_sources WHERE available=1").fetchall()
        for (identifier,) in stale:
            if not present or not db.execute("SELECT 1 FROM semantic_jobs WHERE id=?", (identifier,)).fetchone():
                repo.invalidate(db, evaluation_id=identifier)
        from loom.memory.dependencies import migrate_skills

        migrate_skills(db)
        db.execute("UPDATE memory_namespaces SET mode='runtime_v2',checkpoint='complete'")
        db.execute("INSERT INTO memory_import_state VALUES('complete')")
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
    args = parser.parse_args()
    try:
        result = migrate_database(args.database, backup=args.backup)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
