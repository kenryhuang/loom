"""Dispatch-time dynamic task binding: recommend a per-task spec, fall back to the role's."""

import time
from copy import deepcopy

from loom.service.session_setup import builtin_templates, recommend_setup

# Collections derived from role-level bindings never distinguish a task spec.
DERIVED = frozenset({"knowledge", "external_tools"})


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
            lambda text, *, model, scope, extra=None: recommend_setup(service, text, model=model, scope=scope, extra=extra)
        )
        proposal = recommender(objective, model=role["model"], scope="dispatch")
        templates = {row["id"]: row for row in builtin_templates()}
        template = templates.get(proposal["task_type"], {}).get("task_spec") or templates["general"]["task_spec"]
        spec = deepcopy(template)
        spec["tools"] = {"collections": list(proposal["tool_collections"])}
        for kind in ("context", "workflow"):
            if proposal.get("plugins", {}).get(kind):
                spec[kind] = {**(spec.get(kind) or {}), "plugin": proposal["plugins"][kind]}
        return {
            "task_spec": spec,
            "plan_mode": proposal.get("plan_mode") or "auto",
            "record": {"source": "recommended", "rationale": proposal.get("rationale", ""), "at": at},
        }
    except Exception as exc:  # Recommendation is an optimization; the role contract is the fallback.
        return {
            "task_spec": role.get("task_spec"),
            "plan_mode": role.get("plan_mode") or "auto",
            "record": {"source": "fallback", "reason": str(exc), "at": at},
        }


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
