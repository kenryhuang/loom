"""Versioned role guidance and scoped references at legal request boundaries."""

import json
from dataclasses import replace

from loom.autonomy.store import digest
from loom.memory.store import MemoryError, MemoryStore


def build_snapshot(store, aid, *, goal=None, binding=None, session_id="", run_id="", purpose="execution"):
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        role = agent["role"]
        if binding:
            work = store.get(db, "agent_work_packages", binding["work_package_id"], aid)
            goal = store.get(db, "agent_goals", binding["goal_id"], aid)
            # Environment stays pinned even when Role changes mid-run.
            pinned = store.get(db, "agent_roles", f"{aid}:{binding['role_revision']}", aid)["role"]
        else:
            work, pinned = None, role
        entries = store.rows(db, "agent_guidance", aid)
        entries = [
            e
            for e in entries
            if e["state"] == "active"
            and (e["scope"] == "persistent" or work and e.get("work_package_id") == work["id"])
            and (e.get("effective_run_id") is None or e.get("effective_run_id") == run_id)
            and (run_id not in e.get("excluded_run_ids", []))
        ]
        methods = []
        for entry in entries:
            if entry.get("method_version_id"):
                row = db.execute("SELECT body,state FROM skill_versions WHERE id=?", (entry["method_version_id"],)).fetchone()
                if row and row["state"] == "user_guidance":
                    methods.append(dict(id=entry["method_version_id"], text=json.loads(row["body"])["method_text"], evidence_state="human_asserted"))
        cycles = store.rows(db, "agent_learning_cycles", aid)
        recent_work = [
            dict(id=w["id"], objective=w["objective"], session_id=w.get("session_id"), outcome=w["outcome"])
            for w in store.rows(db, "agent_work_packages", aid)
            if w.get("outcome")
        ][-4:]
    query = (work or goal or {}).get("objective", role["role_text"])
    # Planner with no workspace may use delivery summaries, never aggregate Session memory.
    memory = {"records": [], "issues": []}
    if pinned["working_directory"] or session_id:
        try:
            memory = MemoryStore(store.sessions.path, workspace=pinned["working_directory"], session_id=session_id).search(query[:20000])
        except MemoryError as exc:
            memory["issues"].append({"message": str(exc), "code": exc.code})
    snapshot = dict(
        agent_id=aid,
        session_id=session_id,
        run_id=run_id,
        purpose=purpose,
        role_revision=agent["role_revision"],
        pinned_role_revision=binding["role_revision"] if binding else agent["role_revision"],
        guidance_version=agent["guidance_version"],
        workspace=pinned["working_directory"],
        role_text=role["role_text"],
        long_term_objectives=role["long_term_objectives"],
        source_routes=pinned["source_routes"],
        role_methods=role["methods"],
        goal=goal,
        work_package_id=work["id"] if work else None,
        guidance=[dict(id=e["id"], text=e["text"], source="human", scope=e["scope"]) for e in entries],
        methods=methods,
        memory=memory,
        prior_deliveries=recent_work,
        pending_learning_cycle_ids=[c["id"] for c in cycles if c["state"] not in {"completed", "cancelled"}],
    )
    # Never silently truncate explicit obligations. Excess guidance must be narrowed by the user.
    if len(json.dumps(snapshot, ensure_ascii=False)) > 24000:
        from loom.service.contracts import ServiceError

        raise ServiceError("Agent context exceeds 24000 characters; narrow explicit guidance or split the task", 409)
    identifier = "context_" + digest(snapshot)[:32]
    snapshot["id"] = identifier
    with store.sessions.transaction() as db:
        ref = store.artifact(db, aid, snapshot)
        store.put(
            db,
            "agent_contexts",
            dict(
                id=identifier,
                agent_id=aid,
                artifact_ref=ref,
                session_id=session_id,
                run_id=run_id,
                purpose=purpose,
                guidance_ids=[entry["id"] for entry in entries],
            ),
        )
    return snapshot


