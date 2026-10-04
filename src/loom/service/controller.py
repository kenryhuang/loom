"""Single-writer supervisor for durable sessions and bounded worker execution."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from loom.core import Observation, now_iso
from loom.runtime.checkpoints import decode, encode, plain
from loom.service.contracts import ServiceError, canonical, new_id, stream_key, validate_command
from loom.service.scheduler import ready_sessions
from loom.service.store import SessionStore
from loom.service.workers import process_identity, reap_group, spawn_attempt
from loom.tasks.runner import _report_from_run_result


def _time_budget_exceeded(state):
    run = state["run"]
    elapsed = run["active_seconds"] - run.get("time_budget_start_seconds", 0)
    return elapsed > state["task"]["limits"]["max_duration_seconds"]


class LoomService:
    def __init__(self, directory, *, config_path=None, max_active_runs=2, provider_factory=None, plugin_registry_factory=None):
        if isinstance(max_active_runs, bool) or not isinstance(max_active_runs, int) or max_active_runs < 1:
            raise ServiceError("max_active_runs must be positive")
        self.store = SessionStore(directory, plugin_registry_factory=plugin_registry_factory)
        self.plugin_registry_factory = plugin_registry_factory
        self.owner = (self.store.directory / "supervisor.lock").open("a+")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.owner.close()
            raise ServiceError("Another supervisor owns this data directory", 409) from exc
        self.config_path = str(Path(config_path).resolve()) if config_path else None
        self.capacity = max_active_runs
        self.provider_factory = provider_factory
        self.active = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.closed = False
        self.cleanup_checked = 0
        for state in self.store.list_sessions():
            if state["run"] and state["run"]["state"] == "running":
                self.recover(state["session_id"], "Supervisor restarted")

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._schedule, daemon=True, name="loom-supervisor")
            self.thread.start()
        return self

    def create(self, command_id, payload):
        return self.store.create(command_id, payload)

    def snapshot(self, sid):
        state = self.store.snapshot(sid)
        counters = decode((state["run"] or {}).get("counters", encode({})))
        used = getattr(counters.get("usage"), "total_tokens", 0)
        limit = state["task"]["limits"]["max_tokens"]
        state["token_budget"] = {"limit": limit, "used": used, "remaining": max(0, limit - used)}
        # Checkpoints/model prompts are internal. Artifacts remain separately accessible.
        state.pop("context", None)
        state.pop("control", None)
        state.pop("epoch", None)
        if state["run"]:
            state["streams"] = state["run"].pop("streams", {})
            state["stream_origins"] = state["run"].pop("stream_origins", {})
            for key in ("checkpoint", "process", "attempt_id", "counters", "stream_offsets", "current_operation"):
                state["run"].pop(key, None)
        state["messages"] = state["messages"][-200:]
        return state

    def events(self, sid, after=0, limit=200):
        return self.store.events(sid, after, limit)

    def submit(self, sid, command):
        command = validate_command(command)
        with self.lock:
            state = self.store.snapshot(sid)
            request = state.get("input_request")
            if command.get("type") == "answer_input" and request and request["kind"] == "recovery" and command["payload"]["request_id"] == request["id"]:
                try:
                    answer = json.loads(command["payload"]["answer"])
                    if (
                        answer["resolution"] not in {"completed", "not_applied", "stop"}
                        or not isinstance(answer["evidence"], str)
                        or not answer["evidence"].strip()
                    ):
                        raise ValueError
                except (KeyError, TypeError, ValueError) as exc:
                    raise ServiceError("Recovery answer requires JSON resolution (completed/not_applied/stop) and evidence") from exc
            else:
                answer = None

            def after(state, emit, db):
                if answer:
                    self._reconcile(state, emit, db, answer)
                if command["type"] in {"stop_run", "complete_task"} and (sid not in self.active or not state["run"] or state["run"]["state"] != "running"):
                    self._end_suspended(state, emit, db)

            return self.store.submit(sid, command, after=after)

    def _checkpoint_value(self, state):
        ref = (state.get("run") or {}).get("checkpoint")
        return decode(json.loads(self.store.artifacts.read(ref["sha256"]))) if ref else None

    def _end_suspended(self, state, emit, db):
        if state["input_request"] and state["input_request"]["state"] == "pending":
            state["input_request"]["state"] = "cancelled"
            emit("input.cancelled", state["input_request"])
        cp = self._checkpoint_value(state)
        context = cp["context"] if cp else decode(state.get("context"))
        if context:
            observations = list(context.state.observations) + (list(cp["observations"]) if cp else [])
            seen = {o.id for o in observations}
            run = state.get("run")
            for row in db.execute("SELECT id,body FROM operations WHERE session_id=? ORDER BY rowid", (state["session_id"],)):
                op = json.loads(row[1])
                if not run or not row[0].startswith(run["id"] + ":") or op["status"] not in {"completed", "reconciled"}:
                    continue
                result = decode(op["result"])
                if result["ok"]:
                    observation = result["value"]
                    if not isinstance(observation, Observation):
                        observation = Observation(f"{op['call']['id']}-observation", op["call"]["name"], observation, op["at"])
                else:
                    from loom.llm.api import _tool_failure_observation

                    observation = _tool_failure_observation(
                        result["error"], observation_id=f"{op['call']['id']}-failure", source=op["call"]["name"], at=op["at"]
                    ).unwrap()
                if observation.id not in seen:
                    observations.append(observation)
                    seen.add(observation.id)
            scratch = dict(context.state.scratch or {})
            if run and state.get("plan_run_id") == run["id"]:
                for source, target in (("plan", "plan"), ("workflow_route", "workflowRoute")):
                    if source in state:
                        scratch[target] = state[source]
            context = replace(context, state=replace(context.state, observations=tuple(observations), scratch=scratch))
            state["context"] = encode(context)
        if state["run"]:
            state["run"]["checkpoint"] = None
            state["run"]["state"] = "stopped"
            emit("run.stopped", {"reason": "User ended suspended execution"})

    def prepare_attempt(self, sid):
        def prepare(state, emit, db):
            if state.get("workspace_blocked"):
                raise ServiceError("Previous processes have not exited", 409)
            old = state["run"]
            if not old or old["state"] in {"completed", "stopped", "failed"}:
                state["run"] = {"id": new_id("run"), "state": "queued", "steps": 0, "checkpoint": None, "active_seconds": 0, "counters": encode({})}
                state["run"]["reset_planning"] = bool(old and old["state"] == "completed")
            state["epoch"] += 1
            state["run"].update(state="running", attempt_id=new_id("attempt"), process=None)
            state["task"]["state"] = "running"
            state["task"]["revision"] += 1
            emit("run.started", {"epoch": state["epoch"]})
            emit("task.state.changed", {"state": "running", "revision": state["task"]["revision"]})
            descriptor = json.loads(canonical(state))
            descriptor["checkpoint_value"] = encode(self._checkpoint_value(state))
            return descriptor

        return self.store.update(sid, prepare)

    def _operation(self, db, state, call):
        call = plain(call)
        oid = f"{state['run']['id']}:{call['id']}"
        row = db.execute("SELECT body FROM operations WHERE id=?", (oid,)).fetchone()
        return oid, json.loads(row[0]) if row else None

    def _save_operation(self, db, sid, oid, op):
        db.execute("INSERT OR REPLACE INTO operations VALUES(?,?,?)", (oid, sid, canonical(op)))

    def worker_record(self, sid, record):
        def handle(state, emit, db):
            run = state["run"]
            if not run or record["epoch"] != state["epoch"] or record["attempt_id"] != run["attempt_id"]:
                raise ServiceError("Stale executor epoch", 409)
            previous = db.execute(
                "SELECT response FROM worker_records WHERE attempt_id=? AND record_id=?", (record["attempt_id"], record["record_id"])
            ).fetchone()
            if previous:
                return decode(json.loads(previous[0]))
            if run["state"] != "running":
                raise ServiceError("Executor already suspended or completed", 409)
            attempt = self.active.get(sid)
            if attempt:
                current = time.monotonic()
                run["active_seconds"] += current - attempt.accounted
                attempt.accounted = current
            value = decode(record["payload"])
            kind = record["type"]
            response = None
            if kind == "event":
                event = plain(value)
                if event["type"].endswith(".delta") and isinstance(event.get("delta"), str):
                    offsets = run.setdefault("stream_offsets", {})
                    key = stream_key(event)
                    event["offset"] = offsets.get(key, 0)
                    offsets[key] = event["offset"] + len(event["delta"])
                    streams = run.setdefault("streams", {})
                    origins = run.setdefault("stream_origins", {})
                    text = streams.get(key, "") + event["delta"]
                    dropped = max(0, len(text) - state["task"]["limits"]["max_window_chars"])
                    streams[key] = text[dropped:]
                    origins[key] = origins.get(key, 0) + dropped
                if event["type"] == "llm.completed":
                    prefix = f"{event['llm_call_id']}:"
                    for field in ("streams", "stream_origins", "stream_offsets"):
                        run[field] = {k: v for k, v in run.get(field, {}).items() if not k.startswith(prefix)}
                if event["type"].startswith(("plan.", "workflow.")):
                    state["plan_run_id"] = run["id"]
                    state["task"]["plan_revision"] += 1
                    state["plan_event"] = event
                    for key in ("plan", "workflow_route", "workflow"):
                        if key in event:
                            state[key] = event[key]
                    oid = run.get("current_operation")
                    if oid:
                        row = db.execute("SELECT body FROM operations WHERE id=?", (oid,)).fetchone()
                        op = json.loads(row[0])
                        terminal_event = {
                            "enter_plan": "plan.entered",
                            "submit_plan": "plan.submitted",
                            "update_plan": "plan.updated",
                            "continue_react": "workflow.route.selected",
                            "revise_workflow": "workflow.revised",
                            "complete_node": "workflow.node.completed",
                            "finish": "workflow.node.completed",
                        }.get(op["call"]["name"])
                        if event["type"] == terminal_event:
                            cp = self._checkpoint_value(state)
                            participant = (cp or {}).get("planning", {})
                            observation = Observation(
                                new_id("obs"),
                                op["call"]["name"],
                                {
                                    "accepted": True,
                                    "plan": state.get("plan", participant.get("plan")),
                                    "route": state.get("workflow_route", participant.get("route")),
                                    **({"workflow": state["workflow"]} if "workflow" in state else {}),
                                    **(
                                        {"completed": True, "report": json.loads(op["call"]["arguments"]).get("report", "")}
                                        if op["call"]["name"] == "finish"
                                        else {}
                                    ),
                                },
                                now_iso(),
                                metadata={"controlFlow": {"stepBoundary": True, "reason": "recovered_transition"}},
                            )
                            op.update(status="reconciled", result=encode({"ok": True, "value": observation}))
                            self._save_operation(db, sid, oid, op)
                if len(canonical(event).encode()) > 32768:
                    ref = self.store.artifact_in_transaction(db, sid, event, "event_detail")
                    event = {
                        **{k: event[k] for k in ("type", "trace_id", "llm_call_id", "tool_call_id", "tool_id", "at") if k in event},
                        "artifact": ref,
                        "summary": "Large execution detail; open artifact to read the full content",
                    }
                emit(event["type"], event)
            elif kind == "boundary":
                previous = decode(run.get("counters", encode({})))
                used = getattr(previous.get("usage"), "total_tokens", 0)
                run["checkpoint"] = self.store.artifact_in_transaction(db, sid, encode(value), "execution_checkpoint")
                run["counters"] = encode({"llm_calls": value["llm_calls"], "usage": value["usage"]})
                if value["usage"].total_tokens != used:
                    emit("run.usage.changed", {"total_tokens": value["usage"].total_tokens})
                cursor = value["input_cursor"]
                if cursor > state["input_cursor"]:
                    state["task"]["goal_revision"] += 1
                    state["task"]["objective"] = value["context"].goal.objective
                    state["task"]["revision"] += 1
                    state["input_cursor"] = cursor
                    emit("task.goal.revised", {"objective": state["task"]["objective"], "goal_revision": state["task"]["goal_revision"]})
                    for message in state["messages"]:
                        if message["role"] == "user" and message["seq"] <= cursor:
                            self.store.applied(db, state, message["command_id"])
                request = state["input_request"]
                recovery_command = request.get("command_id") if request and request["kind"] == "recovery" else None
                inputs = [m for m in state["messages"] if m["role"] == "user" and m["seq"] > cursor and m["command_id"] != recovery_command]
                answer = None
                if request and request["state"] in {"answered", "superseded"} and request["kind"] == "clarification":
                    answer = {"request_id": request["id"], "answer": request["answer"], "superseded": request["state"] == "superseded"}
                control = state["control"]
                if not control and _time_budget_exceeded(state):
                    control = {"kind": "paused", "reason": "Active time budget exceeded"}
                response = {"control": control, "inputs": inputs, "input_answer": answer, "goal_revision": state["task"]["goal_revision"]}
            elif kind == "operation_start":
                effect_kind = value.get("effect_kind") if isinstance(value, dict) else None
                value = value["call"] if isinstance(value, dict) and "call" in value else value
                oid, op = self._operation(db, state, value)
                if op and op["call"] != plain(value):
                    raise ServiceError("Tool call ID reused with different arguments", 409)
                if op and op["status"] in {"completed", "reconciled"}:
                    response = decode(op["result"])
                elif op and op["status"] == "started" and op["effect_kind"] == "side_effecting":
                    raise ServiceError("Uncertain operation requires recovery", 409)
                elif state["control"] or any(m["role"] == "user" and m["state"] == "accepted" and m["seq"] > state["input_cursor"] for m in state["messages"]):
                    response = {"defer": True}
                else:
                    call = plain(value)
                    effect = effect_kind or (
                        "service_control"
                        if call["name"] in {"enter_plan", "submit_plan", "update_plan", "continue_react"}
                        else ("read_only" if call["name"] in {"read_file", "finish"} else "side_effecting")
                    )
                    if effect not in {"read_only", "side_effecting", "service_control"}:
                        raise ServiceError("Invalid operation effect declaration")
                    op = {"call": call, "status": "started", "effect_kind": effect, "at": now_iso()}
                    self._save_operation(db, sid, oid, op)
                    run["current_operation"] = oid
                    emit("operation.started", op)
            elif kind == "operation_finish":
                oid, op = self._operation(db, state, value["call"])
                op = op or {"call": plain(value["call"]), "effect_kind": "service_control"}
                result = value["result"]
                if not result["ok"] and result["error"].code == "EXECUTION_UNKNOWN":
                    op.update(status="started", uncertainty=encode(result["error"]))
                    self._save_operation(db, sid, oid, op)
                    run["state"] = "suspended"
                    state["task"]["state"] = "recovering"
                    state["input_request"] = {
                        "id": new_id("input"),
                        "kind": "recovery",
                        "state": "pending",
                        "operations": [oid],
                        "question": "Tool termination or effects are uncertain. Verify them, then answer with JSON resolution "
                        "(completed/not_applied/stop) and evidence.",
                    }
                    emit("operation.uncertain", {"operation_id": oid})
                    emit("input.requested", state["input_request"])
                    emit("run.recovery.required", {"reason": "Tool execution is uncertain", "operations": [oid]})
                    emit("task.state.changed", {"state": "recovering"})
                    db.execute("INSERT INTO worker_records VALUES(?,?,?)", (record["attempt_id"], record["record_id"], "null"))
                    return None
                op.update(status="completed", result=encode(value["result"]))
                self._save_operation(db, sid, oid, op)
                run["current_operation"] = None
                emit("operation.completed", {"operation_id": oid})
            elif kind == "publish_artifact":
                response = self.store.artifact_in_transaction(db, sid, value["value"], value["kind"])
                emit("artifact.created", {"artifact": response})
            elif kind == "read_artifact":
                digest = value["digest"]
                if not db.execute("SELECT 1 FROM artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone():
                    raise ServiceError("Artifact does not belong to this session", 404)
                response = json.loads(self.store.artifacts.read(digest))
            elif kind == "request_input":
                existing = state["input_request"]
                call = plain(value["call"])
                if existing and existing.get("tool_call_id") == call["id"] and existing.get("run_id") == run["id"]:
                    response = existing
                else:
                    response = {
                        "id": new_id("input"),
                        "kind": "clarification",
                        "state": "pending",
                        "run_id": run["id"],
                        "tool_call_id": call["id"],
                        **value["question"],
                    }
                    state["input_request"] = response
                    emit("input.requested", response)
            elif kind == "process_started":
                oid = run.get("current_operation")
                if oid:
                    op = json.loads(db.execute("SELECT body FROM operations WHERE id=?", (oid,)).fetchone()[0])
                    op["process"] = {"pid": value["pid"], "identity": process_identity(value["pid"])}
                    self._save_operation(db, sid, oid, op)
            elif kind == "check_control":
                response = bool(state["control"] and state["control"]["kind"] == "stopped") or _time_budget_exceeded(state)
            elif kind == "poll_control":
                response = state["control"]
                if not response and _time_budget_exceeded(state):
                    response = {"kind": "paused", "reason": "Active time budget exceeded"}
            elif kind == "status":
                response = {"input_cursor": state["input_cursor"]}
            elif kind == "result":
                state["context"] = encode(value.context)
                outputs = (value.context.state.scratch or {}).get("output_artifacts")
                if outputs:
                    state["output_artifacts"] = plain(outputs)
                    emit("task.outputs.changed", {"artifacts": state["output_artifacts"]})
                control = value.control
                pending_control = state["control"]
                if pending_control and control.kind in {"continue", "completed", "waiting_input"}:
                    control = replace(control, kind=pending_control["kind"], reason=pending_control.get("reason", ""))
                    cp = self._checkpoint_value(state)
                    if cp:
                        base = cp["context"]
                        state["context"] = encode(
                            replace(
                                base,
                                state=replace(base.state, observations=(*base.state.observations, *cp["observations"]), scratch=value.context.state.scratch),
                            )
                        )
                if control.kind in {"continue", "completed"}:
                    run["steps"] += 1
                    run["checkpoint"] = None
                if control.kind == "continue" and run["steps"] >= state["task"]["limits"]["max_steps"]:
                    control = replace(control, kind="paused", reason="Step budget exceeded")
                if control.kind == "completed":
                    run["state"] = "completed"
                    state["task"]["state"] = "queued" if any(m["state"] == "accepted" and m["role"] == "user" for m in state["messages"]) else "idle"
                    self.store._message(db, state, None, _report_from_run_result(value), role="assistant", status="applied")
                elif control.kind != "continue":
                    run["state"] = "stopped" if control.kind == "stopped" else "suspended"
                    if control.kind == "stopped":
                        self._end_suspended(state, emit, db)
                    request = state["input_request"]
                    state["task"]["state"] = (
                        ("queued" if request and request["state"] != "pending" else "awaiting_input") if control.kind == "waiting_input" else "paused"
                    )
                    if state["control"]:
                        for cid in state["control"].get("command_ids", [state["control"].get("command_id")]):
                            self.store.applied(db, state, cid)
                    if control.kind == "stopped" and request and request["state"] == "pending":
                        request["state"] = "cancelled"
                        emit("input.cancelled", request)
                emit("run.state.changed", {"state": run["state"], "reason": control.reason})
                state["task"]["revision"] += 1
                emit("task.state.changed", {"state": state["task"]["state"], "revision": state["task"]["revision"]})
                response = {"continue": control.kind == "continue"}
            elif kind == "failure":
                state["task"]["state"] = "failed"
                run["failure"] = value
                emit("run.failed", value)
            else:
                raise ServiceError("Unknown worker record")
            db.execute("INSERT INTO worker_records VALUES(?,?,?)", (record["attempt_id"], record["record_id"], canonical(encode(response))))
            return response

        return self.store.update(sid, handle)

    def recover(self, sid, reason):
        def recovering(state, emit, db):
            run = state["run"]
            if not run:
                return
            operations = []
            for row in db.execute("SELECT id,body FROM operations WHERE session_id=?", (sid,)):
                op = json.loads(row[1])
                if row[0].startswith(run["id"] + ":") and op["status"] == "started" and op["effect_kind"] == "side_effecting":
                    operations.append(row[0])
            self._cleanup_groups(state, emit, db)
            state["epoch"] += 1
            run["state"] = "suspended"
            if operations:
                state["task"]["state"] = "recovering"
                state["input_request"] = {
                    "id": new_id("input"),
                    "kind": "recovery",
                    "state": "pending",
                    "operations": operations,
                    "question": "An operation may have changed the workspace. Verify effects, then answer with JSON "
                    "resolution (completed/not_applied/stop) and evidence.",
                }
                emit("input.requested", state["input_request"])
            elif state["input_request"] and state["input_request"]["state"] == "pending":
                state["task"]["state"] = "awaiting_input"
            else:
                state["task"]["state"] = "paused" if state["control"] else "failed"
            emit("run.recovery.required", {"reason": reason, "operations": operations})
            state["task"]["revision"] += 1
            emit("task.state.changed", {"state": state["task"]["state"], "revision": state["task"]["revision"]})

        self.store.update(sid, recovering)

    def _cleanup_groups(self, state, emit, db):
        run = state["run"]
        processes = [run.get("process")]
        for row in db.execute("SELECT id,body FROM operations WHERE session_id=?", (state["session_id"],)):
            if row[0].startswith(run["id"] + ":"):
                processes.append(json.loads(row[1]).get("process"))
        state["workspace_blocked"] = [p for p in processes if p and not reap_group(p["pid"], p["identity"])]
        if state["workspace_blocked"]:
            emit("workspace.blocked", {"reason": "Previous processes have not exited"})

    def _reconcile(self, state, emit, db, answer):
        sid = state["session_id"]

        def reconcile():
            request = state["input_request"]
            if request.get("reconciled"):
                return
            for oid in request["operations"]:
                op = json.loads(db.execute("SELECT body FROM operations WHERE id=?", (oid,)).fetchone()[0])
                if answer["resolution"] == "completed":
                    observation = Observation(new_id("obs"), op["call"]["name"], {"reconciled": True, "evidence": answer["evidence"]}, now_iso())
                    op.update(status="reconciled", result=encode({"ok": True, "value": observation}))
                else:
                    op["status"] = "not_applied" if answer["resolution"] == "not_applied" else "abandoned"
                self._save_operation(db, sid, oid, op)
                emit("tool.reconciled", {"operation_id": oid, **answer})
            request["reconciled"] = True
            self.store.applied(db, state, request["command_id"])
            if answer["resolution"] == "stop":
                self._end_suspended(state, emit, db)
                state["task"]["state"] = "paused"
            else:
                state["task"]["state"] = "queued"

        reconcile()

    def _schedule(self):
        while not self.stopping.wait(0.02):
            with self.lock:
                if time.monotonic() - self.cleanup_checked >= 1:
                    self.cleanup_checked = time.monotonic()
                    for state in self.store.list_sessions():
                        if state.get("workspace_blocked"):

                            def cleanup(state, emit, db):
                                state["workspace_blocked"] = [p for p in state["workspace_blocked"] if not reap_group(p["pid"], p["identity"])]
                                if not state["workspace_blocked"]:
                                    emit("workspace.released", {})

                            self.store.update(state["session_id"], cleanup)
                for sid, attempt in list(self.active.items()):
                    try:
                        while attempt.connection.poll():
                            record = attempt.connection.recv()
                            try:
                                result = self.worker_record(sid, record)
                                attempt.connection.send({"value": encode(result)})
                            except ServiceError as exc:
                                attempt.connection.send({"error": str(exc)})
                    except (EOFError, BrokenPipeError, OSError):
                        pass
                    state = self.store.snapshot(sid)
                    if attempt.process.is_alive() and state["run"]["state"] != "running":
                        attempt.terminal_since = attempt.terminal_since or time.monotonic()
                        if time.monotonic() - attempt.terminal_since > 2:
                            attempt.process.kill()
                    if not attempt.process.is_alive():
                        attempt.process.join()
                        attempt.connection.close()
                        state = self.store.snapshot(sid)
                        if state["run"]["state"] == "running":
                            self.recover(sid, f"Worker exited ({attempt.process.exitcode})")
                        else:
                            self.store.update(sid, self._cleanup_groups)
                        self.active.pop(sid)
                for sid in ready_sessions(self.store.list_sessions(), self.active, self.capacity):
                    try:
                        descriptor = self.prepare_attempt(sid)
                        attempt = spawn_attempt(descriptor, self.config_path, self.provider_factory, self.plugin_registry_factory)
                        self.active[sid] = attempt
                        process = {"pid": attempt.process.pid, "identity": process_identity(attempt.process.pid)}
                        self.store.update(sid, lambda state, emit, db, process=process: state["run"].update(process=process))
                    except Exception as exc:
                        self.recover(sid, str(exc))

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.thread:
            with self.lock:
                for sid in self.active:

                    def pause(state, emit, db):
                        if state["run"]["state"] == "running" and not state["control"]:
                            state["control"] = {"kind": "paused", "reason": "Service shutdown", "command_id": None}

                    self.store.update(sid, pause)
            deadline = time.monotonic() + 2
            while self.active and time.monotonic() < deadline:
                time.sleep(0.02)
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)
        with self.lock:
            for sid, attempt in list(self.active.items()):
                if attempt.process.is_alive():
                    attempt.process.terminate()
                attempt.process.join(timeout=1)
                if attempt.process.is_alive():
                    attempt.process.kill()
                    attempt.process.join(timeout=1)
                attempt.connection.close()
                state = self.store.snapshot(sid)
                if state["run"]["state"] == "running":
                    self.recover(sid, "Service shut down")
                else:
                    self.store.update(sid, self._cleanup_groups)
            self.active.clear()
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        self.owner.close()
