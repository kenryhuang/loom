"""Host-validated autonomous work contracts. No model output is a state mutation."""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loom.service.contracts import ServiceError, object_value, text

SCHEMA_VERSION = 1
TERMINAL = {"completed", "failed", "stopped"}
# Legacy subquota fields remain readable for stored-role compatibility.
# Admission enforces daily_tokens; an optional output_tokens overrides model configuration.
BUDGET_DEFAULTS = dict(daily_tokens=1000000, goal_tokens=100000000, work_tokens=100000, learning_tokens=250000, max_calls=2000, output_tokens=None)
ROLE_FIELDS = {
    "name",
    "role_text",
    "workspace",
    "working_directory",
    "long_term_objectives",
    "source_routes",
    "methods",
    "knowledge_base_ids",
    "external_tool_ids",
    "model",
    "evaluation_model",
    "interval_seconds",
    "timezone",
    "budget",
    "learning_policy",
    "scheduling_policy",
    "task_spec",
    "plan_mode",
    "owner_id",
    "method_learning",
}


def strict(value, allowed):
    value = object_value(value)
    if set(value) - set(allowed):
        raise ServiceError(f"Unknown fields: {', '.join(sorted(set(value) - set(allowed)))}")
    return value


def strings(value, name, maximum=30):
    if not isinstance(value, list) or len(value) > maximum:
        raise ServiceError(f"{name} must be a list of at most {maximum} entries")
    return [text(v, name, max_length=4000) for v in value]


def integer(value, name, low=1, high=100000000):
    if type(value) is not int or not low <= value <= high:
        raise ServiceError(f"{name} must be between {low} and {high}")
    return value


def learning_policy(role):
    """Old roles receive the same bounded host policy as newly created roles."""
    raw = strict(
        role.get("learning_policy", {}), {"auto_expand", "max_learning_tokens", "max_expansions", "max_resumes", "max_attempts", "max_evaluation_calls"}
    )
    budget = role["budget"]
    value = dict(
        auto_expand=True,
        max_learning_tokens=min(budget["daily_tokens"], budget["learning_tokens"] * 4),
        max_expansions=2,
        max_resumes=3,
        max_attempts=6,
        max_evaluation_calls=500,
    )
    value.update(raw)
    if type(value["auto_expand"]) is not bool:
        raise ServiceError("auto_expand must be boolean")
    integer(value["max_learning_tokens"], "max_learning_tokens")
    integer(value["max_expansions"], "max_expansions", 0, 10)
    integer(value["max_resumes"], "max_resumes", 0, 10)
    integer(value["max_attempts"], "max_attempts", 1, 20)
    integer(value["max_evaluation_calls"], "max_evaluation_calls", 1, 500)
    return value


def scheduling_policy(role):
    value = dict(enabled=True, tick_seconds=60, max_recovery_attempts=5, auto_expand_work_budget=True,
                 max_work_tokens=min(role.get("budget", {}).get("daily_tokens", 10000000), role.get("budget", {}).get("goal_tokens", 10000000)),
                 auto_expand_objective_budget=True, max_objective_tokens=role.get("budget", {}).get("goal_tokens", 10000000))
    raw = strict(role.get("scheduling_policy", {}), {*value, "min_slice_seconds", "max_slice_seconds", "default_slice_seconds"})
    value.update({k: v for k, v in raw.items() if k in value})
    if type(value["enabled"]) is not bool:
        raise ServiceError("scheduling enabled must be boolean")
    if type(value["auto_expand_work_budget"]) is not bool:
        raise ServiceError("auto_expand_work_budget must be boolean")
    if type(value["auto_expand_objective_budget"]) is not bool:
        raise ServiceError("auto_expand_objective_budget must be boolean")
    integer(value["tick_seconds"], "tick_seconds", 1, 31536000)
    integer(value["max_recovery_attempts"], "max_recovery_attempts", 0, 10)
    integer(value["max_work_tokens"], "max_work_tokens")
    integer(value["max_objective_tokens"], "max_objective_tokens")
    return value


