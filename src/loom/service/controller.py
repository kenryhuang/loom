"""Single-writer supervisor for durable sessions and bounded worker execution."""

from __future__ import annotations

import fcntl
import json
import threading
from dataclasses import replace
from pathlib import Path

from loom.core import Observation, now_iso
from loom.runtime.checkpoints import decode, encode, plain
from loom.service.contracts import ServiceError, canonical, new_id
from loom.service.scheduler import ready_sessions
from loom.service.store import SessionStore
from loom.service.workers import process_identity, reap_group, spawn_attempt
from loom.tasks.runner import _report_from_run_result


class LoomService:
    def __init__(self, directory, *, config_path=None, max_active_runs=2, provider_factory=None):
        if isinstance(max_active_runs, bool) or not isinstance(max_active_runs, int) or max_active_runs < 1:
            raise ServiceError("max_active_runs must be positive")
        self.store = SessionStore(directory)
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
        # Checkpoints/model prompts are internal. Artifacts remain separately accessible.
        state.pop("context", None)
        state.pop("control", None)
        state.pop("epoch", None)
        if state["run"]:
            for key in ("checkpoint", "process", "attempt_id", "counters"):
                state["run"].pop(key, None)
        return state

    def events(self, sid, after=0, limit=200):
        return self.store.events(sid, after, limit)

    def submit(self, sid, command):
        with self.lock:
            state = self.store.snapshot(sid)
            request = state.get("input_request")
            if command.get("type") == "answer_input" and request and request["kind"] == "recovery":
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
        if cp:
            context = cp["context"]
            context = replace(context, state=replace(context.state, observations=(*context.state.observations, *cp["observations"])))
            state["context"] = encode(context)
        if state["run"]:
            state["run"]["checkpoint"] = None
            state["run"]["state"] = "stopped"
            emit("run.stopped", {"reason": "User ended suspended execution"})

    def prepare_attempt(self, sid):
        def prepare(state, emit, db):
            old = state["run"]
            if not old or old["state"] in {"completed", "stopped", "failed"}:
                state["run"] = {"id": new_id("run"), "state": "queued", "steps": 0, "checkpoint": None, "active_seconds": 0, "counters": encode({})}
            state["epoch"] += 1
            state["run"].update(state="running", attempt_id=new_id("attempt"), process=None)
            state["task"]["state"] = "running"
            emit("run.started", {"epoch": state["epoch"]})
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
            value = decode(record["payload"])
            kind = record["type"]
            response = None
            if kind == "event":
                event = plain(value)
                if event["type"].startswith(("plan.", "workflow.")):
                    state["task"]["plan_revision"] += 1
                    state["plan_event"] = event
                emit(event["type"], event)
            elif kind == "boundary":
                run["checkpoint"] = self.store.artifact_in_transaction(db, sid, encode(value), "execution_checkpoint")
                run["counters"] = encode({"llm_calls": value["llm_calls"], "usage": value["usage"]})
                cursor = value["input_cursor"]
                if cursor > state["input_cursor"]:
                    state["task"]["goal_revision"] += 1
                    state["input_cursor"] = cursor
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
                if run["active_seconds"] > state["task"]["limits"]["max_duration_seconds"]:
                    control = {"kind": "paused", "reason": "Active time budget exceeded"}
                response = {"control": control, "inputs": inputs, "input_answer": answer}
            elif kind == "operation_start":
                oid, op = self._operation(db, state, value)
                if op and op["status"] in {"completed", "reconciled"}:
                    response = decode(op["result"])
                elif op and op["status"] == "started" and op["effect_kind"] == "side_effecting":
                    raise ServiceError("Uncertain operation requires recovery", 409)
                elif state["control"] or any(m["role"] == "user" and m["state"] == "accepted" and m["seq"] > state["input_cursor"] for m in state["messages"]):
                    response = {"defer": True}
                else:
                    call = plain(value)
                    op = {"call": call, "status": "started", "effect_kind": "read_only" if call["name"] == "read_file" else "side_effecting", "at": now_iso()}
                    self._save_operation(db, sid, oid, op)
                    run["current_operation"] = oid
                    emit("operation.started", op)
            elif kind == "operation_finish":
                oid, op = self._operation(db, state, value["call"])
                op = op or {"call": plain(value["call"]), "effect_kind": "service_control"}
                op.update(status="completed", result=encode(value["result"]))
                self._save_operation(db, sid, oid, op)
                run["current_operation"] = None
                emit("operation.completed", {"operation_id": oid})
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
                response = bool(state["control"] and state["control"]["kind"] == "stopped")
            elif kind == "status":
                response = {"input_cursor": state["input_cursor"]}
            elif kind == "result":
                state["context"] = encode(value.context)
                control = value.control
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
                        run["checkpoint"] = None
                    request = state["input_request"]
                    state["task"]["state"] = (
                        ("queued" if request and request["state"] != "pending" else "awaiting_input") if control.kind == "waiting_input" else "paused"
                    )
                    if state["control"]:
                        self.store.applied(db, state, state["control"]["command_id"])
                emit("run.state.changed", {"state": run["state"], "reason": control.reason})
                emit("task.state.changed", {"state": state["task"]["state"]})
                state["task"]["revision"] += 1
                response = {"continue": control.kind == "continue"}
            elif kind == "failure":
                state["task"]["state"] = "failed"
                run["state"] = "failed"
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
            processes = [run.get("process")]
            operations = []
            for row in db.execute("SELECT id,body FROM operations WHERE session_id=?", (sid,)):
                op = json.loads(row[1])
                processes.append(op.get("process"))
                if row[0].startswith(run["id"] + ":") and op["status"] == "started" and op["effect_kind"] == "side_effecting":
                    operations.append(row[0])
            for process in processes:
                if process:
                    reap_group(process["pid"], process["identity"])
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

        self.store.update(sid, recovering)

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
                    if not attempt.process.is_alive():
                        attempt.process.join()
                        attempt.connection.close()
                        self.active.pop(sid)
                        state = self.store.snapshot(sid)
                        if state["run"]["state"] == "running":
                            self.recover(sid, f"Worker exited ({attempt.process.exitcode})")
                for sid in ready_sessions(self.store.list_sessions(), self.active, self.capacity):
                    try:
                        descriptor = self.prepare_attempt(sid)
                        attempt = spawn_attempt(descriptor, self.config_path, self.provider_factory)
                        self.active[sid] = attempt
                        process = {"pid": attempt.process.pid, "identity": process_identity(attempt.process.pid)}
                        self.store.update(sid, lambda state, emit, db, process=process: state["run"].update(process=process))
                    except Exception as exc:
                        self.recover(sid, str(exc))

    def close(self):
        if self.closed:
            return
        self.closed = True
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
            self.active.clear()
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        self.owner.close()
