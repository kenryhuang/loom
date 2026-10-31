"""Human annotations preserve extraction evidence and suppress repeated lineage."""

from loom.autonomy.contracts import strict
from loom.autonomy.store import digest
from loom.memory.store import MemoryStore
from loom.service.contracts import ServiceError, canonical, new_id, text

KINDS = {"confirm", "reject", "correct", "scope", "tag", "pin", "retire", "restore"}


def annotate(store, aid, cid, payload):
    strict(payload, {"evaluation_id", "record_id", "content_digest", "kind", "value", "reason", "expected_revision"})
    request = dict(type="annotation", agent_id=aid, payload=payload)
    with store.sessions.transaction() as db:
        replay = store.replay(db, cid, request)
        if replay:
            return replay
    agent = store.snapshot(aid)
    # Require actual Agent ownership without widening workspace memory scope.
    with store.sessions.transaction() as db:
        row = db.execute("SELECT session_id FROM semantic_jobs WHERE id=?", (payload.get("evaluation_id"),)).fetchone()
        if not row:
            raise ServiceError("Memory evaluation not found", 404)
        sid = row[0]
        if (
            agent["role"]["working_directory"] is None
            and not db.execute("SELECT 1 FROM agent_sessions WHERE agent_id=? AND session_id=?", (aid, sid)).fetchone()
        ):
            raise ServiceError("Memory outside Agent scope", 404)
    memory = MemoryStore(store.sessions.path, workspace=agent["role"]["working_directory"], session_id=sid)
    rows, _, issues = memory._records((payload.get("evaluation_id"), payload.get("record_id")), apply_annotations=False)
    if not rows or issues:
        raise ServiceError("Memory source unavailable or invalid", 404)
    source, record = rows[0]["source"], rows[0]["record"]
    if source["content_digest"] != payload.get("content_digest"):
        raise ServiceError("Memory content changed; reload before annotating", 409)
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ServiceError("Unknown annotation kind")
    if kind in {"correct", "scope", "tag"}:
        text(payload.get("value"), "annotation value", max_length=4000)
    with store.sessions.transaction() as db:
        replay = store.replay(db, cid, request)
        if replay:
            return replay
        prior = db.execute(
            "SELECT COALESCE(MAX(revision),0) FROM memory_annotations WHERE agent_id=? AND evaluation_id=? AND record_id=?",
            (aid, source["evaluation_id"], source["record_id"]),
        ).fetchone()[0]
        if payload.get("expected_revision") != prior:
            raise ServiceError(f"Annotation revision conflict; current revision is {prior}", 409)
        lineage = digest([source["source_digest"], record["kind"], record["title"], record["summary"], record.get("citations")])
        annotation = dict(
            id=new_id("annotation"),
            agent_id=aid,
            revision=prior + 1,
            kind=kind,
            actor="human",
            value=payload.get("value"),
            reason=payload.get("reason", ""),
            source=source,
            lineage=lineage,
            created_at=store.now(),
            scope="workspace" if agent["role"]["working_directory"] else "session",
        )
        db.execute(
            "INSERT INTO memory_annotations VALUES(?,?,?,?,?,?,?)",
            (annotation["id"], aid, source["evaluation_id"], source["record_id"], source["content_digest"], prior + 1, canonical(annotation)),
        )
        if kind in {"reject", "retire"}:
            db.execute("INSERT OR REPLACE INTO memory_suppressions VALUES(?,?,?)", (aid, lineage, canonical(annotation)))
        if kind == "restore":
            db.execute("DELETE FROM memory_suppressions WHERE agent_id=? AND lineage=?", (aid, lineage))
        db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")
        store.event(db, aid, "memory.annotated", annotation)
        return store.receipt(db, cid, request, annotation)


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
            applied.append(annotation["id"])
        if not hidden:
            source["annotation_ids"] = applied
            source["annotation_revision"] = max((a["revision"] for a in annotations if a["id"] in applied), default=0)
            result.append(dict(record=record, source=source))
    return result
