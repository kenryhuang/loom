"""Human annotations preserve extraction evidence and suppress repeated lineage."""

import json

from loom.autonomy.contracts import strict
from loom.autonomy.store import digest
from loom.memory.store import MemoryError, MemoryStore
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
    try:
        original = memory.query_service().original(payload.get("evaluation_id"), payload.get("record_id"), limit=64000)
    except MemoryError as exc:
        raise ServiceError("Memory source unavailable or invalid", 404) from exc
    if not original["source_available"]:
        raise ServiceError("Memory source unavailable or invalid", 404)
    source, record = original["source"], json.loads(original["text"])
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
        apply_canonical(db, annotation, store.sessions.path)
        store.event(db, aid, "memory.annotated", annotation)
        return store.receipt(db, cid, request, annotation)


def apply_canonical(db, annotation, database):
    """Source-addressed commands share the canonical transaction and denial boundary."""

    from loom.memory.operations import MemoryOperations

    source = annotation['source']
    rows = db.execute('''SELECT DISTINCT i.id,i.namespace_id,i.latest_revision FROM memory_sources src
        JOIN memory_observations o ON o.source_id=src.id JOIN memory_supports s ON s.observation_id=o.id
        JOIN memory_item_versions v ON v.id=s.version_id JOIN memory_items i ON i.id=v.memory_id
        WHERE src.evaluation_id=? AND src.record_id=? AND src.revision=? AND i.canonical_redirect IS NULL''',
        (source['evaluation_id'], source['record_id'], source['content_digest'])).fetchall()
    for row in rows:
        kind = annotation['kind']
        operation = {'confirm': 'CONFIRM', 'reject': 'RETIRE', 'retire': 'RETIRE', 'restore': 'RESTORE',
                     'correct': 'REVISE', 'scope': 'REVISE', 'pin': 'PIN', 'tag': 'TAG'}[kind]
        payload = dict(memory_id=row['id'], expected_revision=row['latest_revision'], reason=annotation.get('reason', ''),
                       value=annotation.get('value'), agent_id=annotation['agent_id'])
        if kind in {'correct', 'scope'}:
            record = json.loads(db.execute('SELECT record FROM memory_item_versions WHERE memory_id=? ORDER BY revision DESC LIMIT 1',
                                           (row['id'],)).fetchone()[0])
            if kind == 'correct':
                record['summary'] = annotation['value']
                record.pop('claim', None)  # Free text cannot preserve a now-contradictory structured value.
            else:
                record['scope'] += '; Human limitation: ' + annotation['value']
                if record.get('claim'):
                    record['claim'] = {**record['claim'], 'conditions': {
                        **record['claim'].get('conditions', {}), 'human_scope_requires_validation': annotation['value']}}
                record['runtime_blocked'] = True
            payload['record'] = record
        MemoryOperations(database).execute(row['namespace_id'], operation, payload,
            command_id='legacy:' + annotation['id'] + ':' + row['id'], _db=db)
