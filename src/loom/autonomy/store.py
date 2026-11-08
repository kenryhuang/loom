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
            if not db.execute('SELECT 1 FROM autonomy_migrations WHERE version=6').fetchone():
                for row in db.execute("SELECT id FROM sessions WHERE json_extract(body,'$.autonomy.agent_id') IS NOT NULL").fetchall():
                    state = self.sessions._load(db, row['id'])
                    if state['task']['limits'].get('max_tokens') is None:
                        continue
                    state['task']['limits']['max_tokens'] = None
                    state['task']['revision'] += 1
                    self.sessions.append(db, state, 'task.budget.changed', dict(max_tokens=None, mode='daily', revision=state['task']['revision']))
                    self.sessions._save(db, state)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone():
                    for row in db.execute("SELECT body FROM agent_learning_cycles WHERE json_extract(body,'$.state') "
                                          "IN ('deferred','incomplete')").fetchall():
                        cycle = json.loads(row['body'])
                        if (cycle.get('budget_decision') or {}).get('outcome') not in {'wait', 'stop'}:
                            continue
                        denial = db.execute('SELECT body FROM agent_budget_blocks WHERE id=?', (cycle.get('evaluation_id'),)).fetchone()
                        job_row = db.execute('SELECT body FROM semantic_jobs WHERE id=?', (cycle.get('evaluation_id'),)).fetchone()
                        if not denial or not job_row:
                            continue
                        block, job = json.loads(denial['body']), json.loads(job_row['body'])
                        if (any(block.get('binding', {}).get(key) != cycle.get(key)
                                for key in ('agent_id', 'goal_id', 'work_package_id', 'evaluation_id'))
                                or not any(s.get('scope') in {'objective', 'goal', 'work', 'learning', 'calls'} for s in block.get('scopes', []))
                                or job.get('state') in {'completed', 'running', 'queued'}
                                or not any(e.get('provider_code') == 'AGENT_BUDGET' for e in job.get('error_details', []))):
                            continue
                        # Reassess with the new policy; progress/attempt/control
                        # guards still run before any model call is admitted.
                        cycle.update(state='pending', auto_resume=True, budget_policy_seen=0, retry_after='', blocked_reason=None)
                        self.put(db, 'agent_learning_cycles', cycle)
                        self.event(db, cycle['agent_id'], 'learning.budget.policy_reassessed', dict(cycle_id=cycle['id'], mode='daily'))
                db.execute('INSERT INTO autonomy_migrations VALUES(6)')

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
        from loom.autonomy.acceptance import initialize, sync_objectives

        if table in {"agent_goals", "agent_stages", "agent_work_packages"}:
            initialize(body)
        elif table == "agents":
            prior = db.execute("SELECT body FROM agents WHERE id=?", (body["id"],)).fetchone()
            sync_objectives(body, json.loads(prior[0]) if prior else None)
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
                next_schedule_at=self.now(),
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

    def snapshot(self, aid, *, fields=None, summaries=False):
        with self.sessions.read() as db:
            agent = self.get(db, "agents", aid)
            for key, table in [
                ("goals", "agent_goals"),
                ("work_packages", "agent_work_packages"),
                ("learning", "agent_learning_cycles"),
                ("learning_allocations", "agent_learning_allocations"),
                ("guidance", "agent_guidance"),
                ("stages", "agent_stages"),
                ("roadmaps", "agent_roadmaps"),
                ("objective_planning", "agent_objective_planning"),
                ("stage_reviews", "agent_stage_reviews"),
            ]:
                if fields is not None and key not in fields:
                    agent[key] = []
                elif summaries:
                    agent[key] = [dict(r) for r in db.execute(
                        f"SELECT id,json_extract(body,'$.state') AS state FROM {table} WHERE agent_id=? ORDER BY rowid", (aid,))]
                else:
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
        if payload and kind != 'recover':
            raise ServiceError("Agent controls accept an empty payload")
        value = dict(agent_id=aid, type=kind, expected_revision=revision, payload=payload)
        with self.sessions.transaction() as db:
            replay = self.replay(db, cid, value)
            if replay:
                return replay
            agent = self.get(db, "agents", aid)
            self.expect(agent, revision)
            if kind not in {"enable", "pause", "resume", "archive", "wake", "recover"}:
                raise ServiceError("Unknown Agent command")
            if agent["state"] == "archived":
                raise ServiceError("Archived Agent cannot run", 409)
            if kind == 'recover':
                from loom.autonomy.contracts import scheduling_policy, strict

                strict(payload, {'work_package_id', 'reason'})
                reason = text(payload.get('reason'), 'reason')
                work = self.get(db, 'agent_work_packages', text(payload.get('work_package_id'), 'work_package_id'), aid)
                if agent['state'] != 'enabled' or work['state'] not in {'blocked', 'failed'}:
                    raise ServiceError('Only blocked or failed work packages of an enabled Agent can be re-evaluated', 409)
                recovery = work.setdefault('recovery', {})
                if recovery.get('pending') or agent.get('scheduler', {}).get('state') in {'checking', 'planning'}:
                    raise ServiceError('A recovery check is already in progress; wait for it to finish', 409)
                before = max(recovery.get('attempt_limit', 0), scheduling_policy(agent['role'])['max_recovery_attempts'])
                after = max(before, recovery.get('attempts', 0) + 1)
                recovery.update(exhausted=False, attempt_limit=after,
                                manual_grants=[*recovery.get('manual_grants', []), dict(command_id=cid, reason=reason, before=before, after=after)])
                work.update(blocker=None, blocked_reason=None)
                self.put(db, 'agent_work_packages', work)
                agent.update(revision=agent['revision'] + 1, schedule_requested=True, next_schedule_at=self.now())
                self.put(db, 'agents', agent)
                self.event(db, aid, 'schedule.recovery.reassessment', dict(work_package_id=work['id'], command_id=cid, reason=reason))
                return self.receipt(db, cid, value, agent)
            if kind == "wake":
                # A manual check must not invalidate an in-flight planning response.
                busy = agent.get("scheduler", {}).get("state") in {"checking", "planning"}
                if not busy and agent["state"] == "enabled":
                    from loom.autonomy.blockers import retryable

                    for planning in self.rows(db, "agent_objective_planning", aid):
                        if not planning.get("manual_pause") and retryable(planning.get("blocker")):
                            planning.update(retry_requested=True, revision=planning["revision"] + 1)
                            self.put(db, "agent_objective_planning", planning)
                    agent.update(revision=agent["revision"] + 1, schedule_requested=True, next_schedule_at=self.now(), learning_schedule_requested=True)
                    self.put(db, "agents", agent)
                self.event(db, aid, "schedule.manual", dict(outcome="busy" if busy else "queued" if agent["state"] == "enabled" else "paused"))
                return self.receipt(db, cid, value, agent)
            if kind in {"enable", "resume"}:
                agent["state"] = "enabled"
            elif kind == "pause":
                agent["state"] = "paused"
            elif kind == "archive":
                agent["state"] = "archived"
            agent.update(revision=agent["revision"] + 1, generation=agent["generation"] + 1, next_wake_at=self.now(), planner_lease=None)
            if kind in {"enable", "resume"}:
                agent["next_schedule_at"] = self.now()
                agent["next_learning_at"] = self.now()
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
            if any(role[key] != agent['role'][key] for key in ('workspace', 'working_directory')):
                raise ServiceError('Agent workspace is fixed; create a new Agent to use another directory', 409)
            previous_role = {'owner_id': 'local', 'method_learning': False, **agent['role']}
            from loom.autonomy.stages import role_signature

            budget_only = role_signature(previous_role) == role_signature(role)
            previous_role_revision = agent["role_revision"]
            agent.pop('pending_configuration', None)
            # Running contracts stay pinned; semantic role changes invalidate queued work.
            agent.update(
                role=role,
                role_revision=agent["role_revision"] + 1,
                revision=revision + 1,
                generation=agent["generation"] + 1,
                guidance_version=agent["guidance_version"] + 1,
                next_wake_at=self.now(),
                next_schedule_at=self.now(),
                next_learning_at=self.now(),
            )
            self.put(db, "agents", agent)
            from loom.autonomy.stage_commands import reconcile_role_change

            reconcile_role_change(self, db, agent, previous_role, cid)
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