def role_contract(value):
    value = strict(value, ROLE_FIELDS)
    if type(value.get("method_learning", False)) is not bool:
        raise ServiceError("method_learning must be boolean")
    workspace = value.get("workspace")
    if workspace is not None:
        workspace = str(Path(text(workspace, "workspace")).expanduser().resolve())
        if not Path(workspace).is_dir():
            raise ServiceError("workspace must be an existing directory")
    directory = value.get("working_directory", workspace)
    if directory is not None:
        directory = str(Path(directory).expanduser().resolve())
        if not workspace or not Path(directory).is_relative_to(workspace) or not Path(directory).is_dir():
            raise ServiceError("working_directory must be an existing directory inside workspace")
    objectives = value.get("long_term_objectives", [])
    if not isinstance(objectives, list) or not 1 <= len(objectives) <= 20:
        raise ServiceError("Provide 1–20 long_term_objectives")
    normalized, ids = [], set()
    for item in objectives:
        item = strict(item, {"id", "desired_outcome", "mode", "indicators", "schedule", "iteration", "stage_planning"})
        identifier = text(item.get("id"), "objective id", max_length=100)
        if identifier in ids or item.get("mode", "ongoing") not in {"ongoing", "finite"}:
            raise ServiceError("Duplicate objective or invalid objective mode")
        ids.add(identifier)
        normalized.append(
            dict(
                id=identifier,
                desired_outcome=text(item.get("desired_outcome"), "desired_outcome"),
                mode=item.get("mode", "ongoing"),
                indicators=strings(item.get("indicators", []), "indicators"),
            )
        )
        if item.get("stage_planning") is not None:
            normalized[-1]["stage_planning"] = stage_policy(item["stage_planning"])
        if item.get("iteration") is not None:
            iteration = strict(item["iteration"], {"acceptance", "max_depth", "review_seconds"})
            criteria = strings(iteration.get("acceptance", []), "iteration acceptance")
            if not criteria:
                raise ServiceError("Autonomous iteration requires explicit acceptance")
            normalized[-1]["iteration"] = dict(
                acceptance=criteria,
                max_depth=integer(iteration.get("max_depth", 2), "max_depth", 1, 5),
                review_seconds=integer(iteration.get("review_seconds", 86400), "review_seconds", 60, 31536000),
            )
        if item.get("schedule") is not None:
            schedule = strict(item["schedule"], {"every_seconds", "acceptance"})
            criteria = strings(schedule.get("acceptance", []), "scheduled acceptance")
            if not criteria or item.get("mode", "ongoing") != "ongoing":
                raise ServiceError("Scheduled responsibilities require ongoing mode and acceptance")
            normalized[-1]["schedule"] = dict(every_seconds=integer(schedule.get("every_seconds"), "every_seconds", 10, 31536000), acceptance=criteria)
    timezone = value.get("timezone", "UTC")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ServiceError("Invalid timezone") from exc
    configured_budget = strict(value.get("budget", {}), BUDGET_DEFAULTS)
    budget = {**BUDGET_DEFAULTS, **configured_budget}
    integer(budget["daily_tokens"], "daily_tokens")
    if "work_tokens" not in configured_budget:
        budget["work_tokens"] = max(1, budget["daily_tokens"] // 10)
    for key, amount in budget.items():
        if key == "output_tokens" and amount is None:
            continue
        integer(amount, key)
    routes = value.get("source_routes", [])
    if not isinstance(routes, list) or len(routes) > 30:
        raise ServiceError("source_routes must contain at most 30 routes")
    route_ids = set()
    for route in routes:
        strict(
            route,
            {
                "id",
                "topic",
                "paths",
                "knowledge_base_ids",
                "external_tool_ids",
                "freshness",
                "on_unavailable",
                "refresh_seconds",
                "objective_id",
                "fallback_route_ids",
            },
        )
        identifier = text(route.get("id"), "source route id", max_length=100)
        if identifier in route_ids:
            raise ServiceError("Duplicate source route")
        route_ids.add(identifier)
        text(route.get("topic"), "source topic")
        if "refresh_seconds" in route:
            integer(route["refresh_seconds"], "refresh_seconds", 10, 31536000)
        if route.get("objective_id") and route["objective_id"] not in ids:
            raise ServiceError("Source route references unknown objective")
        for key in ("knowledge_base_ids", "external_tool_ids"):
            refs = strings(route.get(key, []), key)
            if not set(refs) <= set(strings(value.get(key, []), key, 64)):
                raise ServiceError("Source route references unbound resources")
        for path in strings(route.get("paths", []), "paths"):
            if not workspace or not (Path(directory or workspace) / path).resolve().is_relative_to(workspace):
                raise ServiceError("Source path outside workspace")
    for route in routes:
        fallback = strings(route.get("fallback_route_ids", []), "fallback_route_ids")
        if not set(fallback) <= route_ids - {route["id"]}:
            raise ServiceError("Fallback routes must reference other bound sources")
    plan_mode = value.get("plan_mode", "auto")
    if plan_mode not in {"auto", "force", "off"}:
        raise ServiceError("plan_mode must be auto, force or off")
    return dict(
        schema_version=SCHEMA_VERSION,
        owner_id=text(value.get("owner_id", "local"), "owner_id", max_length=100),
        method_learning=value.get("method_learning", False),
        name=text(value.get("name"), "name", max_length=200),
        role_text=text(value.get("role_text"), "role_text", max_length=20000),
        workspace=workspace,
        working_directory=directory,
        long_term_objectives=normalized,
        source_routes=routes,
        methods=strings(value.get("methods", []), "methods"),
        timezone=timezone,
        budget=budget,
        learning_policy=learning_policy({**value, "budget": budget}),
        scheduling_policy=scheduling_policy({**value, "budget": budget}),
        interval_seconds=integer(value.get("interval_seconds", 86400), "interval_seconds", 10, 31536000),
        knowledge_base_ids=strings(value.get("knowledge_base_ids", []), "knowledge_base_ids", 20),
        external_tool_ids=strings(value.get("external_tool_ids", []), "external_tool_ids", 64),
        model=text(value["model"], "model", max_length=200) if value.get("model") is not None else None,
        evaluation_model=text(value["evaluation_model"], "evaluation_model", max_length=200) if value.get("evaluation_model") is not None else None,
        task_spec=value.get("task_spec"),
        plan_mode=plan_mode,
    )


def stage_policy(raw):
    value = strict(
        raw,
        {
            "mode",
            "horizon_stages",
            "max_goals_per_stage",
            "max_new_stages_per_day",
            "max_replans_without_progress",
            "review_seconds",
            "objective_tokens",
            "constraints",
            "completion_criteria",
        },
    )
    if value.get("mode", "off") not in {"off", "autonomous"}:
        raise ServiceError("Invalid stage planning mode")
    result = dict(mode=value.get("mode", "off"))
    for key, default, low, high in [
        ("horizon_stages", 3, 1, 3),
        ("max_goals_per_stage", 3, 1, 3),
        ("max_new_stages_per_day", 3, 1, 10),
        ("max_replans_without_progress", 2, 1, 5),
        ("review_seconds", 86400, 60, 31536000),
        ("objective_tokens", 10000000, 1, 100000000),
    ]:
        result[key] = integer(value.get(key, default), key, low, high)
    for key in ("constraints", "completion_criteria"):
        result[key] = strings(value.get(key, []), key)
    return result


def stage_enabled(objective):
    return objective.get("stage_planning", {}).get("mode") == "autonomous"


def goal_contract(value, role):
    value = strict(
        value,
        {
            "objective",
            "acceptance",
            "long_term_objective_id",
            "depends_on",
            "priority",
            "stop_conditions",
            "learning_dependency",
            "deadline",
            "required_source_ids",
        },
    )
    objective_id = value.get("long_term_objective_id") or role["long_term_objectives"][0]["id"]
    if objective_id not in {g["id"] for g in role["long_term_objectives"]}:
        raise ServiceError("Unknown long-term objective")
    acceptance = strings(value.get("acceptance", []), "acceptance")
    if not acceptance:
        raise ServiceError("Goal requires explicit acceptance criteria")
    deadline = value.get("deadline")
    if deadline is not None:
        from datetime import datetime

        try:
            if datetime.fromisoformat(deadline).tzinfo is None:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ServiceError("deadline requires an ISO timestamp with timezone") from exc
    required = strings(value.get("required_source_ids", []), "required_source_ids")
    if not set(required) <= {r["id"] for r in role["source_routes"]}:
        raise ServiceError("Unknown required source route")
    return dict(
        deadline=deadline,
        required_source_ids=required,
        objective=text(value.get("objective"), "objective"),
        acceptance=acceptance,
        long_term_objective_id=objective_id,
        depends_on=strings(value.get("depends_on", []), "depends_on"),
        priority=integer(value.get("priority", 50), "priority", 0, 100),
        stop_conditions=strings(value.get("stop_conditions", []), "stop_conditions"),
        learning_dependency=strings(value.get("learning_dependency", []), "learning_dependency"),
    )


def check_dependencies(goals):
    graph = {g["id"]: g.get("depends_on", []) for g in goals}
    visiting, visited = set(), set()

    def visit(identifier):
        if identifier not in graph:
            raise ServiceError("Unknown goal dependency")
        if identifier in visiting:
            raise ServiceError("Goal dependency cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)
