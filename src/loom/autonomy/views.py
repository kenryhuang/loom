"""Read models for Agent pages, separate from durable execution snapshots."""

import json

from loom.service.contracts import ServiceError

AGENT_FIELDS = [
    "planning_engine",
    "id",
    "agent_id",
    "revision",
    "role_revision",
    "role",
    "state",
    "activity",
    "error",
    "scheduling_error",
    "background_error",
    "scheduler",
    "schedule_requested",
    "next_schedule_at",
    "next_wake_at",
    "next_learning_at",
    "learning_schedule_requested",
    "pending_configuration",
]
GOAL_FIELDS = [
    "id",
    "revision",
    "stage_id",
    "state",
    "objective",
    "acceptance",
    "proposal",
    "not_before",
    "source_changed",
    "blocked_reason",
    "reason",
    "depends_on",
    "required_source_ids",
    "learning_dependency",
]
WORK_FIELDS = [
    "id",
    "goal_id",
    "session_id",
    "state",
    "objective",
    "order",
    "reason",
    "blocked_reason",
    "superseded_contract",
    "created_at",
    "started_at",
    "dispatched_at",
    "last_slice_at",
    "run_ids",
    "role_revision",
    "terminal_run_id",
    "outcome",
    "slice",
    "recovery",
    "blocker",
]
LEARNING_FIELDS = [
    "id",
    "work_package_id",
    "goal_id",
    "session_id",
    "run_id",
    "analysis_id",
    "evaluation_id",
    "state",
    "phase",
    "coverage",
    "queue_order",
    "enqueued_at",
    "created_at",
    "memory_result",
    "blocked_reason",
    "retry_after",
    "attempts",
    "round_start_attempts",
    "budget_waits",
    "budget_resumes",
    "auto_resume",
]

# Only fields consumed by each tab or its execution projection cross the DB
# boundary. Full records remain available through the resource detail routes.
PAGE_COLLECTIONS = {
    "overview": {key: ["id", "state"] for key in ("goals", "work_packages", "learning")},
    "goals": {
        "goals": GOAL_FIELDS,
        "work_packages": ["id", "goal_id", "session_id", "state", "objective", "order", "reason", "blocked_reason", "superseded_contract"],
        "stages": [
            "kind",
            "plan_id",
            "engine_version",
            "id",
            "objective_id",
            "roadmap_id",
            "revision",
            "state",
            "title",
            "goal_ids",
            "expected_outcome",
            "expected_contribution",
            "acceptance",
            "scope",
            "non_goals",
            "assumptions",
            "stop_conditions",
            "estimated_cost",
            "review_id",
        ],
        "roadmaps": ["id", "objective_id", "revision", "created_at", "baseline", "future", "reason"],
        "objective_planning": ["id", "objective_id", "revision", "state", "stage_id", "roadmap_id", "plan_id", "reason", "manual_pause", "question",
                               "next_review_at", "blocker", "revision_requested"],
        "stage_reviews": ["id", "decision", "learning"],
    },
    "work": {
        "goals": [
            "id",
            "stage_id",
            "state",
            "objective",
            "not_before",
            "source_changed",
            "blocked_reason",
            "reason",
            "depends_on",
            "required_source_ids",
            "learning_dependency",
        ],
        "work_packages": WORK_FIELDS,
        "learning": LEARNING_FIELDS,
        "stages": ["id", "title", "state", "reason"],
        "objective_planning": ["id", "objective_id", "stage_id", "state", "reason", "blocker"],
    },
    "profile": {},
    "knowledge": {},
    "learning": {
        "guidance": ["id", "revision", "text", "scope", "state", "method_version_id", "applied_run_ids"],
        "learning": ["id", "state", "coverage", "session_id", "analysis_id", "evaluation_id", "run_id", "memory_result", "blocked_reason"],
        "work_packages": ["id", "state", "objective"],
    },
}
COLLECTION_TABLES = {
    "goals": "agent_goals",
    "work_packages": "agent_work_packages",
    "learning": "agent_learning_cycles",
    "guidance": "agent_guidance",
    "stages": "agent_stages",
    "roadmaps": "agent_roadmaps",
    "objective_planning": "agent_objective_planning",
    "stage_reviews": "agent_stage_reviews",
}


