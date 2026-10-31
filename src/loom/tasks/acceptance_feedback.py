"""Host completion decisions and durable feedback, independent of workflow policy."""

from copy import deepcopy
from dataclasses import asdict, dataclass

from loom.tasks.workspace_probe import digest


@dataclass(frozen=True)
class CompletionDecision:
    completion_id: str
    assessment_id: str
    outcome: str
    reason_code: str
    reason: str
    feedback_id: str | None
    blockers: list
    binding: dict
    budget: dict

    def snapshot(self):
        return asdict(self)


def progress_binding(controller, candidate):
    """Ignore random call IDs and plan notes; include source content and validity."""
    try:
        binding = controller.fingerprint(candidate) if controller.data.get("plan") else {"goal": controller.data.get("goal_digest")}
    except (ValueError, OSError) as exc:
        binding = {"goal": controller.data.get("goal_digest"), "unavailable": str(exc), "cacheable": False}
    binding.pop("write_epoch", None)
    return binding


def decide(controller, *, origin="completion", completion_id=None):
    """The verifier supplies verdicts; only this host policy admits repair."""
    from loom.tasks.acceptance_budget import public_budget

    data, assembly = controller.data, controller.assembly
    binding = progress_binding(controller, data.get("candidate") or "")
    assessment_id = "assessment:" + digest({"binding": binding, "sequence": data.get("verification_sequence", 0),
                                           "generation": data.get("budget_generation", 0)})
    completion_id = completion_id or assessment_id.replace("assessment:", "completion:")
    blockers = deepcopy([r for r in data["results"] if r["status"] != "passed" and not r.get("deferred")])
    feedback = data.get("feedback")
    outcome, code, reason = "pause", data.get("cause_code") or "ACCEPTANCE_BLOCKED", data["reason"]
    if binding.get("unavailable"):
        code, reason = "ACCEPTANCE_BINDING_UNAVAILABLE", binding["unavailable"]
    elif data["state"] == "passed":
        outcome, code = "accept", "ACCEPTANCE_PASSED"
    elif data.get("pending_operations"):
        code = "EXECUTION_UNKNOWN"
    elif data.get("cause_code"):
        pass
    elif (data.get("plan") or {}).get("unresolved_requirements"):
        code = "ACCEPTANCE_EXTERNAL_DEPENDENCY"
    elif data["model_calls"] >= controller.config["max_model_calls"] or data["elapsed_seconds"] >= controller.config["max_seconds"]:
        code = "ACCEPTANCE_BUDGET_EXHAUSTED"
        reason = (f"Acceptance budget exhausted: model calls {data['model_calls']}/{controller.config['max_model_calls']}; "
                  f"seconds {data['elapsed_seconds']:.1f}/{controller.config['max_seconds']}")
    else:
        gaps = []
        for row in blockers:
            gap = row.get("gap") or {"kind": "deliverable_mismatch" if row["status"] == "failed" else "unknown",
                                      "targets": [], "required_fact": row.get("reason", ""), "suggested_action": ""}
            gaps.append({"criterion_id": row["criterion_id"], "status": row["status"], **gap})
        kinds = {g["kind"] for g in gaps}
        if "external_dependency" in kinds:
            code = "ACCEPTANCE_EXTERNAL_DEPENDENCY"
        elif not gaps or kinds & {"unknown", "insufficient_evidence_view"}:
            code = "ACCEPTANCE_EVIDENCE_VIEW" if "insufficient_evidence_view" in kinds else "ACCEPTANCE_UNKNOWN_GAP"
        elif not _capable(assembly, gaps):
            code = "ACCEPTANCE_CAPABILITY_UNAVAILABLE"
        elif feedback and feedback["state"] == "applied" and feedback["progress_baseline"] == binding:
            code = "ACCEPTANCE_NO_PROGRESS"
            reason = "Acceptance repair produced no new relevant evidence or candidate; unresolved: " + data["reason"]
        elif not controller.can_repair():
            code, reason = "ACCEPTANCE_REPAIR_LIMIT", controller.repair_limit_reason()
        else:
            outcome, code = "repair", "ACCEPTANCE_REPAIR_REQUIRED"
            feedback_id = "feedback:" + digest({"assessment": assessment_id, "gaps": gaps})
            if not feedback or feedback["id"] != feedback_id:
                if feedback:
                    previous = deepcopy(feedback)
                    previous["state"] = "superseded"
                    data.setdefault("feedback_history", []).append(assembly.publish_artifact(previous, "acceptance_feedback"))
                data["feedback"] = {
                    "schema_version": 1, "id": feedback_id, "assessment_id": assessment_id, "completion_id": completion_id,
                    "goal_digest": data["goal_digest"], "task_episode_id": assembly.request.metadata.get("task_episode_id"),
                    "plan_revision": data["revision"], "candidate_ref": assembly.publish_artifact({"candidate": data["candidate"]}, "candidate"),
                    "evidence_manifest_ref": data.get("evidence_manifest_ref"), "gaps": gaps,
                    "prior_attempts_ref": list(data.get("feedback_history", [])), "progress_baseline": binding,
                    "repair_round": data.get("repair_rounds", 0) + 1, "repair_limit": controller.config["max_repairs"],
                    "state": "pending", "origin": origin,
                }
    decision = CompletionDecision(completion_id, assessment_id, outcome, code, reason,
                                  (data.get("feedback") or {}).get("id"), blockers, binding, public_budget(controller))
    data["decision"] = decision.snapshot()
    return decision


def _capable(assembly, gaps):
    from loom.workflows.feedback import TaskFeedbackWorkflow

    if not isinstance(assembly.workflow, TaskFeedbackWorkflow):
        return False
    tools = set(assembly.bindings)
    for gap in gaps:
        for target in gap.get("targets", []):
            if target["kind"] == "tool" and target["value"] not in tools:
                return False
            if target["kind"] == "workspace_path" and not tools.intersection({"read_file", "shell_execute", "process_execute"}):
                return False
    return True


def feedback_objective(feedback):
    return "Resolve task acceptance feedback: " + "; ".join(
        f"{g['criterion_id']}: {g['required_fact']}" for g in feedback["gaps"]
    )
