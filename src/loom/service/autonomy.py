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

    def snapshot(self, aid):
        result = self.store.snapshot(aid)
        result["service_enabled"] = self.service.autonomy_enabled
        result["service_error"] = self.supervisor.last_error
        result["budget"] = BudgetLedger(self.store.sessions.path).summary(aid)
        result["pause_pending"] = result["state"] in {"paused", "archived"} and any(s["session_id"] in self.service.active for s in result["sessions"])
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
                works = [w for w in self.store.rows(db, "agent_work_packages", aid) if w["goal_id"] == gid]
                if kind == "revise":
                    if any(w["state"] in {"dispatched", "running", "blocked"} and w.get("session_id") for w in works):
                        raise ServiceError("Pause and finish or cancel current work before revising its contract", 409)
                    goal.update(goal_contract(body.get("payload", {}), agent["role"]))
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
        strict(body, {"command_id", "type", "expected_revision"})
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
            if kind == "bind":
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
            if action == "retry" and cycle["state"] not in {"failed", "deferred", "completed", "cancelled"}:
                raise ServiceError("Learning is already active", 409)
            if action == "retry" and cycle["state"] == "completed" and cycle["coverage"] != "partial":
                raise ServiceError("Completed evaluation has no retryable gap", 409)
            cycle.update(requested_action=action, command_generation=cycle.get("command_generation", 0) + 1, retry_options=limits, retry_after="")
            if action == "cancel":
                cycle["state"] = "cancelled"
            self.store.put(db, "agent_learning_cycles", cycle)
            self.store.event(db, aid, "learning.command", dict(cycle_id=lid, action=action))
            return self.store.receipt(db, body["command_id"], request, cycle)

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
            with self.store.sessions.transaction() as db:
                return {"agents": [json.loads(r[0]) for r in db.execute("SELECT body FROM agents")], "service_enabled": self.service.autonomy_enabled}
        aid = parts[0]
        self.store.snapshot(aid)  # All routes enforce existing Agent identity.
        if len(parts) == 1 and method == "GET":
            return self.snapshot(aid)
        route = parts[1:]
        if route == ["commands"] and method == "POST":
            return self.control(aid, body)
        if route == ["role", "revisions"] and method == "POST":
            strict(body, {"command_id", "expected_revision", "role"})
            return self.store.revise_role(aid, body.get("command_id"), body.get("expected_revision"), body.get("role"))
        if route == ["goals"] and method == "POST":
            strict(body, {"command_id", "goal"})
            return self.store.add_goal(aid, body.get("command_id"), body.get("goal"))
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
        if len(route) == 3 and route[0] == "learning" and route[2] == "commands" and method == "POST":
            return self.learning_command(aid, route[1], body)
        if method != "GET":
            raise ServiceError("Unknown Agent route", 404)
        if route == ["memory"]:
            from loom.memory.store import MemoryStore

            agent = self.store.snapshot(aid)
            if not agent["role"]["working_directory"]:
                sid = query.get("session_id", [""])[0]
                if sid not in {s["session_id"] for s in agent["sessions"]}:
                    raise ServiceError("Select an owned Session for memory without workspace")
            else:
                sid = ""
            return MemoryStore(self.store.sessions.path, workspace=agent["role"]["working_directory"], session_id=sid).search(
                query.get("query", [agent["role"]["role_text"]])[0]
            )
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
