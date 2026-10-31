"""Agent state uses the SessionStore transaction and immutable history."""

import hashlib
import json
from datetime import UTC, datetime

from loom.autonomy.contracts import check_dependencies, goal_contract, role_contract
from loom.autonomy.migrations import TABLES, migrate
from loom.service.contracts import ServiceError, canonical, new_id, text


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class AgentStore:
    def __init__(self, sessions, *, clock=None):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        with self.sessions.transaction() as db:
            migrate(db)

    def now(self):
        return self.clock().isoformat()

    def get(self, db, table, identifier, agent_id=None):
        assert table in TABLES
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ServiceError("Agent resource not found", 404)
        value = json.loads(row[0])
        if agent_id is not None and value["agent_id"] != agent_id:
            raise ServiceError("Resource outside Agent scope", 404)
        return value

    def rows(self, db, table, agent_id):
        assert table in TABLES
        return [json.loads(r[0]) for r in db.execute(f"SELECT body FROM {table} WHERE agent_id=? ORDER BY rowid", (agent_id,))]

    def put(self, db, table, body):
        assert table in TABLES
        db.execute(f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (body["id"], body["agent_id"], canonical(body)))
        if "revision" in body:
            db.execute("INSERT OR IGNORE INTO agent_revisions VALUES(?,?,?,?)", (table, body["id"], body["revision"], canonical(body)))

    def event(self, db, agent_id, kind, payload):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM agent_events WHERE agent_id=?", (agent_id,)).fetchone()[0]
        event = dict(agent_id=agent_id, seq=seq, type=kind, at=self.now(), payload=payload)
        db.execute("INSERT INTO agent_events VALUES(?,?,?)", (agent_id, seq, canonical(event)))
        return event

    def replay(self, db, command_id, value):
        text(command_id, "command_id", max_length=200)
        row = db.execute("SELECT input,receipt FROM agent_commands WHERE id=?", (command_id,)).fetchone()
        if row:
            if row[0] != canonical(value):
                raise ServiceError("Command ID has different input", 409)
            return json.loads(row[1])
        return None

    def receipt(self, db, cid, value, receipt):
        db.execute("INSERT INTO agent_commands VALUES(?,?,?)", (cid, canonical(value), canonical(receipt)))
        return receipt

    @staticmethod
    def expect(body, revision):
        if type(revision) is not int or revision != body["revision"]:
            raise ServiceError(f"Revision conflict; current revision is {body['revision']}", 409)

    def artifact(self, db, aid, value, kind="agent_context"):
        ref = self.sessions.artifacts.publish(value, kind)
        db.execute("INSERT OR IGNORE INTO agent_artifacts VALUES(?,?,?)", (aid, ref["sha256"], canonical(ref)))
        return ref

    def read_artifact(self, aid, sha):
        with self.sessions.transaction() as db:
            self.get(db, "agents", aid)
            if not db.execute("SELECT 1 FROM agent_artifacts WHERE agent_id=? AND digest=?", (aid, sha)).fetchone():
                raise ServiceError("Artifact outside Agent scope", 404)
        return json.loads(self.sessions.artifacts.read(sha))

    def create(self, cid, raw):
        role = role_contract(raw)
        self.validate_role_resources(role)
        value = dict(type="create", role=role)
        with self.sessions.transaction() as db:
            replay = self.replay(db, cid, value)
            if replay:
                return replay
            aid = new_id("agent")
            agent = dict(
                id=aid,
                agent_id=aid,
                revision=1,
                generation=1,
                role_revision=1,
                role=role,
                state="draft",
                activity="idle",
                guidance_version=0,
                next_wake_at=self.now(),
                created_at=self.now(),
                planner_lease=None,
            )
            self.put(db, "agents", agent)
            self.put(
                db,
                "agent_roles",
                dict(id=f"{aid}:1", agent_id=aid, revision=1, role=role, explicit_fields=sorted(raw), derived_fields=sorted(set(role) - set(raw))),
            )
            self.event(db, aid, "agent.created", {"revision": 1})
            return self.receipt(db, cid, value, agent)

    def snapshot(self, aid):
        with self.sessions.transaction() as db:
            agent = self.get(db, "agents", aid)
            for key, table in [
                ("goals", "agent_goals"),
                ("work_packages", "agent_work_packages"),
                ("learning", "agent_learning_cycles"),
                ("guidance", "agent_guidance"),
            ]:
                agent[key] = self.rows(db, table, aid)
            agent["event_cursor"] = db.execute("SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE agent_id=?", (aid,)).fetchone()[0]
            agent["sessions"] = [dict(r) for r in db.execute("SELECT * FROM agent_sessions WHERE agent_id=?", (aid,))]
            return agent

    def add_goal(self, aid, cid, raw):
        value = dict(type="goal", agent_id=aid, payload=raw)
        with self.sessions.transaction() as db:
            replay = self.replay(db, cid, value)
            if replay:
                return replay
            agent = self.get(db, "agents", aid)
            goal = dict(**goal_contract(raw, agent["role"]), id=new_id("goal"), agent_id=aid, revision=1, state="queued", created_at=self.now())
            check_dependencies([*self.rows(db, "agent_goals", aid), goal])
            self.put(db, "agent_goals", goal)
            agent.update(generation=agent["generation"] + 1, next_wake_at=self.now())
            self.put(db, "agents", agent)
            self.event(db, aid, "goal.created", {"goal_id": goal["id"]})
            return self.receipt(db, cid, value, goal)

    def command(self, aid, cid, kind, revision, payload=None):
        payload = payload or {}
        if payload:
            raise ServiceError("Agent controls accept an empty payload")
        value = dict(agent_id=aid, type=kind, expected_revision=revision, payload=payload)
        with self.sessions.transaction() as db:
            replay = self.replay(db, cid, value)
            if replay:
                return replay
            agent = self.get(db, "agents", aid)
            self.expect(agent, revision)
            if kind not in {"enable", "pause", "resume", "archive", "wake"}:
                raise ServiceError("Unknown Agent command")
            if agent["state"] == "archived":
                raise ServiceError("Archived Agent cannot run", 409)
            if kind in {"enable", "resume"}:
                agent["state"] = "enabled"
            elif kind == "pause":
                agent["state"] = "paused"
            elif kind == "archive":
                agent["state"] = "archived"
            agent.update(revision=agent["revision"] + 1, generation=agent["generation"] + 1, next_wake_at=self.now(), planner_lease=None)
            self.put(db, "agents", agent)
            self.event(db, aid, f"agent.{kind}", {"revision": agent["revision"]})
            if kind in {"pause", "archive", "resume"}:
                from loom.autonomy.control import enqueue

                for link in db.execute("SELECT * FROM agent_sessions WHERE agent_id=? AND taken_over=0", (aid,)).fetchall():
                    enqueue(db, aid, cid, link["session_id"], "resume" if kind == "resume" else "pause")
            return self.receipt(db, cid, value, agent)

    def revise_role(self, aid, cid, revision, raw):
        role = role_contract(raw)
        self.validate_role_resources(role)
        value = dict(type="role", agent_id=aid, revision=revision, role=role)
        with self.sessions.transaction() as db:
            replay = self.replay(db, cid, value)
            if replay:
                return replay
            agent = self.get(db, "agents", aid)
            self.expect(agent, revision)
            budget_only = {k: v for k, v in agent["role"].items() if k != "budget"} == {k: v for k, v in role.items() if k != "budget"}
            previous_role_revision = agent["role_revision"]
            # Running contracts stay pinned; semantic role changes invalidate queued work.
            agent.update(
                role=role,
                role_revision=agent["role_revision"] + 1,
                revision=revision + 1,
                generation=agent["generation"] + 1,
                guidance_version=agent["guidance_version"] + 1,
                next_wake_at=self.now(),
            )
            self.put(db, "agents", agent)
            if budget_only:
                # Resource limits do not change the execution contract. Keep
                # queued packages dispatchable after a budget-only revision.
                for work in self.rows(db, "agent_work_packages", aid):
                    if work["state"] == "pending" and work["role_revision"] == previous_role_revision:
                        work["role_revision"] = agent["role_revision"]
                        self.put(db, "agent_work_packages", work)
            self.put(
                db,
                "agent_roles",
                dict(
                    id=f"{aid}:{agent['role_revision']}",
                    agent_id=aid,
                    revision=agent["role_revision"],
                    role=role,
                    explicit_fields=sorted(raw),
                    derived_fields=sorted(set(role) - set(raw)),
                ),
            )
            self.event(db, aid, "role.changed", {"role_revision": agent["role_revision"]})
            return self.receipt(db, cid, value, agent)

    def validate_role_resources(self, role):
        from loom.autonomy.dispatch import task_payload
        from loom.service.contracts import validate_create

        self.sessions.knowledge.validate_ids(role["knowledge_base_ids"])
        bindings = self.sessions.external_tools.resolve(role["external_tool_ids"])
        task = validate_create(
            task_payload({"role": role}, dict(objective=role["role_text"], acceptance=[])),
            plugin_registry=self.sessions.plugin_registry_factory() if self.sessions.plugin_registry_factory else None,
            external_bindings=bindings,
        )
        if task["workspace"] != role["working_directory"]:
            raise ServiceError("Task environment must keep the Role working directory")
