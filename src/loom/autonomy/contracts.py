"""Host-validated autonomous work contracts. No model output is a state mutation."""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loom.service.contracts import ServiceError, object_value, text

SCHEMA_VERSION = 1
TERMINAL = {"completed", "failed", "stopped"}
BUDGET_DEFAULTS = dict(daily_tokens=1000000, goal_tokens=100000000, work_tokens=100000, learning_tokens=250000, max_calls=2000, output_tokens=8192)
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
    "task_spec",
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
    raw = strict(role.get("learning_policy", {}), {"auto_expand", "max_learning_tokens", "max_expansions", "max_resumes"})
    budget = role["budget"]
    value = dict(auto_expand=True, max_learning_tokens=min(budget["daily_tokens"], budget["learning_tokens"] * 4),
                 max_expansions=2, max_resumes=3)
    value.update(raw)
    if type(value["auto_expand"]) is not bool:
        raise ServiceError("auto_expand must be boolean")
    integer(value["max_learning_tokens"], "max_learning_tokens")
    integer(value["max_expansions"], "max_expansions", 0, 10)
    integer(value["max_resumes"], "max_resumes", 0, 10)
    return value


def role_contract(value):
    value = strict(value, ROLE_FIELDS)
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
        item = strict(item, {"id", "desired_outcome", "mode", "indicators", "schedule"})
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
        integer(amount, key)
    routes = value.get("source_routes", [])
    if not isinstance(routes, list) or len(routes) > 30:
        raise ServiceError("source_routes must contain at most 30 routes")
    route_ids = set()
    for route in routes:
        strict(route, {"id", "topic", "paths", "knowledge_base_ids", "external_tool_ids", "freshness", "on_unavailable"})
        identifier = text(route.get("id"), "source route id", max_length=100)
        if identifier in route_ids:
            raise ServiceError("Duplicate source route")
        route_ids.add(identifier)
        text(route.get("topic"), "source topic")
        for key in ("knowledge_base_ids", "external_tool_ids"):
            refs = strings(route.get(key, []), key)
            if not set(refs) <= set(strings(value.get(key, []), key, 64)):
                raise ServiceError("Source route references unbound resources")
        for path in strings(route.get("paths", []), "paths"):
            if not workspace or not (Path(directory or workspace) / path).resolve().is_relative_to(workspace):
                raise ServiceError("Source path outside workspace")
    return dict(
        schema_version=SCHEMA_VERSION,
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
        interval_seconds=integer(value.get("interval_seconds", 86400), "interval_seconds", 10, 31536000),
        knowledge_base_ids=strings(value.get("knowledge_base_ids", []), "knowledge_base_ids", 20),
        external_tool_ids=strings(value.get("external_tool_ids", []), "external_tool_ids", 64),
        model=text(value["model"], "model", max_length=200) if value.get("model") is not None else None,
        evaluation_model=text(value["evaluation_model"], "evaluation_model", max_length=200) if value.get("evaluation_model") is not None else None,
        task_spec=value.get("task_spec"),
    )


def goal_contract(value, role):
    value = strict(value, {"objective", "acceptance", "long_term_objective_id", "depends_on", "priority", "stop_conditions", "learning_dependency"})
    objective_id = value.get("long_term_objective_id") or role["long_term_objectives"][0]["id"]
    if objective_id not in {g["id"] for g in role["long_term_objectives"]}:
        raise ServiceError("Unknown long-term objective")
    acceptance = strings(value.get("acceptance", []), "acceptance")
    if not acceptance:
        raise ServiceError("Goal requires explicit acceptance criteria")
    return dict(
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
