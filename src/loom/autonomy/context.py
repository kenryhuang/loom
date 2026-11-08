"""Versioned role guidance and scoped references at legal request boundaries."""

import json
from dataclasses import replace

from loom.autonomy.store import digest
from loom.memory.store import MemoryError, MemoryStore

CONTEXT_MAX_CHARS = 96000


def _source_summary(rows):
    result = []
    for row in rows:
        item = {k: v for k, v in row.items() if k != "work_states"}
        states = row.get("work_states")
        if isinstance(states, dict):
            counts = {}
            for state in states.values():
                counts[state] = counts.get(state, 0) + 1
            item["work_state_counts"] = counts
        result.append(item)
    return result


def _stage_contract(stage, goal):
    if not stage:
        return None
    contract = {k: stage[k] for k in ('title', 'expected_outcome', 'scope', 'non_goals', 'acceptance', 'stop_conditions')}
    # Remove only exact duplicates; distinct stage obligations remain intact.
    for field in ("acceptance", "stop_conditions"):
        goal_values = (goal or {}).get(field) or []
        repeated = [value for value in contract[field] if value in goal_values]
        if repeated:
            contract[field] = [value for value in contract[field] if value not in goal_values]
            contract[field + "_also_in_goal"] = len(repeated)
    return contract


def _goal_summary(goal):
    if not isinstance(goal, dict):
        return goal
    result = dict(goal)
    criteria, acceptance = goal.get("acceptance_criteria"), goal.get("acceptance")
    if isinstance(criteria, list) and isinstance(acceptance, list):
        rows = []
        indexed = False
        for row in criteria:
            row = dict(row)
            if row.get("text") in acceptance:
                row["acceptance_index"] = acceptance.index(row.pop("text"))
                indexed = True
            rows.append(row)
        result["acceptance_criteria"] = rows
        if indexed:
            result["acceptance_index_note"] = "acceptance_index is a zero-based index into goal.acceptance"
    return result


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
        from loom.autonomy.profile import effective
        from loom.autonomy.sources import view as source_view

        preferences = effective(store, db, agent, work_id=work['id'] if work else None)
        source_status = _source_summary(source_view(store, db, agent))
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
        # Content-parsed (JSON) tool calling hides call ids from the model; the
        # snapshot must re-expose them or agent_record_use can never link.
        recent_calls = []
        if session_id and run_id:
            prefix = run_id + ":"
            rows = db.execute(
                "SELECT body FROM operations WHERE session_id=? AND substr(id,1,?)=? "
                "AND json_extract(body,'$.status') IN ('completed','reconciled') "
                "ORDER BY rowid DESC LIMIT 20",
                (session_id, len(prefix), prefix),
            ).fetchall()
            for (body,) in reversed(rows):
                op = json.loads(body)
                recent_calls.append(dict(id=op["call"]["id"], name=op["call"]["name"]))
        stage = store.get(db, 'agent_stages', goal['stage_id'], aid) if goal and goal.get('stage_id') else None
    query = (work or goal or {}).get("objective", role["role_text"])
    # Planner with no workspace may use delivery summaries, never aggregate Session memory.
    memory = {"records": [], "issues": []}
    if pinned["working_directory"] or session_id:
        try:
            memory = MemoryStore(store.sessions.path, workspace=pinned["working_directory"], session_id=session_id).search(query[:20000])
        except MemoryError as exc:
            memory["issues"].append({"message": str(exc), "code": exc.code})
    from loom.memory.runtime import compact_memory

    memory = compact_memory(memory)
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
        token_policy=dict(mode='daily', daily_tokens=role['budget']['daily_tokens'], timezone=role['timezone'],
                          cumulative_limits=False, learning_subquota=False),
        long_term_objectives=role["long_term_objectives"],
        source_routes=pinned["source_routes"],
        source_status=source_status,
        preferences=[{k: p[k] for k in ('id', 'key', 'text', 'scope', 'revision', 'state')} for p in preferences],
        role_methods=role["methods"],
        decision_policy=("When a material execution choice depends on missing human preferences or conflicting requirements, "
                         "use request_input with a concrete question, alternatives and their tradeoffs. "
                         "Proceed autonomously on routine implementation choices. Retrospective analysis alternatives are suggestions, "
                         "not pending human decisions. Human answers guide the current task; only explicit persistent guidance applies across tasks."),
        goal=_goal_summary(goal),
        stage_id=(goal or {}).get('stage_id'),
        roadmap_id=(goal or {}).get('roadmap_id'),
        stage_contract=_stage_contract(stage, goal),
        work_package_id=work["id"] if work else None,
        compiled_inputs=(binding or {}).get("input_bindings", []),
        guidance=[dict(id=e["id"], text=e["text"], source="human", scope=e["scope"]) for e in entries],
        methods=methods,
        memory=memory,
        prior_deliveries=recent_work,
        pending_learning_cycle_ids=[c["id"] for c in cycles if c["state"] not in {"completed", "cancelled", "failed", "incomplete"}],
    )
    # Never silently truncate explicit obligations. Excess guidance must be narrowed by the user.
    if len(json.dumps(snapshot, ensure_ascii=False)) > CONTEXT_MAX_CHARS:
        from loom.service.contracts import ServiceError

        raise ServiceError(f"Agent context exceeds {CONTEXT_MAX_CHARS} characters; narrow explicit guidance or split the task", 409)
    # Auto-generated and bounded (≤20 recent calls): rides above the explicit-
    # obligation cap so it can never break a borderline snapshot. Still part of
    # the digested artifact — the recorded context is exactly what the model saw.
    snapshot["recent_tool_calls"] = recent_calls
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
                memory_refs=[{k: r.get('source', r).get(k)
                              for k in ('record_id', 'evaluation_id', 'content_digest', 'memory_id', 'version_id') if k in r.get('source', r)}
                             for r in memory.get('records', [])],
                completed_learning_ids=[c['id'] for c in cycles if c['state'] == 'completed' and c.get('coverage') == 'full'],
                created_at=store.now(),
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
                "Record which historical memory or user method informed completed tool calls; "
                "copy their ids from recent_tool_calls in the agent_context message. "
                "This records your interpretation, not proof of causal improvement.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "evaluation_id": {"type": "string"},
                        "record_id": {"type": "string"},
                        "memory_id": {"type": "string"},
                        "version_id": {"type": "string"},
                        "method_version_id": {"type": "string"},
                        "explanation": {"type": "string"},
                        "disposition": {"enum": ["adopted", "declined"]},
                        "tool_call_ids": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 20},
                    },
                    "required": ["tool_call_ids", "explanation"],
                    "additionalProperties": False,
                },
            ),
        )

    def handlers(self):
        from loom.core import Observation, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json

        async def record(value, _options=None):
            payload = thaw_json(value)
            if isinstance(payload, dict) and payload.get("disposition", "adopted") != "declined" and not payload.get("tool_call_ids"):
                # Intercept before the RPC: in JSON mode the model cannot see
                # call ids, so an empty link is answered with a pointer, not a
                # service round trip that only echoes the same rejection.
                return err(make_loom_error(
                    "agent.use_invalid",
                    "tool_call_ids must link at least one completed tool call from this run; "
                    "copy ids from recent_tool_calls in the agent_context message",
                    retryable=False,
                ))
            try:
                value = self.execution.rpc("agent_record_use", payload)
                return ok(Observation(new_trace_id(), "agent_record_use", value, now_iso()))
            except (ValueError, RuntimeError) as exc:
                return err(make_loom_error("agent.use_invalid", str(exc), retryable=False))

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

    strict(payload, {"evaluation_id", "record_id", "method_version_id", "tool_call_ids", "explanation", "disposition", "memory_id", "version_id"})
    if payload.get('disposition', 'adopted') not in {'adopted', 'declined'}:
        raise ServiceError('Invalid adoption disposition')
    ids = strings(payload.get("tool_call_ids", []), "tool_call_ids", 20)
    if not ids and payload.get('disposition') != 'declined':
        raise ServiceError("Link at least one completed tool call from this run; "
                           "ids are listed in recent_tool_calls of the agent_context message")
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
        elif not (payload.get("memory_id") and payload.get("version_id")) and (not payload.get("evaluation_id") or not payload.get("record_id")):
            raise ServiceError("Provide memory reference or method_version_id")
        for identifier in ids:
            row = db.execute("SELECT body FROM operations WHERE id=? AND session_id=?", (f"{run_id}:{identifier}", link[0])).fetchone()
            if not row or json.loads(row[0])["status"] not in {"completed", "reconciled"}:
                raise ServiceError("Use real completed calls from this run")
    if not payload.get("method_version_id"):
        memory = MemoryStore(store.sessions.path, workspace=agent["role"]["working_directory"], session_id=link[0])
        if payload.get('memory_id'):
            memory.read(memory_id=payload['memory_id'], version_id=payload.get('version_id'))
        else:
            memory.read(payload["evaluation_id"], payload["record_id"])
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
