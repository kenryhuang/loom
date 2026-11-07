"""Authenticated Agent facade; model work is owned by the background supervisor."""

import json

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.contracts import check_dependencies, goal_contract, strict
from loom.autonomy.store import AgentStore
from loom.autonomy.supervisor import AgentSupervisor
from loom.service.contracts import ServiceError, new_id, text


class AutonomyService:
    def __init__(self, service, background):
        self.service = service
        self.store = AgentStore(service.store)
        self.background = background
        self.supervisor = AgentSupervisor(service, self.store, background, planner=service.agent_planner)

    def start(self):
        if self.service.autonomy_enabled:
            from loom.autonomy.control import drain

            with self.store.sessions.transaction() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM agents")]
            for aid in ids:
                drain(self.service, self.store, aid)
            self.supervisor.start()

    def close(self):
        self.supervisor.close()

    def snapshot(self, aid, *, view=None):
        from loom.autonomy.views import VIEW_FIELDS

        result = self.store.snapshot(aid, fields=VIEW_FIELDS[view] if view else None, summaries=view == 'overview')
        if view:
            result['view'] = view
        result["service_enabled"] = self.service.autonomy_enabled
        result["service_error"] = self.supervisor.last_error
        from loom.autonomy.configuration import preview

        result['configuration'] = dict(role=preview(result), pending=bool(result.get('pending_configuration')),
                                       effective_role_revision=result['role_revision'])
        result['budget'] = []
        if view in {None, 'overview'}:
            result["budget"] = BudgetLedger(self.store.sessions.path).summary(aid)
            result['daily_budget'] = BudgetLedger(self.store.sessions.path, clock=self.store.clock).daily_summary(aid)
            result["objective_budget"] = BudgetLedger(self.store.sessions.path).objective_summary(aid)
            result["learning_budget"] = BudgetLedger(self.store.sessions.path, clock=self.store.clock).learning_summary(aid)
        result["pause_pending"] = result["state"] in {"paused", "archived"} and any(s["session_id"] in self.service.active for s in result["sessions"])
        result["pending_decisions"] = []
        with self.store.sessions.read() as db:
            from loom.autonomy.profile import effective
            from loom.autonomy.sources import view as source_view

            if view == 'goals':
                from loom.autonomy.views import add_work_metrics

                add_work_metrics(db, aid, result['work_packages'])
            result['preferences'] = effective(self.store, db, result) if view in {None, 'profile', 'learning'} else []
            owner = result['role'].get('owner_id', 'local')
            result['profile_entries'] = [json.loads(r[0]) for r in db.execute('SELECT body FROM agent_profile')
                if json.loads(r[0])['agent_id'] == aid or (json.loads(r[0])['scope'] == 'owner' and json.loads(r[0])['owner_id'] == owner)
                ] if view in {None, 'profile', 'learning'} else []
            result['source_status'] = source_view(self.store, db, result) if view in {None, 'work', 'knowledge'} else []
            result['shared_roles'] = [dict(id=a['id'], name=a['role']['name']) for r in db.execute('SELECT body FROM agents')
                if (a := json.loads(r[0]))['role'].get('owner_id', 'local') == owner]
            rows = db.execute(
                "SELECT a.session_id,a.work_package_id,a.taken_over,json_extract(s.body,'$.input_request') AS request,"
                "json_extract(s.body,'$.task.revision') AS task_revision,json_extract(s.body,'$.task.objective') AS objective "
                "FROM agent_sessions a JOIN sessions s ON s.id=a.session_id WHERE a.agent_id=? "
                "AND json_extract(s.body,'$.deleted_at') IS NULL AND json_extract(s.body,'$.input_request.state')='pending'", (aid,),
            )
            result["pending_decisions"] = [{**dict(row), "request": json.loads(row["request"]), "taken_over": bool(row["taken_over"])} for row in rows]
            from loom.autonomy.execution import report

            if view in {None, 'work'}:
                result['execution'] = report(self.store, self.service, db, result, analysis_details=False)
        return result

    def control(self, aid, body):
        strict(body, {"command_id", "type", "expected_revision", "payload"})
        with self.service.lock:
            result = self.store.command(aid, body.get("command_id"), body.get("type"), body.get("expected_revision"), body.get("payload"))
            from loom.autonomy.control import drain

            drain(self.service, self.store, aid)
        return result

    def guidance(self, aid, body, *, method=False):
        strict(body, {"command_id", "text", "scope", "work_package_id", "effective", "title", "method_text", "document_path"})
        cid = body.get("command_id")
        request = dict(agent_id=aid, method=method, **body)
        with self.store.sessions.transaction() as db:
            replay = self.store.replay(db, cid, request)
            if replay:
                return replay
        content = body.get("method_text") if method else body.get("text")
        source_document = None
        if method and body.get("document_path"):
            from pathlib import Path

            from loom.autonomy.store import digest

            agent = self.store.snapshot(aid)
            directory = agent["role"]["working_directory"]
            if not directory:
                raise ServiceError("Document import requires a Role working directory")
            path = (Path(directory) / text(body["document_path"], "document_path")).resolve()
            if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > 32000:
                raise ServiceError("Method document must be a UTF-8 file of at most 32 KB inside working_directory")
            content = path.read_text(encoding="utf-8")
            source_document = dict(path=str(path), content_digest=digest(content))
        content = text(content, "guidance", max_length=8000)
        scope = body.get("scope", "current_task")
        effective = body.get("effective", "next_boundary")
        if scope not in {"current_task", "persistent"} or effective not in {"next_boundary", "next_run"}:
            raise ServiceError("Invalid guidance scope or effective boundary")
        with self.store.sessions.transaction() as db:
            replay = self.store.replay(db, cid, request)
            if replay:
                return replay
            agent = self.store.get(db, "agents", aid)
            work = self.store.get(db, "agent_work_packages", body.get("work_package_id"), aid) if body.get("work_package_id") else None
            if scope == "current_task" and work is None:
                raise ServiceError("Current-task guidance needs a work_package_id")
            item = dict(
                id=new_id("guidance"),
                agent_id=aid,
                text=content,
                scope=scope,
                state="active",
                revision=1,
                actor="human",
                created_at=self.store.now(),
                effective=effective,
                work_package_id=work["id"] if work else None,
                applied_run_ids=[],
                source_document=source_document,
            )
            if effective == "next_run":
                active_ids = [
                    self.service.store._load(db, r[0]).get("run") for r in db.execute("SELECT session_id FROM agent_sessions WHERE agent_id=?", (aid,))
                ]
                item["excluded_run_ids"] = [r["id"] for r in active_ids if r and r["state"] not in {"completed", "failed", "stopped"}]
            if method:
                from loom.skills.imports import import_method

                version = import_method(db, aid, agent["role"]["working_directory"], content, body.get("title", "User method"))
                item["method_version_id"] = version["id"]
            self.store.put(db, "agent_guidance", item)
            agent.update(guidance_version=agent["guidance_version"] + 1, generation=agent["generation"] + 1)
            self.store.put(db, "agents", agent)
            self.store.event(db, aid, "guidance.received", {"guidance_id": item["id"], "effective": effective})
            return self.store.receipt(db, cid, request, item)

    def goal_command(self, aid, gid, body):
        strict(body, {"command_id", "type", "expected_revision", "payload"})
        request = dict(agent_id=aid, goal_id=gid, **body)
        stop_sessions = []
        with self.service.lock:
            with self.store.sessions.transaction() as db:
                replay = self.store.replay(db, body.get("command_id"), request)
                if replay:
                    return replay
                agent = self.store.get(db, "agents", aid)
                goal = self.store.get(db, "agent_goals", gid, aid)
                self.store.expect(goal, body.get("expected_revision"))
                kind = body.get("type")
                if goal["state"] in {"completed", "cancelled", "superseded"}:
                    raise ServiceError("Goal is terminal; create a successor", 409)
                if kind not in {"pause", "resume", "cancel", "revise"}:
                    raise ServiceError("Unknown Goal command")
                from loom.autonomy.stage_goals import changed, editable_stage

                if goal.get("stage_id") or kind == "revise":
                    stage, planning = editable_stage(self.store, db, agent, goal.get("stage_id"), goal=goal)
                works = [w for w in self.store.rows(db, "agent_work_packages", aid) if w["goal_id"] == gid]
                if kind == "revise":
                    if any(w["state"] in {"dispatched", "running", "blocked"} and w.get("session_id") for w in works):
                        raise ServiceError("Pause and finish or cancel current work before revising its contract", 409)
                    payload = strict(body.get("payload", {}), {"objective", "acceptance"})
                    contract = {
                        key: goal[key] for key in ("objective", "acceptance", "long_term_objective_id", "depends_on", "priority",
                                                  "stop_conditions", "learning_dependency", "deadline", "required_source_ids") if key in goal
                    }
                    goal.update(goal_contract({**contract, **payload}, agent["role"]))
                    from loom.autonomy.acceptance import inherit, revise

                    revise(goal)
                    if stage["goal_ids"][-1] == gid:
                        inherit(goal, stage["acceptance_criteria"])
                    changed(self.store, db, stage, planning)
                    goal.pop('source_changed', None)
                    for work in works:
                        work["superseded_contract"] = True
                        if work["state"] == "pending":
                            work["state"] = "cancelled"
                        self.store.put(db, "agent_work_packages", work)
                    goal["state"] = "queued"
                elif kind == "resume":
                    goal["state"] = "active" if works else "queued"
                else:
                    goal["state"] = "cancelled" if kind == "cancel" else "paused"
                goal["revision"] += 1
                check_dependencies([g for g in self.store.rows(db, "agent_goals", aid) if g["id"] != gid] + [goal])
                self.store.put(db, "agent_goals", goal)
                if kind in {"cancel", "pause", "resume"}:
                    for work in works:
                        if work["state"] == "pending" and kind == "cancel":
                            work["state"] = "cancelled"
                            self.store.put(db, "agent_work_packages", work)
                        elif work.get("session_id") and work["state"] in {"running", "dispatched", "blocked"}:
                            stop_sessions.append(work["session_id"])
                agent.update(generation=agent["generation"] + 1, next_wake_at=self.store.now())
                self.store.put(db, "agents", agent)
                self.store.event(db, aid, "goal." + kind, {"goal_id": gid})
                result = self.store.receipt(db, body["command_id"], request, goal)
                from loom.autonomy.control import enqueue

                for sid in stop_sessions:
                    enqueue(db, aid, body["command_id"], sid, {"cancel": "stop_run", "pause": "pause", "resume": "resume"}[kind], gid)
            from loom.autonomy.control import drain

            drain(self.service, self.store, aid)
            return result

    def profile_command(self, aid, body):
        strict(body, {"command_id", "type", "entry_id", "expected_revision"})
        request = dict(agent_id=aid, **body)
        with self.store.sessions.transaction() as db:
            replay = self.store.replay(db, body.get("command_id"), request)
            if replay:
                return replay
            entry = self.store.get(db, "agent_guidance", body.get("entry_id"), aid)
            self.store.expect(entry, body.get("expected_revision"))
            if body.get("type") not in {"retire", "restore", "lock"}:
                raise ServiceError("Use retire, restore or lock")
            if body["type"] == "lock":
                entry["locked_by_user"] = True
            else:
                entry["state"] = "retired" if body["type"] == "retire" else "active"
            entry["revision"] += 1
            self.store.put(db, "agent_guidance", entry)
            agent = self.store.get(db, "agents", aid)
            agent.update(guidance_version=agent["guidance_version"] + 1, generation=agent["generation"] + 1)
            self.store.put(db, "agents", agent)
            self.store.event(db, aid, "guidance.changed", {"guidance_id": entry["id"], "state": entry["state"]})
            return self.store.receipt(db, body["command_id"], request, entry)

    def session_command(self, aid, sid, body):
        strict(body, {"command_id", "type", "expected_revision", "target_agent_id", "target_revision"})
        request = dict(agent_id=aid, session_id=sid, **body)
        with self.service.lock, self.store.sessions.transaction() as db:
            replay = self.store.replay(db, body.get("command_id"), request)
            if replay:
                return replay
            agent = self.store.get(db, "agents", aid)
            self.store.expect(agent, body.get("expected_revision"))
            state = self.service.store._load(db, sid)
            link = db.execute("SELECT * FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
            kind = body.get("type")
            if kind == 'transfer':
                from loom.memory.store import bound_workspace

                target = self.store.get(db, 'agents', body.get('target_agent_id'))
                self.store.expect(target, body.get('target_revision'))
                if (not link or link['agent_id'] != aid or link['work_package_id'] or sid in self.service.active
                        or state['task']['state'] != 'idle' or target['id'] == aid
                        or agent['role'].get('owner_id', 'local') != target['role'].get('owner_id', 'local')
                        or bound_workspace(state['task']) != target['role']['working_directory']
                        or state['task'].get('model') != target['role']['model']
                        or state['task'].get('knowledge_base_ids', []) != target['role']['knowledge_base_ids']
                        or state['task'].get('external_tool_ids', []) != target['role']['external_tool_ids']
                        or (state.get('input_request') or {}).get('state') == 'pending'
                        or any(m['role'] == 'user' and m['state'] == 'accepted' for m in state['messages'])):
                    raise ServiceError('Transfer requires an idle compatible unreserved Session and matching owner', 409)
                db.execute('UPDATE agent_sessions SET agent_id=?,taken_over=0 WHERE session_id=?', (target['id'], sid))
                state.pop('autonomy', None)
                self.service.store._save(db, state)
                target.update(generation=target['generation'] + 1, revision=target['revision'] + 1)
                self.store.put(db, 'agents', target)
                self.store.event(db, target['id'], 'session.transferred', dict(session_id=sid, from_agent_id=aid))
            elif kind == "bind":
                from loom.memory.store import bound_workspace

                if link or bound_workspace(state["task"]) != agent["role"]["working_directory"] or state["task"]["state"] != "idle":
                    raise ServiceError("Only compatible unowned idle Sessions can be bound", 409)
                db.execute("INSERT INTO agent_sessions VALUES(?,?,NULL,0)", (sid, aid))
            elif kind in {"takeover", "release"}:
                if not link or link["agent_id"] != aid:
                    raise ServiceError("Session outside Agent scope", 404)
                db.execute("UPDATE agent_sessions SET taken_over=? WHERE session_id=?", (kind == "takeover", sid))
            else:
                raise ServiceError("Unknown Session ownership command")
            agent.update(generation=agent["generation"] + 1, revision=agent["revision"] + 1)
            self.store.put(db, "agents", agent)
            self.store.event(db, aid, "session." + kind, {"session_id": sid})
            return self.store.receipt(db, body["command_id"], request, {"session_id": sid, "state": kind, "revision": agent["revision"]})

    def learning_command(self, aid, lid, body):
        strict(body, {"command_id", "type", "limits"})
        request = dict(agent_id=aid, cycle_id=lid, **body)
        limits = strict(body.get("limits", {}), {"max_calls", "max_seconds", "max_tokens"})
        from loom.autonomy.contracts import integer
        from loom.service.semantic import BOUNDS

        for key, amount in limits.items():
            integer(amount, key, *BOUNDS[key])
        with self.store.sessions.transaction() as db:
            replay = self.store.replay(db, body.get("command_id"), request)
            if replay:
                return replay
            cycle = self.store.get(db, "agent_learning_cycles", lid, aid)
            action = body.get("type")
            if action not in {"retry", "cancel"}:
                raise ServiceError("Unknown learning command")
            if action == "retry" and cycle["state"] not in {"incomplete", "failed", "deferred", "completed", "cancelled"}:
                raise ServiceError("Learning is already active", 409)
            if action == "retry" and cycle["state"] == "completed" and cycle["coverage"] != "partial":
                raise ServiceError("Completed evaluation has no retryable gap", 409)
            if action == "retry" and cycle.get("evaluation_id") and db.execute(
                "SELECT 1 FROM semantic_jobs WHERE id=? AND json_extract(body,'$.state') IN ('queued','running')", (cycle["evaluation_id"],)
            ).fetchone():
                raise ServiceError("Evaluation is still running or cancelling; wait for it to stop", 409)
            cycle.update(requested_action=action, command_generation=cycle.get("command_generation", 0) + 1, retry_options=limits, retry_after="")
            if action == "retry":
                from loom.autonomy.learning_queue import next_order

                cycle.update(state="pending", queue_order=next_order(db, aid), enqueued_at=self.store.now(),
                             retry_round=cycle.get("retry_round", 0) + 1, round_start_attempts=cycle.get("attempts", 0),
                             control_failures=0, analysis_attempts=0, budget_waits=0, auto_resume=False,
                             resume_intent=None, budget_resume_pending=None, last_retry_progress=None, blocked_reason=None)
            if action == "cancel":
                cycle["state"] = "cancelled"
            agent = self.store.get(db, "agents", aid)
            agent["learning_schedule_requested"] = True
            self.store.put(db, "agents", agent)
            self.store.put(db, "agent_learning_cycles", cycle)
            self.store.event(db, aid, "learning.command", dict(cycle_id=lid, action=action))
            return self.store.receipt(db, body["command_id"], request, cycle)

    def recommend_agent_setup(self, body):
        """One recommend call for the agent creation form. Writes nothing."""
        from loom.autonomy.binding import spec_from_proposal
        from loom.service.session_setup import recommend_setup

        value = strict(body, {"description", "model"})
        description = text(value.get("description"), "description", max_length=20000)
        model = value.get("model") or None
        if model is not None:
            text(model, "model", max_length=200)
        try:
            recommender = getattr(self.service, "setup_recommender", None)
            proposal = (
                recommender(description, model=model, scope="agent_init")
                if recommender
                else recommend_setup(self.service, description, model=model, scope="agent_init")
            )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("Setup model request failed; retry or configure manually", 502) from exc
        return dict(
            task_spec=spec_from_proposal(proposal),
            plan_mode=proposal.get("plan_mode") or "auto",
            knowledge_base_ids=proposal.get("knowledge_base_ids", []),
            external_tool_ids=proposal.get("external_tools", []),
            rationale=proposal.get("rationale", ""),
            collection_reasons=proposal.get("collection_reasons", {}),
        )

    def dispatch(self, method, parts, query, body=None):
        from urllib.parse import unquote

        if method not in {"GET", "POST"}:
            raise ServiceError("Agent routes support GET and POST", 405)
        parts = [unquote(p) for p in parts]
        body = body or {}
        if not parts:
            if method == "POST":
                strict(body, {"command_id", "role"})
                return self.store.create(body.get("command_id"), body.get("role"))
            with self.store.sessions.read() as db:
                return {"agents": [json.loads(r[0]) for r in db.execute("SELECT body FROM agents")], "service_enabled": self.service.autonomy_enabled}
        if parts == ["setup", "recommend"]:
            if method != "POST":
                raise ServiceError("Setup recommendation supports POST", 405)
            return self.recommend_agent_setup(body)
        aid = parts[0]
        with self.store.sessions.read() as db:
            self.store.get(db, 'agents', aid)  # Identity check without loading every collection.
        if len(parts) == 1 and method == "GET":
            return self.snapshot(aid)
        route = parts[1:]
        if len(route) == 2 and route[0] == 'views' and method == 'GET':
            from loom.autonomy.views import VIEW_FIELDS

            if route[1] not in VIEW_FIELDS:
                raise ServiceError('Unknown Agent view', 404)
            return self.snapshot(aid, view=route[1])
        if len(route) == 3 and route[0] == 'learning' and route[2] == 'diagnostics' and method == 'GET':
            from loom.autonomy.execution import analysis_diagnostics

            with self.store.sessions.read() as db:
                cycle = self.store.get(db, 'agent_learning_cycles', route[1], aid)
                return analysis_diagnostics(db, cycle)
        if len(route) == 3 and route[0] == 'objectives' and route[2] == 'roadmap' and method == 'GET':
            from loom.autonomy.stages import planning_state

            with self.store.sessions.transaction() as db:
                agent = self.store.get(db, 'agents', aid)
                objective = next((o for o in agent['role']['long_term_objectives'] if o['id'] == route[1]), None)
                if objective is None:
                    raise ServiceError('Unknown long-term objective', 404)
                state = planning_state(self.store, db, agent, objective)
                return dict(planning=state, roadmap=self.store.get(db, 'agent_roadmaps', state['roadmap_id'], aid) if state.get('roadmap_id') else None)
        if len(route) == 4 and route[0] == 'objectives' and route[2:] == ['planning', 'commands'] and method == 'POST':
            from loom.autonomy.stage_commands import command

            return command(self, aid, route[1], body)
        if route == ['stages'] and method == 'GET':
            return dict(stages=self.store.snapshot(aid)['stages'])
        if len(route) == 2 and route[0] == 'stages' and method == 'GET':
            with self.store.sessions.transaction() as db:
                stage = self.store.get(db, 'agent_stages', route[1], aid)
                return dict(stage=stage, reviews=[r for r in self.store.rows(db, 'agent_stage_reviews', aid) if r['stage_id'] == stage['id']])
        if len(route) == 3 and route[0] == 'stages' and route[2] == 'commands' and method == 'POST':
            from loom.autonomy.stage_commands import command

            return command(self, aid, route[1], body, stage_command=True)
        if len(route) == 3 and route[0] == 'stages' and route[2] == 'goals' and method == 'POST':
            from loom.autonomy.stage_goals import add

            return add(self, aid, route[1], body)
        if route == ['habits'] and method == 'POST':
            from loom.autonomy.habits import configure

            return configure(self.store, aid, body)
        if route == ['methods', 'evolve'] and method == 'POST':
            from loom.autonomy.method_feedback import evolve

            return evolve(self, aid, body)
        if route == ['methods', 'promote'] and method == 'POST':
            from loom.skills.promotion import promote

            strict(body, {'version_id', 'revision', 'target_agent_id', 'conditions', 'environment'})
            source, target = self.store.snapshot(aid), self.store.snapshot(body.get('target_agent_id'))
            skill = self.background.skills.store.version(body.get('version_id'))
            if (source['role'].get('owner_id', 'local') != target['role'].get('owner_id', 'local')
                    or skill['workspace'] != source['role']['working_directory']):
                raise ServiceError('Method promotion outside owner/workspace scope', 404)
            return promote(self.background.skills.store, skill['id'], revision=body.get('revision'),
                           target_workspace=target['role']['working_directory'], conditions=body.get('conditions'), environment=body.get('environment', {}))
        if route == ['methods', 'feedback'] and method == 'GET':
            from loom.autonomy.method_feedback import report

            return report(self.store, aid)
        if route == ['observations'] and method == 'POST':
            from loom.autonomy.goals import observe

            strict(body, {'command_id', 'observation'})
            return observe(self.store, aid, body.get('command_id'), body.get('observation'))
        if route == ['external-events'] and method == 'POST':
            from loom.autonomy.events import ingest

            return ingest(self.store, aid, body)
        if route == ['sources', 'refresh'] and method == 'POST':
            from loom.autonomy.sources import refresh

            strict(body, set())
            return {'sources': refresh(self.store, aid, force=True)}
        if route == ['preferences', 'feedback'] and method == 'POST':
            from loom.autonomy.profile import feedback

            return feedback(self.store, aid, body)
        if route == ['preferences', 'commands'] and method == 'POST':
            from loom.autonomy.profile import command

            return command(self.store, aid, body)
        if len(route) == 3 and route[0] == 'goals' and route[2] == 'retry' and method == 'POST':
            from loom.autonomy.goals import retry

            return retry(self.store, aid, route[1], body, stage_only=True)
        if route == ["commands"] and method == "POST":
            return self.control(aid, body)
        if route == ["role", "revisions"] and method == "POST":
            strict(body, {"command_id", "expected_revision", "role"})
            return self.store.revise_role(aid, body.get("command_id"), body.get("expected_revision"), body.get("role"))
        if len(route) == 2 and route[0] == 'configuration' and method == 'POST':
            from loom.autonomy.configuration import revise

            strict(body, {'command_id', 'expected_revision', 'changes'})
            return revise(self.store, aid, body.get('command_id'), body.get('expected_revision'), route[1], body.get('changes'))
        if route == ["goals"] and method == "POST":
            raise ServiceError("Standalone Goal creation was removed; add Goals under a current unfinished stage", 409)
        if len(route) == 3 and route[0] == "goals" and route[2] == "commands" and method == "POST":
            return self.goal_command(aid, route[1], body)
        if len(route) == 3 and route[0] == "sessions" and route[2] == "commands" and method == "POST":
            return self.session_command(aid, route[1], body)
        if route == ["guidance"] and method == "POST":
            return self.guidance(aid, body)
        if route == ["profile", "commands"] and method == "POST":
            return self.profile_command(aid, body)
        if route == ["methods", "import"] and method == "POST":
            return self.guidance(aid, body, method=True)
        if route == ["memory", "annotations"] and method == "POST":
            from loom.memory.annotations import annotate

            return annotate(self.store, aid, body.get("command_id"), {k: v for k, v in body.items() if k != "command_id"})
        if route == ["learning", "schedule"] and method == "POST":
            strict(body, {"command_id"})
            request = dict(agent_id=aid, type="learning.schedule", **body)
            with self.store.sessions.transaction() as db:
                replay = self.store.replay(db, body.get("command_id"), request)
                if replay:
                    return replay
                agent = self.store.get(db, "agents", aid)
                agent["learning_schedule_requested"] = True
                self.store.put(db, "agents", agent)
                self.store.event(db, aid, "learning.schedule.requested", {})
                return self.store.receipt(db, body["command_id"], request, {"state": "requested"})
        if len(route) == 3 and route[0] == "learning" and route[2] == "commands" and method == "POST":
            return self.learning_command(aid, route[1], body)
        if method != "GET":
            raise ServiceError("Unknown Agent route", 404)
        if route in (["memory"], ["memory", "library"]):
            from loom.memory.store import MemoryStore

            agent = self.store.snapshot(aid)
            if not agent["role"]["working_directory"]:
                sid = query.get("session_id", [""])[0]
                if sid not in {s["session_id"] for s in agent["sessions"]}:
                    raise ServiceError("Select an owned Session for memory without workspace")
            else:
                sid = ""
            memory = MemoryStore(self.store.sessions.path, workspace=agent["role"]["working_directory"], session_id=sid)
            if route == ["memory", "library"]:
                try:
                    offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["20"])[0])
                except ValueError as exc:
                    raise ServiceError("Invalid memory library pagination") from exc
                result = memory.browse(query=query.get("query", [""])[0], kind=query.get("kind", [""])[0],
                                       review=query.get("review", [""])[0], offset=offset, limit=limit)
                # Review commands use this Agent's revision even when workspace annotations are shared.
                with self.store.sessions.transaction() as db:
                    for record in result["records"]:
                        source = record["source"]
                        source["annotation_revision"] = db.execute(
                            "SELECT COALESCE(MAX(revision),0) FROM memory_annotations WHERE agent_id=? AND evaluation_id=? AND record_id=?",
                            (aid, source["evaluation_id"], source["record_id"]),
                        ).fetchone()[0]
                return result
            return memory.search(query.get("query", [agent["role"]["role_text"]])[0])
        if route == ["profile"]:
            snapshot = self.snapshot(aid)
            return dict(role=snapshot["role"], entries=snapshot["guidance"])
        if route == ["sessions"]:
            return {"sessions": self.snapshot(aid)["sessions"]}
        if route == ["memory", "annotations"]:
            with self.store.sessions.transaction() as db:
                return {
                    "annotations": [
                        json.loads(r[0]) for r in db.execute("SELECT body FROM memory_annotations WHERE agent_id=? ORDER BY rowid DESC LIMIT 200", (aid,))
                    ]
                }
        if len(route) == 2 and route[0] == "artifacts":
            return self.store.read_artifact(aid, route[1])
        if route == ["events"]:
            after = int(query.get("after", ["0"])[0])
            if after < 0:
                raise ServiceError("Invalid event cursor")
            with self.store.sessions.transaction() as db:
                return {
                    "events": [
                        json.loads(r[0]) for r in db.execute("SELECT body FROM agent_events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 200", (aid, after))
                    ]
                }
        tables = {
            "runs": "agent_runs",
            "goals": "agent_goals",
            "work-packages": "agent_work_packages",
            "learning": "agent_learning_cycles",
            "context": "agent_contexts",
            "routes": "agent_routes",
            "role-revisions": "agent_roles",
            "observations": "agent_observations",
            "sources": "agent_sources",
            "external-events": "agent_external_events",
            "method-feedback": "agent_method_feedback",
        }
        if len(route) in {1, 2} and route[0] in tables:
            with self.store.sessions.transaction() as db:
                result = self.store.get(db, tables[route[0]], route[1], aid) if len(route) == 2 else self.store.rows(db, tables[route[0]], aid)
            if route[0] == "context" and len(route) == 2:
                return self.store.read_artifact(aid, result["artifact_ref"]["sha256"])
            if isinstance(result, list):
                offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["100"])[0])
                if offset < 0 or not 1 <= limit <= 200:
                    raise ServiceError("Invalid pagination")
                return {route[0]: result[offset : offset + limit], "next_offset": offset + limit if offset + limit < len(result) else None}
            return result
        raise ServiceError("Unknown Agent route", 404)
