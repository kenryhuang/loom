"""Dispatch-time dynamic task binding: recommend a per-task spec, fall back to the role's."""

import time
from copy import deepcopy
from pathlib import Path

from loom.service.contracts import ServiceError
from loom.service.session_setup import DERIVED, builtin_templates, recommend_setup


def spec_from_proposal(proposal):
    """Build a task_spec from a recommendation: template by task_type, collections
    replaced, context/workflow plugins merged over template defaults."""
    templates = {row["id"]: row for row in builtin_templates()}
    template = templates.get(proposal["task_type"], {}).get("task_spec") or templates["general"]["task_spec"]
    spec = deepcopy(template)
    spec["tools"] = {"collections": list(proposal["tool_collections"])}
    for kind in ("context", "workflow"):
        if proposal.get("plugins", {}).get(kind):
            spec[kind] = {**(spec.get(kind) or {}), "plugin": proposal["plugins"][kind]}
    return spec


def bind_for_dispatch(service, agent, work):
    """Recommend a task_spec/plan_mode for this work package. Never raises.

    Returns None for read_only work (restricted fixed environment). Otherwise a
    dict {task_spec, plan_mode, record}; record["source"] is "recommended" or
    "fallback" (recommendation failed: record["reason"] carries the error).
    """
    if work.get("read_only"):
        return None
    role = agent["role"]
    objective = work["objective"] + "\nAcceptance criteria:\n" + "\n".join("- " + c for c in work.get("acceptance", []))
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        recommender = getattr(service, "setup_recommender", None) or (
            lambda text, *, model, scope, extra=None: recommend_setup(service, text, model=model, scope=scope, extra=extra,
                provider_factory=getattr(service, 'setup_provider_factory', None),
                budget_binding=dict(agent_id=agent['id'], goal_id=work['goal_id'], work_package_id=work['id']))
        )
        proposal = recommender(objective, model=role["model"], scope="dispatch",
                               extra={"required_tools": work.get("required_tools", [])})
        return {
            "task_spec": spec_from_proposal(proposal),
            "plan_mode": proposal.get("plan_mode") or "auto",
            "record": {"source": "recommended", "rationale": proposal.get("rationale", ""), "at": at},
        }
    except Exception as exc:  # Recommendation is an optimization; the role contract is the fallback.
        return {
            "task_spec": role.get("task_spec"),
            "plan_mode": role.get("plan_mode") or "auto",
            "record": {"source": "fallback", "reason": str(exc), "at": at},
        }


def repair_collections(service, *, objective, model, unresolved, current_collections, budget_binding=None):
    """Recommend the minimum collection additions for an acceptance-blocked task.

    Re-raises recommender failures (the caller counts them as decision-level
    recovery failures). Returns only the NEW collections; an empty list means
    the repair sees nothing to add.
    """
    recommender = getattr(service, "setup_recommender", None) or (
        lambda text, *, model, scope, extra=None: recommend_setup(
            service, text, model=model, scope=scope, extra=extra,
            provider_factory=getattr(service, "setup_provider_factory", None),
            budget_binding=budget_binding,
        )
    )
    proposal = recommender(
        objective,
        model=model,
        scope="repair",
        extra=dict(unresolved_requirements=list(unresolved), current_collections=list(current_collections)),
    )
    current = set(current_collections)
    return [item for item in proposal["tool_collections"] if item not in current and item not in DERIVED]


def merge_repair_collections(service, task, additions):
    """Deep-copy the task's spec with additions merged in, validated the same way
    session creation validates: normalize_task_spec + TaskAssembly. Raises
    ServiceError when the merged configuration cannot be assembled."""
    from loom.tasks.assembly import TaskAssembly, normalize_task_spec
    from loom.tasks.request import TaskRequest

    spec = deepcopy(task.get("task_spec") or {})
    tools = spec.setdefault("tools", {})
    collections = list(tools.get("collections") or [])
    for item in additions:
        if item not in collections:
            collections.append(item)
    tools["collections"] = collections
    workspace = Path(task["workspace"]) if task.get("workspace") else None
    plan_mode = task.get("plan_mode") or "auto"
    try:
        normalized = normalize_task_spec(spec, workspace=workspace, plan_mode=plan_mode)
        registry = service.plugin_registry_factory() if getattr(service, "plugin_registry_factory", None) else None
        TaskAssembly(
            TaskRequest(
                task.get("objective") or "Session repair",
                workspace=workspace,
                task_spec=normalized,
                metadata={
                    "knowledge_base_ids": task.get("knowledge_base_ids") or [],
                    "external_tool_bindings": task.get("external_tool_bindings") or [],
                },
            ),
            plan_mode=plan_mode,
            registry=registry,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ServiceError(str(exc)) from exc
    return normalized


def compatible_config(candidate_task, payload):
    """Whether an idle session's task config matches the dispatch payload's."""
    def config(task_spec):
        spec = task_spec or {}
        return (
            set((spec.get("tools") or {}).get("collections") or []) - DERIVED,
            (spec.get("context") or {}).get("plugin"),
            (spec.get("workflow") or {}).get("plugin"),
        )

    if config(candidate_task.get("task_spec")) != config(payload.get("task_spec")):
        return False
    return (candidate_task.get("plan_mode") or "auto") == (payload.get("plan_mode") or "auto")


def ensure_required_tools(service, payload, required):
    """Close the minimal collection set over the work contract before dispatch.

    Actual bindings, including resource restrictions and registered external
    tools, are authoritative. Never turn a read-only resource into a writer.
    """
    from loom.tasks.assembly import normalize_task_spec
    from loom.tasks.recovery import TOOL_COLLECTIONS

    payload = deepcopy(payload)
    spec = normalize_task_spec(payload.get("task_spec"), workspace=payload.get("workspace"),
                               plan_mode=payload.get("plan_mode", "auto"))
    collections = spec["tools"]["collections"]
    additions = sorted({TOOL_COLLECTIONS[t] for t in required if t in TOOL_COLLECTIONS} - set(collections))
    collections.extend(additions)
    payload["task_spec"] = spec
    validated, available = validated_tool_ids(service, payload)
    missing = set(required) - available
    if missing:
        raise ServiceError("Work contract requires unavailable tools: " + ", ".join(sorted(missing))
                           + "; check registered bindings and workspace access before dispatch", 409, code="EXECUTION_CONTRACT")
    payload["task_spec"] = validated["task_spec"]
    return payload, additions


def validated_tool_ids(service, payload):
    """Resolve actual bindings consistently for planning and dispatch."""
    validated, assembly = validated_assembly(service, payload)
    return validated, assembly.available_tool_ids()


def validated_assembly(service, payload):
    """Assemble without opening a Session or granting additional permissions."""
    from loom.service.contracts import validate_create
    from loom.tasks.assembly import TaskAssembly
    from loom.tasks.request import TaskRequest

    registry = service.plugin_registry_factory() if service.plugin_registry_factory else None
    validated = validate_create(payload, plugin_registry=registry,
                                external_bindings=service.store.external_tools.resolve(payload.get("external_tool_ids", [])))
    assembly = TaskAssembly(TaskRequest(payload["objective"], workspace=validated["workspace"],
        task_spec=validated["task_spec"], metadata={**{k: validated[k] for k in ("knowledge_base_ids", "external_tool_bindings")},
                                                    "memory_database": str(service.store.path)}),
        registry=registry)
    return validated, assembly