def project(db, table, fields, where, parameters):
    """SQL and field names are internal constants, never request parameters."""
    pairs = ",".join(f"'{key}',CASE json_type(body,'$.{key}') "
                     f"WHEN 'true' THEN json('true') WHEN 'false' THEN json('false') ELSE json_extract(body,'$.{key}') END" for key in fields)
    return [{k: v for k, v in json.loads(r[0]).items() if v is not None} for r in db.execute(f"SELECT json_object({pairs}) FROM {table} {where}", parameters)]


def page_snapshot(store, aid, view):
    with store.sessions.read() as db:
        agents = project(db, "agents", [*AGENT_FIELDS, *(["selection"] if view == "goals" else [])], "WHERE id=?", (aid,))
        if not agents:
            raise ServiceError("Agent resource not found", 404)
        agent = agents[0]
        collections = PAGE_COLLECTIONS[view]
        for key, table in COLLECTION_TABLES.items():
            agent[key] = project(db, table, collections[key], "WHERE agent_id=? ORDER BY rowid", (aid,)) if key in collections else []
        agent["learning_allocations"] = []
        agent["sessions"] = (
            [dict(r) for r in db.execute("SELECT * FROM agent_sessions WHERE agent_id=?", (aid,))] if view in {"overview", "work", "learning"} else []
        )
        if view == "goals":
            agent["planning_activity"] = planning_activity(db, aid, agent["role"]["long_term_objectives"])
        return agent


def planning_activity(db, aid, objectives):
    """Bounded progress records, without model proposals or input snapshots."""
    activity = []
    for objective in objectives:
        runs = project(db, "agent_runs", ["id", "objective_id", "kind", "state", "created_at", "reason"],
            "WHERE agent_id=? AND json_extract(body,'$.objective_id')=? "
            "AND json_extract(body,'$.kind') IN ('unified_plan','stage_plan','stage_review') ORDER BY rowid DESC LIMIT 3",
            (aid, objective["id"]))
        for run in runs:
            events = db.execute(
                "SELECT seq,json_extract(body,'$.at') AS at,json_extract(body,'$.type') AS type,"
                "json_extract(body,'$.payload.attempt') AS attempt,json_extract(body,'$.payload.code') AS code,"
                "substr(coalesce(json_extract(body,'$.payload.validation_error'),json_extract(body,'$.payload.reason'),"
                "json_extract(body,'$.payload.issues[0].message')),1,1200) AS message "
                "FROM agent_events WHERE agent_id=? "
                "AND (json_extract(body,'$.type') LIKE 'plan.%' OR json_extract(body,'$.type') LIKE 'stage.%') "
                "AND coalesce(json_extract(body,'$.payload.agent_run_id'),json_extract(body,'$.payload.run_id'))=? "
                "ORDER BY seq DESC LIMIT 20", (aid, run["id"])).fetchall()
            run["events"] = [dict(row) for row in reversed(events)]
            activity.append(run)
    return activity


VIEW_FIELDS = {name: set(collections) for name, collections in PAGE_COLLECTIONS.items()}


def add_work_metrics(db, aid, works):
    """Use the same current Session run counters as the Session details."""
    from loom.runtime.checkpoints import decode

    sessions = {
        row[0]: row[1:]
        for row in db.execute(
            "SELECT s.id,json_extract(s.body,'$.run.active_seconds'),json_extract(s.body,'$.run.counters') FROM sessions s "
            "JOIN agent_sessions a ON a.session_id=s.id WHERE a.agent_id=? AND json_type(s.body,'$.run')='object'",
            (aid,),
        )
    }
    for work in works:
        if work.get('session_id') not in sessions:
            continue
        seconds, encoded = sessions[work['session_id']]
        counters = decode(json.loads(encoded)) if encoded else {}
        work["execution_seconds"] = seconds
        work["actual_tokens"] = getattr(counters.get("usage"), "total_tokens", 0)
