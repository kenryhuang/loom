"""Transactional Session snapshots, commands, events and execution journals."""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from loom.core import now_iso
from loom.core.sqlite import after_commit, write_lock, write_transaction
from loom.service import session_storage as storage
from loom.service.artifacts import ServiceArtifacts
from loom.service.contracts import EDITABLE_TASK_FIELDS, EVENT_SCHEMA, ServiceError, canonical, new_id, object_value, text, validate_command, validate_create
from loom.service.session_notifications import Notifications

SCHEDULING_PREDICATE = storage.ADMISSION


class SessionStore:
    def __init__(self, directory, *, plugin_registry_factory=None):
        self.plugin_registry_factory = plugin_registry_factory
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "service.sqlite"
        self.artifacts = ServiceArtifacts(self.directory)
        from loom.external_tools.store import ExternalToolStore
        from loom.knowledge.store import KnowledgeStore

        self.lock = write_lock(self.path)
        self.notifications = Notifications()
        from loom.service.session_migration import upgrade_guard

        with upgrade_guard(self.path):
            # WAL persists in the database header: readers never block writers and
            # writers never block readers, so polling and analytics cannot stall runs.
            with sqlite3.connect(self.path) as db:
                db.execute("PRAGMA journal_mode=WAL")
            with self.transaction() as db:
                schema = """
                    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS skill_dirty_sessions(session_id TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS commands(
                        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, input TEXT NOT NULL, receipt TEXT NOT NULL, state TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(session_id,seq));
                    CREATE TABLE IF NOT EXISTS artifacts(session_id TEXT NOT NULL, digest TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(session_id,digest));
                    CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS worker_records(
                        attempt_id TEXT NOT NULL, record_id TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(attempt_id,record_id));
                """
                for statement in schema.split(';'):
                    if statement.strip():
                        db.execute(statement)
                storage.migrate(db)
                from loom.memory.schema import migrate as migrate_memory

                migrate_memory(db)
        self.external_tools = ExternalToolStore(self.directory / "external_tools")
        self.knowledge = KnowledgeStore(self.directory / "knowledge")

    @contextmanager
    def transaction(self):
        with write_transaction(self.path) as db:
            yield db

    def _notify(self, sid):
        after_commit(self.path, lambda: self.notifications.notify(sid), key=(self.notifications, sid))

    @contextmanager
    def read(self):
        """Consistent read snapshot without the write lock; safe under WAL."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN")  # Deferred: never competes with writers.
            yield db
            db.commit()
        finally:
            db.close()

    def _load(self, db, sid, *, lazy=False):
        return storage.load(db, sid, lazy=lazy)

    def _head(self, db, sid):
        return storage.head(db, sid)

    def _save(self, db, state):
        if state['task']['state'] == 'queued':
            state.setdefault('queued_since', now_iso())
        else:
            state.pop('queued_since', None)
        if state.get('autonomy') and storage.lifecycle_changed(state):
            from loom.autonomy.results import record_session_state

            record_session_state(db, state)
        if state.get('deleted_at'):
            from loom.memory.repository import Repository

            Repository(self.path).invalidate(db, session_id=state['session_id'])
        if state.get('deleted_at') and db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone():
            db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")
        storage.save(db, state)
        if state.get('deleted_at'):
            self._notify(state["session_id"])
        db.execute("INSERT OR IGNORE INTO skill_dirty_sessions VALUES(?)", (state['session_id'],))

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
        if (kind in {'acceptance.gate.passed', 'acceptance.gate.blocked', 'run.failed', 'run.stopped'}
                and db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_uses'").fetchone()):
            from loom.skills.runtime import record_outcome

            record_outcome(db, event)
        state["event_cursor"] = seq
        self._notify(sid)
        return event

    def _replay(self, db, cid, normalized):
        row = db.execute("SELECT input,receipt FROM commands WHERE id=?", (cid,)).fetchone()
        if row:
            previous = json.loads(row[0])
            if previous.get("type") == "create":
                previous["payload"].setdefault("knowledge_base_ids", [])
                previous["payload"].setdefault("external_tool_ids", [])
                previous["payload"].setdefault("external_tool_bindings", [])
            if canonical(previous) != canonical(normalized):
                raise ServiceError("Command conflict: ID already has different input", 409)
            return json.loads(row[1])
        return None

    def create(self, command_id, payload, *, after=None, producer='human'):
        text(command_id, "command_id", max_length=200)
        bindings = self.external_tools.resolve(object_value(payload).get("external_tool_ids", []))
        payload = validate_create(payload, plugin_registry=self.plugin_registry_factory() if self.plugin_registry_factory else None, external_bindings=bindings)
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
            storage.save(db, state)
            self.append(db, state, "command.accepted", {"type": "create"}, command_id=command_id)
            if payload["objective"]:
                self._message(db, state, command_id, payload["objective"], producer=producer)
            receipt = {"session_id": sid, "command_id": command_id, "state": "accepted", "input": payload}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (command_id, sid, canonical(normalized), canonical(receipt), "accepted"))
            if not payload["objective"]:
                self.applied(db, state, command_id)
            if after is not None:
                after(state, lambda kind, value: self.append(db, state, kind, value), db)
            self._save(db, state)
            return receipt

    def _message(self, db, state, cid, content, *, role="user", status="accepted", producer=None):
        message = {"id": new_id("msg"), "role": role, "content": content, "command_id": cid, "state": status,
                   "producer": producer or ('human' if role == 'user' else 'solver')}
        event = self.append(db, state, "message.created", message, command_id=cid)
        message["seq"] = event["seq"]
        if isinstance(state, storage.Session) and 'messages' not in state.values_cache:
            db.execute('INSERT INTO session_messages SELECT ?,COALESCE(MAX(position),-1)+1,?,?,? '
                       'FROM session_messages WHERE session_id=?',
                       (state['session_id'], message['seq'], cid, canonical(message), state['session_id']))
        else:
            state["messages"].append(message)
        return message

    def _restart_configuration(self, task, changes, *, relocation=None):
        if "workspace" in changes:
            requested = str(Path(changes["workspace"]).expanduser().resolve()) if changes["workspace"] is not None else None
            if requested != task["workspace"]:
                raise ServiceError("Workspace is fixed for this session. Create a new session to use another directory.")
        value = {key: task.get(key) for key in EDITABLE_TASK_FIELDS}
        value.update(changes)
        value["limits"] = {**task["limits"], **object_value(changes.get("limits", {}), "limits")}
        workflow = value["task_spec"].get("workflow") if isinstance(value["task_spec"], dict) else None
        context = value["task_spec"].get("context") if isinstance(value["task_spec"], dict) else None
        if "max_window_chars" in changes.get("limits", {}) and isinstance(context, dict) and context.get("plugin") in {"bounded_context", "research_context"}:
            value["task_spec"] = json.loads(canonical(value["task_spec"]))
            value["task_spec"]["context"]["max_window_chars"] = value["limits"]["max_window_chars"]
        if "plan_mode" in changes and isinstance(workflow, dict) and workflow.get("plugin") == "legacy_planning":
            value["task_spec"] = json.loads(canonical(value["task_spec"]))
            value["task_spec"]["workflow"]["mode"] = changes["plan_mode"]
        # Restart resolves current registry bindings, rather than replaying pinned tool metadata.
        self.knowledge.validate_ids(value.get("knowledge_base_ids") or [])
        updated = validate_create(value, plugin_registry=self.plugin_registry_factory() if self.plugin_registry_factory else None,
                                  external_bindings=self.external_tools.resolve(value.get("external_tool_ids") or []))
        if updated["workspace"] != task["workspace"] and updated['workspace'] != relocation:
            raise ServiceError("Workspace is fixed for this session. Create a new session to use another directory.")
        return updated

    def submit(self, sid, command, *, after=None, producer='human'):
        command = validate_command(command)
        cid = command["command_id"]
        normalized = {"session_id": sid, **command}
        with self.transaction() as db:
            replay = self._replay(db, cid, normalized)
            if replay:
                return replay
            state = self._load(db, sid, lazy=True)
            task = state["task"]
            kind, payload = command["type"], command["payload"]
            request = state.get("input_request")
            if (kind in {"pause", "stop_run", "resume", "complete_task", "restart_task"}
                    and request and request["kind"] == "recovery" and request["state"] == "pending"):
                raise ServiceError("Verify the uncertain effects and answer the recovery question first", 409)
            if kind in {"resume", "answer_input", "restart_task"} and state.get("workspace_blocked"):
                raise ServiceError("Previous processes have not exited; workspace remains blocked", 409)
            expected = command["expected_task_revision"]
            if expected is not None and expected != task["revision"]:
                raise ServiceError("Task revision conflict", 409)
            if kind == "submit_message":
                if task["state"] == "completed":
                    raise ServiceError("Reopen the completed task first", 409)
                if (producer == 'human' and state.get('autonomy') and state.get('run')
                        and state['run']['state'] in {'completed', 'failed', 'stopped'}):
                    # An unrelated human delivery must not reopen the last Agent contract.
                    binding = state.pop('autonomy')
                    db.execute('UPDATE agent_sessions SET work_package_id=NULL WHERE session_id=? AND work_package_id=?',
                               (sid, binding['work_package_id']))
            elif kind in {"set_knowledge_bases", "set_external_tools"}:
                if task["state"] not in {"idle", "completed", "paused"} or (
                    state["run"] and state["run"]["state"] not in {"completed", "stopped", "suspended"}
                ):
                    raise ServiceError("Change tool bindings while paused or between tasks, when the session is idle or completed", 409)
                if state.get("workspace_blocked") or (request and request["state"] == "pending"):
                    raise ServiceError("Resolve pending input or recovery and wait for workspace processes before changing tools", 409)
                if kind == "set_knowledge_bases":
                    self.knowledge.validate_ids(payload["knowledge_base_ids"])
                else:
                    self.external_tools.resolve(payload["external_tool_ids"])
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
            elif kind == 'restart_task':
                if not payload.get('task', {}).get('objective', task['objective']):
                    raise ServiceError('Enter a task before restarting', 409)
                if task['state'] not in {'idle', 'paused', 'failed', 'completed'} or (state['run'] and state['run']['state'] == 'running'):
                    raise ServiceError('Pause or stop active execution before restarting', 409)
                reassembly = state.get('pending_reassembly') or {}
                relocation = None
                from loom.tasks.workspace_probe import digest

                reassembly_valid = (reassembly.get('command_id') == cid
                                    and reassembly.get('source_run_id') == (state.get('run') or {}).get('id')
                                    and reassembly.get('task_key') == digest(payload.get('task', {})))
                if reassembly.get('command_id') == cid and not reassembly_valid:
                    raise ServiceError('Recovery command does not match its prepared task', 409, code='EXECUTION_CONTRACT')
                if reassembly_valid:
                    copy = reassembly.get('workspace_copy') or {}
                    expected = self.directory / 'recovery_workspaces' / reassembly['source_key']
                    if copy.get('path') == str(expected) and copy.get('source') == task['workspace']:
                        relocation = str(expected)
                updated_task = self._restart_configuration(task, payload.get("task", {}), relocation=relocation)
            elif kind in {"pause", "stop_run"} and task["state"] == "completed":
                raise ServiceError("Task is completed", 409)
            self.append(db, state, "command.accepted", {"type": kind}, command_id=cid)
            if kind == "submit_message":
                self._message(db, state, cid, payload["content"], producer=producer)
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
                extension = state.get('pending_reassembly') or {}
                if extension.get('command_id') == cid and extension.get('tool_extension'):
                    from loom.tasks.tool_extension import extension as validate_extension
                    from loom.tasks.workspace_probe import digest

                    if (extension.get('source_run_id') != (state.get('run') or {}).get('id')
                            or extension.get('task_key') != digest(extension.get('task'))
                            or (state.get('control') or {}).get('command_id')):
                        raise ServiceError('Tool extension no longer matches the paused task', 409, code='EXECUTION_CONTRACT')
                    updated = self._restart_configuration(task, extension['task'])
                    try:
                        change = validate_extension(task['task_spec'], updated['task_spec'])
                    except ValueError as exc:
                        raise ServiceError(str(exc), 409, code='EXECUTION_CONTRACT') from exc
                    if change != extension['tool_extension']:
                        raise ServiceError('Tool extension configuration changed', 409, code='EXECUTION_CONTRACT')
                    for key in ('task_spec', 'plugin_version_refs', 'resource_claims'):
                        task[key] = updated[key]
                    state['run'].update(assembly_update_pending=task['revision'] + 1, tool_extension_pending=change)
                    state.pop('pending_reassembly')
                    self.append(db, state, 'task.configuration.changed',
                                {key: task[key] for key in (*updated, 'goal_revision')}, command_id=cid)
                    self.append(db, state, 'task.tools.extended', change, command_id=cid)
                if state['run']:
                    state['run']['acceptance_budget_request'] = cid
                self.queue_resume(db, state, cid)
            elif kind == "complete_task":
                task["state"] = "completed"
                state["control"] = None
                if state["run"] and state["run"]["state"] == "suspended":
                    state["run"]["state"] = "stopped"
            elif kind == "reopen_task":
                task["state"] = "idle"
            elif kind == 'restart_task':
                previous_run = state['run']
                reassembly = state.pop('pending_reassembly', None)
                if reassembly_valid:
                    state['recovery_context'] = reassembly
                    self.append(db, state, 'task.reassembled', reassembly, command_id=cid)
                else:
                    state.pop('recovery_context', None)
                state['restart_episode_id'] = ((previous_run or {}).get('task_episode_id')
                                               if updated_task['objective'] == task['objective'] else None)
                if updated_task["objective"] != task["objective"]:
                    task["goal_revision"] += 1
                task.update(updated_task)
                state["title"] = task["title"]
                self.append(db, state, "task.configuration.changed", {**updated_task, "goal_revision": task["goal_revision"]}, command_id=cid)
                if request and request['state'] == 'pending':
                    request['state'] = 'cancelled'
                    self.append(db, state, 'input.cancelled', request, command_id=cid)
                if previous_run and previous_run['state'] not in {'completed', 'stopped'}:
                    self.append(db, state, 'run.stopped', {'reason': 'Restarted from scratch'}, command_id=cid)
                pending = [m for m in state['messages'] if m['role'] == 'user' and m['seq'] > state['input_cursor']]
                # Pending guidance belongs to the new goal once, not to an abandoned checkpoint.
                guidance = [] if "objective" in payload.get("task", {}) else [m['content'] for m in pending if m['content'] != task['objective']]
                content = '\n\n'.join([task['objective'], *guidance])
                for message in pending:
                    self.applied(db, state, message['command_id'])
                state['input_cursor'] = state['event_cursor']
                cleared = ['plan', 'plan_run_id', 'workflow', 'workflow_route', 'acceptance', 'acceptance_run_id',
                           'output_artifacts', 'workspace_profile', 'acceptance_current_check', 'plan_event']
                for field in cleared:
                    state.pop(field, None)
                state.update(run=None, context=None, control=None, input_request=None, restart_pending=True)
                task.update(state='queued', plan_revision=0)
                self.append(db, state, 'task.restarted', {'previous_run_id': previous_run['id'] if previous_run else None,
                            'reset_fields': cleared, 'objective': content}, command_id=cid)
                self._message(db, state, cid, content)
            elif kind in {"set_knowledge_bases", "set_external_tools"}:
                updated = validate_create(
                    {
                        **{key: task[key] for key in ("objective", "workspace", "title", "model", "plan_mode", "limits", "task_spec")},
                        "objective": task["objective"] or None,
                        "knowledge_base_ids": payload.get("knowledge_base_ids", task.get("knowledge_base_ids", [])),
                        "external_tool_ids": payload.get("external_tool_ids", task.get("external_tool_ids", [])),
                    },
                    plugin_registry=self.plugin_registry_factory() if self.plugin_registry_factory else None,
                    external_bindings=(self.external_tools.resolve(payload["external_tool_ids"]) if kind == "set_external_tools"
                                       else task.get("external_tool_bindings", [])),
                )
                if task["state"] == "paused" and state["run"] and state["run"]["state"] == "suspended":
                    removed = set(task.get("external_tool_ids", [])) - set(updated["external_tool_ids"])
                    if task.get("knowledge_base_ids") and not updated["knowledge_base_ids"]:
                        removed.update({"knowledge_list", "knowledge_search", "knowledge_read"})
                    nodes = (state.get("workflow") or {}).get("workflow", {}).get("nodes", [])
                    for node in nodes:
                        config = node.get("executor_config", {})
                        required = set(config.get("allowed_tools", [])) | {config.get("tool_id")}
                        if required & removed:
                            raise ServiceError(
                                f"Saved workflow node {node['id']} references detached tools: {', '.join(sorted(required & removed))}. "
                                "Keep these tools attached to resume this workflow.", 409)
                    # Resume must retain the run's progress, but reconsider pending actions with new tools.
                    state["run"]["assembly_update_pending"] = task["revision"] + 1
                else:
                    state["context"] = None
                for key in ("knowledge_base_ids", "external_tool_ids", "external_tool_bindings", "task_spec", "plugin_version_refs"):
                    task[key] = updated[key]
                self.append(
                    db, state, "task.knowledge.changed" if kind == "set_knowledge_bases" else "task.external_tools.changed",
                    {key: task[key] for key in ("knowledge_base_ids", "external_tool_ids", "external_tool_bindings", "task_spec", "plugin_version_refs")},
                    command_id=cid
                )
            elif kind == "set_token_budget":
                task["limits"]["max_tokens"] = payload["max_tokens"]
                self.append(db, state, "task.budget.changed", {"max_tokens": payload["max_tokens"]}, command_id=cid)
            task["revision"] += 1
            self.append(db, state, "task.state.changed", {"state": task["state"], "revision": task["revision"]})
            receipt = {"session_id": sid, "command_id": cid, "state": "accepted"}
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (cid, sid, canonical(normalized), canonical(receipt), "accepted"))
            if kind in {"resume", "reopen_task", "complete_task", "set_token_budget", "set_knowledge_bases", "set_external_tools"} or (
                kind in {"pause", "stop_run"} and task["state"] == "paused"
            ):
                self.applied(db, state, cid)
            # Human intervention re-arms scheduler recovery for the linked work package.
            if (kind in {"resume", "restart_task"} and not cid.startswith("schedule_resume")
                    and db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_sessions'").fetchone()):
                link = db.execute("SELECT agent_id, work_package_id FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
                row = link and link["work_package_id"] and db.execute(
                    "SELECT body FROM agent_work_packages WHERE id=?", (link["work_package_id"],)
                ).fetchone()
                if row:
                    package = json.loads(row[0])
                    recovery = package.get("recovery") or {}
                    if recovery.get("attempts") or recovery.get("exhausted"):
                        package["recovery"] = {"attempts": 0, "pending": None}
                        package["blocker"] = None
                        if recovery.get("exhausted"):
                            package["blocked_reason"] = None
                        db.execute(
                            "INSERT INTO agent_work_packages VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (package["id"], package["agent_id"], canonical(package)),
                        )
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
        if isinstance(state, storage.Session) and 'messages' not in state.values_cache:
            db.execute("UPDATE session_messages SET body=json_set(body,'$.state','applied') WHERE session_id=? AND command_id=?",
                       (state['session_id'], cid))
        else:
            for message in state["messages"]:
                if message["command_id"] == cid:
                    message["state"] = "applied"
        self.append(db, state, "command.applied", {}, command_id=cid)

    def snapshot(self, sid, *, messages_limit=None, context=True):
        with self.read() as db:
            return storage.load(db, sid, messages_limit=messages_limit, context=context)

    def status(self, sid):
        with self.read() as db:
            row = self._head(db, sid)
            return dict(session_id=sid, task={'state': row['task_state']}, run={'state': row['run_state']})

    def pending_messages(self, db, state, *, after=0, accepted=False):
        if isinstance(state, storage.Session) and 'messages' not in state.values_cache:
            rows = db.execute("SELECT body FROM session_messages WHERE session_id=? AND seq>? "
                              "AND json_extract(body,'$.role')='user'" +
                              (" AND json_extract(body,'$.state')='accepted'" if accepted else '') + ' ORDER BY seq',
                              (state['session_id'], after))
            return [json.loads(row[0]) for row in rows]
        return [m for m in state['messages'] if m['role'] == 'user' and m['seq'] > after
                and (not accepted or m['state'] == 'accepted')]

    def list_sessions(self, *, running_only=False):
        with self.read() as db:
            query = "SELECT id FROM session_heads WHERE deleted_at IS NULL"
            if running_only:
                query += " AND run_state='running'"
            return [storage.load(db, row[0]) for row in db.execute(query + ' ORDER BY ordinal DESC')]

    def scheduling_sessions(self):
        with self.read() as db:
            rows = db.execute(f"SELECT * FROM session_heads WHERE {SCHEDULING_PREDICATE} ORDER BY ordinal DESC")
            result = []
            for row in rows:
                task = dict(state=row['task_state'], workspace=row['workspace'])
                if row['resource_claims'] is not None:
                    task['resource_claims'] = json.loads(row['resource_claims'])
                result.append(dict(session_id=row['id'], task=task,
                    workspace_blocked=json.loads(row['workspace_blocked'] or '[]'), autonomy=bool(row['agent_id']),
                    created_at=row['created_at'], queued_since=row['queued_since']))
            return result

    def session_page(self, *, cursor=None, offset=0, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset < 2**63:
            raise ServiceError('Invalid session page')
        values, clause = [], ''
        if cursor is not None:
            if offset:
                raise ServiceError('Use cursor or offset, not both')
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError()
                version, at, ordinal = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if type(version) is not int or version != 1 or not isinstance(at, str) or type(ordinal) is not int or not 1 <= ordinal < 2**63:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ServiceError('Invalid session cursor') from exc
            clause = ' AND (last_activity_at,ordinal)<(?,?)'
            values.extend((at, ordinal))
        with self.read() as db:
            rows = db.execute('SELECT id,title,event_cursor,last_activity_at,ordinal FROM session_heads '
                'WHERE deleted_at IS NULL' + clause + ' ORDER BY last_activity_at DESC,ordinal DESC LIMIT ? OFFSET ?',
                (*values, limit + 1, offset)).fetchall()
            sessions = [dict(session_id=r['id'], title=r['title'], event_cursor=r['event_cursor'],
                task=storage.section(db, r['id'], 'task')) for r in rows[:limit]]
            more = len(rows) > limit
            key = [1, rows[limit-1]['last_activity_at'], rows[limit-1]['ordinal']] if more else None
            next_cursor = base64.urlsafe_b64encode(canonical(key).encode()).decode() if more else None
            return dict(sessions=sessions, next_cursor=next_cursor, next_offset=offset + limit if more and cursor is None else None)

    def messages(self, sid, *, before=None, limit=200):
        if type(limit) is not int or not 1 <= limit <= 1000 or (before is not None and (type(before) is not int or not 0 <= before < 2**63)):
            raise ServiceError('Invalid message cursor or limit')
        with self.read() as db:
            self._head(db, sid)
            clause = ' AND position<?' if before is not None else ''
            rows = db.execute('SELECT position,body FROM session_messages WHERE session_id=?' + clause +
                              ' ORDER BY position DESC LIMIT ?', (sid, *((before,) if before is not None else ()), limit + 1)).fetchall()
            return dict(messages=[json.loads(r[1]) for r in reversed(rows[:limit])],
                        next_before=rows[limit-1][0] if len(rows) > limit else None)

    def list_sessions_by_activity(self):
        with self.read() as db:
            return [storage.load(db, r[0]) for r in db.execute('SELECT id FROM session_heads WHERE deleted_at IS NULL '
                'ORDER BY last_activity_at DESC,ordinal DESC')]

    def delete(self, sid):
        with self.transaction() as db:
            state = storage.load(db, sid, lazy=True, deleted=True)
            if state.get("deleted_at"):
                return {"session_id": sid, "deleted": True}
            if (state["task"]["state"] not in {"idle", "completed", "failed", "paused"}
                    or (state.get("run") or {}).get("state") == "running" or state.get("workspace_blocked")):
                raise ServiceError("Stop the session and wait for its processes to exit before deleting", 409)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("semantic_jobs", "session_analyses"):
                if table in tables and db.execute(
                    f"SELECT 1 FROM {table} WHERE session_id=? AND json_extract(body,'$.state') IN ('queued','running')", (sid,)
                ).fetchone():
                    raise ServiceError("Wait for analysis or stop Deep evaluation before deleting this session", 409)
            state["deleted_at"] = now_iso()
            self._save(db, state)
            return {"session_id": sid, "deleted": True}

    def events(self, sid, after=0, limit=200):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0 or not 1 <= limit <= 1000:
            raise ServiceError("Invalid event cursor or limit")
        with self.read() as db:
            state = self._head(db, sid)
            if after > state["event_cursor"]:
                raise ServiceError("Cursor exceeds session history; reload snapshot", 409)
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (sid, after, limit))]

    def update(self, sid, callback):
        with self.transaction() as db:
            state = self._load(db, sid, lazy=True)
            result = callback(state, lambda kind, payload: self.append(db, state, kind, payload), db)
            self._save(db, state)
            return storage.materialize(result)

    def processes(self, sid):
        """Index execution rounds without loading model token streams."""
        with self.read() as db:
            state = self._head(db, sid)
            rows = db.execute(f"SELECT body FROM events WHERE session_id=? AND {storage.PROCESS_PREDICATE} ORDER BY seq", (sid,))
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
        with self.artifacts.prepare(value, kind) as prepared, self.transaction() as db:
            self._head(db, sid)
            return self.register_artifact(db, sid, prepared)

    def register_artifact(self, db, sid, prepared):
        ref = prepared.install()
        db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?)', (sid, ref['sha256'], canonical(ref)))
        return ref

    def read_artifact(self, sid, digest):
        with self.read() as db:
            self._head(db, sid)
            if not db.execute("SELECT 1 FROM artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone():
                raise ServiceError("Artifact not referenced by session", 404)
        return self.artifacts.read(digest)


    def artifact_ref(self, sid, digest):
        with self.read() as db:
            self._head(db, sid)
            row = db.execute("SELECT ref FROM artifacts WHERE session_id=? AND digest=?", (sid, digest)).fetchone()
            if not row:
                raise ServiceError("Artifact not referenced by session", 404)
            return json.loads(row[0])
