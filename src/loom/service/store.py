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
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "service.sqlite"
        self.artifacts = ServiceArtifacts(self.directory)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, input TEXT NOT NULL, receipt TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(session_id,seq));
                CREATE TABLE IF NOT EXISTS artifacts(session_id TEXT NOT NULL, digest TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(session_id,digest));
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_records(attempt_id TEXT NOT NULL, record_id TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(attempt_id,record_id));
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
            if row[0] != canonical(normalized):
                raise ServiceError("Command conflict: ID already has different input", 409)
            return json.loads(row[1])
        return None

    def create(self, command_id, payload):
        text(command_id, "command_id", max_length=200)
        payload = validate_create(payload)
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
                    "state": "queued",
                    "revision": 1,
                    "goal_revision": 1,
                    "plan_revision": 0,
                    "task_kind": "coding",
                    "plugin_version_refs": [],
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
            self._message(db, state, command_id, payload["objective"])
            receipt = {"session_id": sid, "command_id": command_id, "state": "accepted", "input": payload}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (command_id, sid, canonical(normalized), canonical(receipt), "accepted"))
            self._save(db, state)
            return receipt

    def _message(self, db, state, cid, content, *, role="user", status="accepted"):
        message = {"id": new_id("msg"), "role": role, "content": content, "command_id": cid, "state": status}
        event = self.append(db, state, "message.created", message, command_id=cid)
        message["seq"] = event["seq"]
        state["messages"].append(message)
        return message

    def submit(self, sid, command):
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
            expected = command["expected_task_revision"]
            if expected is not None and expected != task["revision"]:
                raise ServiceError("Task revision conflict", 409)
            if kind == "submit_message":
                if task["state"] == "completed":
                    raise ServiceError("Reopen the completed task first", 409)
            elif kind in {"answer_input", "supersede_input"}:
                request = state["input_request"]
                if not request or request["id"] != payload["request_id"] or request["state"] != "pending":
                    raise ServiceError("Input request is stale or already answered", 409)
                if kind == "supersede_input" and request["kind"] == "recovery":
                    raise ServiceError("Recovery cannot be superseded", 409)
            elif kind == "resume":
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
                if task["state"] == "idle":
                    task["state"] = "queued"
            elif kind in {"answer_input", "supersede_input"}:
                request = state["input_request"]
                request["state"] = "answered" if kind == "answer_input" else "superseded"
                request["answer"] = payload.get("answer", payload.get("content"))
                request["command_id"] = cid
                self._message(db, state, cid, request["answer"])
                self.append(db, state, "input.answered" if kind == "answer_input" else "input.superseded", request, command_id=cid)
                task["state"] = "queued"
            elif kind in {"pause", "stop_run"}:
                state["control"] = {"kind": "paused" if kind == "pause" else "stopped", "command_id": cid, "reason": payload.get("reason", kind)}
                task["state"] = "pausing" if task["state"] == "running" else "paused"
            elif kind == "resume":
                state["control"] = None
                task["state"] = "queued"
            elif kind == "complete_task":
                task["state"] = "completed"
                state["control"] = None
                if state["run"] and state["run"]["state"] == "suspended":
                    state["run"]["state"] = "stopped"
            elif kind == "reopen_task":
                task["state"] = "idle"
            task["revision"] += 1
            self.append(db, state, "task.state.changed", {"state": task["state"]})
            receipt = {"session_id": sid, "command_id": cid, "state": "accepted"}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (cid, sid, canonical(normalized), canonical(receipt), "accepted"))
            if kind in {"resume", "reopen_task", "complete_task"} or (kind in {"pause", "stop_run"} and task["state"] == "paused"):
                self.applied(db, state, cid)
            self._save(db, state)
            return receipt

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
