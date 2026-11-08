"""Agent-scoped knowledge detail and mutation endpoints."""

from loom.memory.operations import MemoryOperations
from loom.memory.query import MemoryQueryService
from loom.memory.repository import namespace_id
from loom.service.contracts import ServiceError


def dispatch(service, aid, method, route, query, body):
    agent = service.store.snapshot(aid, fields=set())
    workspace = agent["role"]["working_directory"]
    session_id = query.get("session_id", [body.get("session_id", "")])[0]
    if not workspace and session_id not in {s["session_id"] for s in agent["sessions"]}:
        raise ServiceError("Select an owned Session for memory without workspace")
    reader = MemoryQueryService(service.store.sessions.path, workspace=workspace, session_id=session_id)
    ns = namespace_id(workspace, session_id)
    try:
        if method == "GET" and len(route) == 3 and route[0] == "items" and route[2] == "history":
            return reader.history(route[1], valid_at=query.get("valid_at", [None])[0], known_at=query.get("known_at", [None])[0])
        if method == "GET" and len(route) == 2 and route[0] == "items":
            return reader.detail(route[1])
        if method == "GET" and route == ["status"]:
            with reader.repo.connect() as db:
                state = reader.state(db)
                return dict(
                    namespace=dict(state) if state else None,
                    pending=db.execute("SELECT COUNT(*) FROM memory_jobs WHERE namespace_id=? AND state='pending'", (ns,)).fetchone()[0],
                )
        if method == "GET" and route == ["proposals"]:
            import json

            with reader.repo.connect() as db:
                return dict(
                    proposals=[
                        {**dict(r), "input": json.loads(r["input"]), "result": json.loads(r["result"]) if r["result"] else None}
                        for r in db.execute("SELECT * FROM memory_jobs WHERE namespace_id=? ORDER BY rowid DESC LIMIT 50", (ns,))
                    ]
                )
        if method == "POST" and route == ["proposals"]:
            from loom.memory.consolidation import Consolidator

            return Consolidator(service.store.sessions.path).accept(ns, body["job_id"], body["candidate_version_id"], command_id=body["command_id"])
        if method == "POST" and route == ["commands"]:
            operation = body.get("operation")
            if not isinstance(body.get("command_id"), str) or not body["command_id"] or not isinstance(body.get("payload"), dict):
                raise ValueError("command_id and payload are required")
            commands = MemoryOperations(service.store.sessions.path)
            if operation == "UNDO_MERGE":
                return commands.undo_merge(ns, body["payload"]["event_id"], command_id=body["command_id"])
            return commands.execute(ns, operation, body["payload"], command_id=body["command_id"])
    except (ValueError, KeyError) as exc:
        code = getattr(exc, "code", "")
        raise ServiceError(str(exc), 409 if "conflict" in code else 404 if "not_found" in code else 400) from exc
    raise ServiceError("Unknown memory route", 404)
