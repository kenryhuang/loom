"""Transactional Session snapshots, commands, events and execution journals."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from loom.core import now_iso
from loom.service.artifacts import ServiceArtifacts
from loom.service.contracts import EVENT_SCHEMA, ServiceError, canonical, new_id, text, validate_command, validate_create


class SessionStore:
    def __init__(self, directory, *, plugin_registry_factory=None):
        self.plugin_registry_factory = plugin_registry_factory
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "service.sqlite"
        self.artifacts = ServiceArtifacts(self.directory)
        from loom.knowledge.store import KnowledgeStore

        self.knowledge = KnowledgeStore(self.directory / "knowledge")
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, input TEXT NOT NULL, receipt TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(session_id,seq));
                CREATE TABLE IF NOT EXISTS artifacts(session_id TEXT NOT NULL, digest TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(session_id,digest));
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_records(
                    attempt_id TEXT NOT NULL, record_id TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(attempt_id,record_id));
            """)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=10000")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
                if db.total_changes:
                    self.changed.notify_all()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _load(self, db, sid):
        row = db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ServiceError("Session not found", 404)
        return json.loads(row[0])

    def _save(self, db, state):
        db.execute("UPDATE sessions SET body=? WHERE id=?", (canonical(state), state["session_id"]))

    def append(self, db, state, kind, payload, *, command_id=None):
        sid = state["session_id"]
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?", (sid,)).fetchone()[0]
        event = {
            "schema_version": EVENT_SCHEMA,
            "session_id": sid,
            "seq": seq,
            "event_id": new_id("evt"),
            "type": kind,
            "at": now_iso(),
            "task_id": state["task"]["id"],
            "run_id": (state.get("run") or {}).get("id"),
            "command_id": command_id,
            "goal_revision": state["task"]["goal_revision"],
            "plan_revision": state["task"]["plan_revision"],
            "trace_id": payload.get("trace_id"),
            "payload": payload,
        }
        db.execute("INSERT INTO events VALUES(?,?,?)", (sid, seq, canonical(event)))
        state["event_cursor"] = seq
        return event

    def _replay(self, db, cid, normalized):
        row = db.execute("SELECT input,receipt FROM commands WHERE id=?", (cid,)).fetchone()
        if row:
            previous = json.loads(row[0])
            if previous.get("type") == "create":
                previous["payload"].setdefault("knowledge_base_ids", [])
            if canonical(previous) != canonical(normalized):
                raise ServiceError("Command conflict: ID already has different input", 409)
            return json.loads(row[1])
        return None

    def create(self, command_id, payload):
        text(command_id, "command_id", max_length=200)
        payload = validate_create(payload, plugin_registry=self.plugin_registry_factory() if self.plugin_registry_factory else None)
        self.knowledge.validate_ids(payload["knowledge_base_ids"])
        normalized = {"type": "create", "payload": payload}
        with self.transaction() as db:
            replay = self._replay(db, command_id, normalized)
            if replay:
                return replay
            sid = new_id("sess")
            state = {
                "session_id": sid,
                "title": payload["title"],
                "created_at": now_iso(),
                "event_cursor": 0,
                "epoch": 0,
                "task": {
                    "id": new_id("task"),
                    **payload,
                    "objective": payload["objective"] or "",
                    "state": "queued" if payload["objective"] else "idle",
                    "revision": 1,
                    "goal_revision": 1 if payload["objective"] else 0,
                    "plan_revision": 0,
                    "task_kind": "coding" if payload["workspace"] else "general",
                    "plugin_version_refs": payload["plugin_version_refs"],
                    "method_version_refs": [],
                },
                "messages": [],
                "input_request": None,
                "context": None,
                "run": None,
                "control": None,
                "input_cursor": 0,
            }
            db.execute("INSERT INTO sessions VALUES(?,?)", (sid, canonical(state)))
            self.append(db, state, "command.accepted", {"type": "create"}, command_id=command_id)
            if payload["objective"]:
                self._message(db, state, command_id, payload["objective"])
            receipt = {"session_id": sid, "command_id": command_id, "state": "accepted", "input": payload}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (command_id, sid, canonical(normalized), canonical(receipt), "accepted"))
            if not payload["objective"]:
                self.applied(db, state, command_id)
            self._save(db, state)
            return receipt

    def _message(self, db, state, cid, content, *, role="user", status="accepted"):
        message = {"id": new_id("msg"), "role": role, "content": content, "command_id": cid, "state": status}
        event = self.append(db, state, "message.created", message, command_id=cid)
        message["seq"] = event["seq"]
        state["messages"].append(message)
        return message

    def submit(self, sid, command, *, after=None):
        command = validate_command(command)
        cid = command["command_id"]
        normalized = {"session_id": sid, **command}
        with self.transaction() as db:
            replay = self._replay(db, cid, normalized)
            if replay:
                return replay
            state = self._load(db, sid)
            task = state["task"]
            kind, payload = command["type"], command["payload"]
            request = state.get("input_request")
            if kind in {"pause", "stop_run", "resume", "complete_task"} and request and request["kind"] == "recovery" and request["state"] == "pending":
                raise ServiceError("Verify the uncertain effects and answer the recovery question first", 409)
            if kind in {"resume", "answer_input"} and state.get("workspace_blocked"):
                raise ServiceError("Previous processes have not exited; workspace remains blocked", 409)
            expected = command["expected_task_revision"]
            if expected is not None and expected != task["revision"]:
                raise ServiceError("Task revision conflict", 409)
            if kind == "submit_message":
                if task["state"] == "completed":
                    raise ServiceError("Reopen the completed task first", 409)
            elif kind == "set_knowledge_bases":
                if task["state"] not in {"idle", "completed"} or (state["run"] and state["run"]["state"] not in {"completed", "stopped"}):
                    raise ServiceError("Change knowledge bases between tasks, when the session is idle or completed", 409)
                self.knowledge.validate_ids(payload["knowledge_base_ids"])
            elif kind == "set_token_budget":
                if task["state"] in {"running", "pausing", "queued"} or (state["run"] and state["run"]["state"] == "running"):
                    raise ServiceError("Pause execution before changing the token budget", 409)
            elif kind in {"answer_input", "supersede_input"}:
                request = state["input_request"]
                if not request or request["id"] != payload["request_id"] or request["state"] != "pending":
                    raise ServiceError("Input request is stale or already answered", 409)
                if kind == "supersede_input" and request["kind"] == "recovery":
                    raise ServiceError("Recovery cannot be superseded", 409)
            elif kind == "resume":
                if not task["objective"]:
                    raise ServiceError("Enter a task before starting execution", 409)
                if state["run"] and state["run"]["state"] == "running":
                    raise ServiceError("Execution has not reached a suspension boundary", 409)
                if task["state"] not in {"paused", "failed", "recovering"}:
                    raise ServiceError("Task cannot resume in this state", 409)
                if state["input_request"] and state["input_request"]["state"] == "pending":
                    raise ServiceError("Answer the pending recovery question first", 409)
            elif kind == "complete_task" and task["state"] not in {"idle", "paused"}:
                raise ServiceError("Pause or stop active execution before completing", 409)
            elif kind == "reopen_task" and task["state"] != "completed":
                raise ServiceError("Task is not completed", 409)
            elif kind in {"pause", "stop_run"} and task["state"] == "completed":
                raise ServiceError("Task is completed", 409)
            self.append(db, state, "command.accepted", {"type": kind}, command_id=cid)
            if kind == "submit_message":
                self._message(db, state, cid, payload["content"])
                if not task["objective"]:
                    task["objective"] = payload["content"]
                    task["goal_revision"] += 1
                    if state["title"] == "New Session":
                        state["title"] = task["title"] = payload["content"][:80]
                    task["state"] = "queued"
                    self.append(
                        db, state, "task.goal.revised", {"objective": task["objective"], "goal_revision": task["goal_revision"], "title": state["title"]}
                    )
                if task["state"] == "idle":
                    task["state"] = "queued"
                elif task["state"] == "failed" and not state.get("workspace_blocked") and not (request and request["state"] == "pending"):
                    run = state["run"]
                    if run and run["state"] == "running":
                        # Wait for the failing worker's cleanup before retrying.
                        run["retry_requested"] = cid
                    else:
                        self.queue_resume(db, state, cid)
            elif kind in {"answer_input", "supersede_input"}:
                request = state["input_request"]
                request["state"] = "answered" if kind == "answer_input" else "superseded"
                request["answer"] = payload.get("answer", payload.get("content"))
                request["command_id"] = cid
                self._message(db, state, cid, request["answer"])
                self.append(db, state, "input.answered" if kind == "answer_input" else "input.superseded", request, command_id=cid)
                task["state"] = "queued"
            elif kind in {"pause", "stop_run"}:
                previous = state["control"] or {}
                ids = previous.get("command_ids", [previous["command_id"]] if previous.get("command_id") else [])
                control = {"kind": "paused" if kind == "pause" else "stopped", "command_id": cid, "reason": payload.get("reason", kind)}
                if previous.get("kind") == "stopped":
                    control = dict(previous)
                control["command_ids"] = [*ids, cid]
                state["control"] = control
                task["state"] = "pausing" if state["run"] and state["run"]["state"] == "running" else "paused"
            elif kind == "resume":
                self.queue_resume(db, state, cid)
            elif kind == "complete_task":
                task["state"] = "completed"
                state["control"] = None
                if state["run"] and state["run"]["state"] == "suspended":
                    state["run"]["state"] = "stopped"
            elif kind == "reopen_task":
                task["state"] = "idle"
            elif kind == "set_knowledge_bases":
                updated = validate_create(
                    {
                        **{key: task[key] for key in ("objective", "workspace", "title", "model", "plan_mode", "limits", "task_spec")},
                        "objective": task["objective"] or None,
                        "knowledge_base_ids": payload["knowledge_base_ids"],
                    },
                    plugin_registry=self.plugin_registry_factory() if self.plugin_registry_factory else None,
                )
                state["context"] = None  # New tools take effect in a fresh turn; durable conversation/trace remain intact.
                for key in ("knowledge_base_ids", "task_spec", "plugin_version_refs"):
                    task[key] = updated[key]
                self.append(
                    db, state, "task.knowledge.changed", {key: task[key] for key in ("knowledge_base_ids", "task_spec", "plugin_version_refs")}, command_id=cid
                )
            elif kind == "set_token_budget":
                task["limits"]["max_tokens"] = payload["max_tokens"]
                self.append(db, state, "task.budget.changed", {"max_tokens": payload["max_tokens"]}, command_id=cid)
            task["revision"] += 1
            self.append(db, state, "task.state.changed", {"state": task["state"], "revision": task["revision"]})
            receipt = {"session_id": sid, "command_id": cid, "state": "accepted"}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (cid, sid, canonical(normalized), canonical(receipt), "accepted"))
            if kind in {"resume", "reopen_task", "complete_task", "set_token_budget", "set_knowledge_bases"} or (
                kind in {"pause", "stop_run"} and task["state"] == "paused"
            ):
                self.applied(db, state, cid)
            if after is not None:
                after(state, lambda kind, payload: self.append(db, state, kind, payload), db)
            self._save(db, state)
            return receipt

    def queue_resume(self, db, state, command_id):
        state["control"] = None
        run = state["run"]
        if run and run["state"] == "suspended":
            run["time_budget_start_seconds"] = run.get("active_seconds", 0)
            self.append(
                db,
                state,
                "run.time_budget.renewed",
                {"active_seconds": run["time_budget_start_seconds"], "max_duration_seconds": state["task"]["limits"]["max_duration_seconds"]},
                command_id=command_id,
            )
        state["task"]["state"] = "queued"

    def applied(self, db, state, cid):
        row = db.execute("SELECT state FROM commands WHERE id=?", (cid,)).fetchone()
        if not row or row[0] == "applied":
            return
        db.execute("UPDATE commands SET state='applied' WHERE id=?", (cid,))
        for message in state["messages"]:
            if message["command_id"] == cid:
                message["state"] = "applied"
        self.append(db, state, "command.applied", {}, command_id=cid)

    def snapshot(self, sid):
        with self.transaction() as db:
            return self._load(db, sid)

    def list_sessions(self):
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM sessions ORDER BY rowid DESC")]

    def events(self, sid, after=0, limit=200):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0 or not 1 <= limit <= 1000:
            raise ServiceError("Invalid event cursor or limit")
        with self.transaction() as db:
            state = self._load(db, sid)
            if after > state["event_cursor"]:
                raise ServiceError("Cursor exceeds session history; reload snapshot", 409)
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (sid, after, limit))]

    def update(self, sid, callback):
        with self.transaction() as db:
            state = self._load(db, sid)
            result = callback(state, lambda kind, payload: self.append(db, state, kind, payload), db)
            self._save(db, state)
            return result

    def processes(self, sid):
        """Index execution rounds without loading model token streams."""
        with self.transaction() as db:
            state = self._load(db, sid)
            rows = db.execute(
                """SELECT body FROM events WHERE session_id=? AND (
                    json_extract(body,'$.type') IN ('run.started','run.failed','run.stopped','run.recovery.required')
                    OR (json_extract(body,'$.type')='run.state.changed'
                        AND json_extract(body,'$.payload.state')!='running')
                    OR (json_extract(body,'$.type')='message.created'
                        AND json_extract(body,'$.payload.role')='assistant')) ORDER BY seq""",
                (sid,),
            )
            processes, active = [], {}
            for row in rows:
                event = json.loads(row[0])
                run_id = event.get("run_id")
                if not run_id:
                    continue
                process = active.get(run_id)
                if event["type"] == "run.started":
                    if process is None or process["state"] == "completed":
                        process = {"id": f"{run_id}:{event['seq']}", "run_id": run_id, "start_seq": event["seq"], "state": "running", "milestones": []}
                        active[run_id] = process
                        processes.append(process)
                    process["state"] = "running"
                if process is None:
                    continue
                if event["type"] == "message.created":
                    process["state"] = "completed"
                    continue
                process["milestones"].append(event)
                if event["type"] == "run.failed":
                    process["state"] = "failed"
                elif event["type"] == "run.state.changed":
                    process["state"] = event["payload"]["state"]
                elif event["type"] in {"run.stopped", "run.recovery.required"}:
                    process["state"] = "stopped" if event["type"] == "run.stopped" else "suspended"
            for index, process in enumerate(processes):
                process["end_seq"] = processes[index + 1]["start_seq"] - 1 if index + 1 < len(processes) else state["event_cursor"]
            return processes

    def artifact_in_transaction(self, db, sid, value, kind):
        ref = self.artifacts.publish(value, kind)
        db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)", (sid, ref["sha256"], canonical(ref)))
        return ref

    def save_artifact(self, sid, value, kind):
        with self.transaction() as db:
            self._load(db, sid)
            return self.artifact_in_transaction(db, sid, value, kind)

    def read_artifact(self, sid, digest):
        with self.transaction() as db:
            self._load(db, sid)
            if not db.execute("SELECT 1 FROM artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone():
                raise ServiceError("Artifact not referenced by session", 404)
        return self.artifacts.read(digest)