class RuntimeAgentContext:
    def __init__(self, execution):
        self.execution = execution
        self.last_id = None

    def refs(self):
        from loom.core import ToolRef

        return (
            ToolRef(
                "agent_record_use",
                "Record which historical memory or user method informed completed tool calls. "
                "This records your interpretation, not proof of causal improvement.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "evaluation_id": {"type": "string"},
                        "record_id": {"type": "string"},
                        "method_version_id": {"type": "string"},
                        "explanation": {"type": "string"},
                        "tool_call_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
                    },
                    "required": ["tool_call_ids", "explanation"],
                    "additionalProperties": False,
                },
            ),
        )

    def handlers(self):
        from loom.core import Observation, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json

        async def record(value, _options=None):
            try:
                value = self.execution.rpc("agent_record_use", thaw_json(value))
                return ok(Observation(new_trace_id(), "agent_record_use", value, now_iso()))
            except (ValueError, RuntimeError) as exc:
                return err(make_loom_error("agent.use_invalid", str(exc)))

        return {"agent_record_use": record}

    async def attach(self, context, runtime):
        snapshot = self.execution.rpc("agent_context", {})
        if snapshot["id"] != self.last_id:
            self.last_id = snapshot["id"]
            await runtime.trace_sink.emit(
                dict(type="context.updated", snapshot_id=snapshot["id"], run_id=context.run_id, guidance_ids=[g["id"] for g in snapshot["guidance"]])
            )
        metadata = dict(context.metadata or {})
        metadata["agent_context"] = snapshot
        return replace(context, metadata=metadata)


def record_use(store, binding, run_id, payload):
    from loom.autonomy.contracts import strict, strings
    from loom.service.contracts import ServiceError, canonical

    strict(payload, {"evaluation_id", "record_id", "method_version_id", "tool_call_ids", "explanation"})
    ids = strings(payload.get("tool_call_ids", []), "tool_call_ids", 20)
    if not ids:
        raise ServiceError("Link at least one completed tool call")
    aid = binding["agent_id"]
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        link = db.execute("SELECT session_id FROM agent_run_links WHERE run_id=? AND agent_id=?", (run_id, aid)).fetchone()
        if not link:
            raise ServiceError("Unknown Agent run", 404)
        if payload.get("method_version_id"):
            if not any(
                g.get("method_version_id") == payload["method_version_id"]
                and g["state"] == "active"
                and (g["scope"] == "persistent" or g.get("work_package_id") == binding["work_package_id"])
                and run_id not in g.get("excluded_run_ids", [])
                for g in store.rows(db, "agent_guidance", aid)
            ):
                raise ServiceError("Method is not active guidance for this Agent")
        elif not payload.get("evaluation_id") or not payload.get("record_id"):
            raise ServiceError("Provide memory reference or method_version_id")
        for identifier in ids:
            row = db.execute("SELECT body FROM operations WHERE id=? AND session_id=?", (f"{run_id}:{identifier}", link[0])).fetchone()
            if not row or json.loads(row[0])["status"] not in {"completed", "reconciled"}:
                raise ServiceError("Use real completed calls from this run")
    if not payload.get("method_version_id"):
        MemoryStore(store.sessions.path, workspace=agent["role"]["working_directory"], session_id=link[0]).read(payload["evaluation_id"], payload["record_id"])
    usage = dict(
        kind="model_reported_use",
        run_id=run_id,
        work_package_id=binding["work_package_id"],
        references=payload,
        action_refs_validated=True,
        semantic_adoption="model_reported",
        causal_effect="unknown",
    )
    identifier = "use_" + digest(usage)
    with store.sessions.transaction() as db:
        db.execute("INSERT OR IGNORE INTO agent_memory_uses VALUES(?,?,?,?)", (identifier, aid, run_id, canonical(usage)))
    return dict(id=identifier, **usage)
