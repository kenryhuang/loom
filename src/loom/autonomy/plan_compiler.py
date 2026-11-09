"""Compile a whole stage before publishing any executable work.

The compiler has no database writes and never calls a model. Both planning and
dispatch resolve capabilities through the same real TaskAssembly bindings.
"""

from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.autonomy.acceptance import inherit, initialize, objective_view
from loom.autonomy.binding import validated_assembly
from loom.autonomy.contracts import goal_contract
from loom.autonomy.dispatch import task_payload
from loom.autonomy.stage_planner import SCHEMA, TEXT, TEXTS, obj, validate, validate_evidence_refs
from loom.autonomy.store import digest
from loom.core import thaw_json
from loom.service.contracts import ServiceError
from loom.tasks.acceptance import AcceptanceController
from loom.tasks.acceptance_contract import evidence_check
from loom.tasks.acceptance_protocols import plan_protocol


class CompileError(ServiceError):
    def __init__(self, message, path="/", *, code="CONTRACT_INVALID"):
        super().__init__(message, 409, code=code)
        self.details = dict(issues=[dict(path=path, message=message)])


def environments(service, agent):
    result = {}
    for mode in ("inspect", "workspace"):
        payload = task_payload(agent, dict(objective="Plan environment", acceptance=[], read_only=mode == "inspect"))
        validated, assembly = validated_assembly(service, payload)
        result[mode] = dict(payload=payload, validated=validated, assembly=assembly)
    return result


def binding_fingerprint(assembly):
    return digest(dict(spec=assembly.spec, external_bindings=assembly.request.metadata.get("external_tool_bindings", []),
                        tools=[dict(id=b.ref.id, schema=thaw_json(b.ref.input_schema),
                        effect=b.effect_kind, provider=b.id, version=b.version, entrypoint=b.entrypoint_id,
                        resources=b.resource_refs)
                        for b in assembly.bindings.values()]))


def capabilities(envs):
    return {mode: dict(fingerprint=binding_fingerprint(env["assembly"]),
                      resources=env["validated"]["task_spec"]["session_environment"].get("resources", []),
                      tools=[dict(id=b.ref.id, description=b.ref.description, input_schema=thaw_json(b.ref.input_schema),
                                  effect=b.effect_kind) for b in env["assembly"].bindings.values()])
            for mode, env in envs.items()}


def schema(envs, payload=None):
    value = deepcopy(SCHEMA)
    if payload is not None:
        from loom.autonomy.plan_evidence import reference_schema

        value["properties"]["source_refs"] = reference_schema(payload)
        value["properties"]["checks"]["items"]["properties"]["source_refs"] = reference_schema(payload)
    value["properties"]["action"]["enum"] = ["propose_stage", "review_stage", "wait", "request_input", "complete_objective"]
    if payload and payload.get("revision_requested"):
        value["properties"]["action"]["enum"].remove("review_stage")
    if payload and payload.get("stage"):
        value["properties"]["action"]["enum"].remove("complete_objective")
    stage = value["properties"]["stage"]
    stage["properties"]["kind"] = {"enum": ["discovery", "delivery"]}
    stage["required"].append("kind")
    goal = stage["properties"]["goals"]["items"]
    for key in ("reuse_goal_id", "reuse_contract_revision"):
        goal["properties"].pop(key)
    variants = []
    for mode, env in envs.items():
        verification = deepcopy(plan_protocol(env["assembly"]).schema)
        verification["properties"]["unresolved_requirements"] = dict(
            verification["properties"]["unresolved_requirements"], maxItems=0,
            description="Must be [] for an executable contract. Unknown project facts belong in discovery scope/assumptions. "
                        "If verification itself cannot be specified, return request_input or wait instead of publishing work.")
        variants.append(obj(dict(key=TEXT, objective=TEXT,
            reason={**TEXT, "description": "Business rationale for this specific work package: the goal gap it addresses and how its output "
                    "contributes to the parent Goal. Exclude planning retries, schema corrections and framework diagnostics."}, acceptance=TEXTS,
            environment={"enum": [mode]}, execution_tools=TEXTS,
            verification=verification,
            estimated_seconds={"type": "integer", "minimum": 30, "maximum": 14400}),
            ["key", "objective", "reason", "acceptance", "environment", "execution_tools", "verification", "estimated_seconds"]))
    goal["properties"]["work_packages"] = dict(type="array", minItems=1, maxItems=3, items={"anyOf": variants})
    goal["required"].append("work_packages")
    # Repair is expressed as another complete proposal, not a second planner.
    value["properties"]["result"]["enum"] = ["completed", "blocked"]
    return value


def validate_proposal(value, payload, envs):
    if isinstance(value, dict) and isinstance(value.get("source_refs"), list) and all(isinstance(r, str) for r in value["source_refs"]):
        validate_evidence_refs(value["source_refs"], {e["id"] for e in payload["evidence"]}, "/source_refs")
    errors = list(Draft202012Validator(schema(envs, payload)).iter_errors(value))
    if errors:
        error = errors[0]
        raise CompileError(error.message, "/" + "/".join(map(str, error.absolute_path)))
    legacy = deepcopy(value)
    if value["action"] == "propose_stage":
        legacy["stage"].pop("kind")
        for goal in legacy["stage"]["goals"]:
            goal.pop("work_packages")
    # Existing reference, coverage, dependency and overlap rules remain authoritative.
    validate(legacy, {**payload, "planning_engine": 2})
    if value["action"] != "propose_stage":
        return
    if payload.get("stage") and any(g["state"] not in {"completed", "cancelled", "blocked", "queued", "active"}
                                   for g in payload.get("existing_goals", []) if g.get("stage_id") == payload["stage"]["id"]):
        raise CompileError("Current stage cannot be revised in this state")
    for gi, goal in enumerate(value["stage"]["goals"]):
        keys = set()
        for wi, work in enumerate(goal["work_packages"]):
            path = f"/stage/goals/{gi}/work_packages/{wi}"
            if work["key"] in keys or not work["acceptance"]:
                raise CompileError("Work keys must be unique and acceptance cannot be empty", path)
            keys.add(work["key"])
            env = envs[work["environment"]]
            if (payload.get("trigger") or {}).get("kind") == "routine" and work["environment"] != "inspect":
                raise CompileError("Adopted routine checks require the inspect environment", path + "/environment")
            required = set(work["execution_tools"])
            for check in work["verification"]["criteria"]:
                required.update(check.get("required_tools", []))
                if check["verifier"] in {"command", "tool_call"}:
                    required.add(check["check"]["tool_id"])
                if check["verifier"] == "visual":
                    required.add("browser_capture")
            missing = required - env["assembly"].available_tool_ids()
            if missing:
                raise CompileError("Unavailable tools in " + work["environment"] + ": " + ", ".join(sorted(missing)), path)
            try:
                AcceptanceController(env["assembly"]).validate_plan(work["verification"])
            except ValueError as exc:
                raise CompileError(str(exc), path + "/verification") from exc
            if work["verification"].get("unresolved_requirements"):
                raise CompileError("Unresolved verification requirements must become a request_input or wait decision", path)
    proposed_checks = [c for g in value["stage"]["goals"] for w in g["work_packages"] for c in w["verification"]["criteria"]]
    for requirement in payload.get("required_verification", []):
        old = requirement["criterion"]
        # A legacy completion/control prerequisite was never a valid evidence
        # obligation. Its business outcome remains inherited by the Goal.
        if old["verifier"] == "tool_call" and any(
                (env["assembly"].bindings.get(old["check"]["tool_id"])) is not None
                and env["assembly"].bindings[old["check"]["tool_id"]].placement == "control" for env in envs.values()):
            continue
        if not any(c["verifier"] == old["verifier"] and evidence_check(c) == evidence_check(old)
                   and set(old.get("required_tools", [])) <= set(c.get("required_tools", [])) for c in proposed_checks):
            raise CompileError(f"Unproven concrete evidence requirement {old['id']} from {requirement['work_package_id']} "
                               "must be retained; semantic assertions cannot replace it", "/stage/goals")


def compile_plan(service, agent, payload, proposal, envs, run_id, now):
    validate_proposal(proposal, payload, envs)
    if proposal["action"] != "propose_stage":
        return None
    old_stage = payload.get("stage")
    sid = old_stage["id"] if old_stage else "stage_" + digest(run_id)[:32]
    revision = old_stage["revision"] + 1 if old_stage else 1
    plan_id = "plan_" + digest([sid, run_id])[:32]
    objective = payload["objective"]
    raw = deepcopy(proposal["stage"])
    goal_proposals = raw.pop("goals")
    stage = dict(raw, id=sid, agent_id=agent["id"], objective_id=objective["id"], revision=revision,
                 state="active", engine_version=2, plan_id=plan_id, role_revision=agent["role_revision"],
                 created_at=old_stage["created_at"] if old_stage else now)
    # Identity follows the requirement, not its display position in a new proposal.
    prior_criteria = {c["text"]: c for c in (old_stage or {}).get("acceptance_criteria", [])}
    stage["contract_revision"] = (old_stage or {}).get("contract_revision", 0) + 1
    stage["acceptance_criteria"] = [deepcopy(prior_criteria[t]) if t in prior_criteria else
        dict(id="criterion_" + digest([sid, t])[:32], owner_id=sid, revision=1, text=t) for t in stage["acceptance"]]
    covered = {(c["criterion_id"], c["criterion_revision"]) for c in raw.get("covers", [])}
    inherit(stage, [c for c in objective_view(agent, objective)["acceptance_criteria"] if (c["id"], c["revision"]) in covered])
    if old_stage:
        # A method revision cannot delete any already committed stage requirement.
        inherit(stage, old_stage["acceptance_criteria"])
    if payload.get("required_revision"):
        inherit(stage, payload["required_revision"]["acceptance_criteria"])
    trigger = payload.get("trigger") or {}
    if trigger:
        from loom.autonomy.acceptance import criteria

        obligations = trigger.get("contract", objective.get("iteration", {})).get("acceptance", [])
        inherit(stage, criteria(objective["id"] + ":trigger:" + trigger["kind"], obligations))
    for index, requested in enumerate(payload.get("requested_goals", [])):
        from loom.autonomy.acceptance import criteria

        inherit(stage, criteria(sid + ":requested:" + str(index), [requested["objective"], *requested["acceptance"]]))
    goals, works, mapping, outputs = [], [], {}, {}
    source_key = digest(payload.get("source_status", {}))
    env_keys = {mode: binding_fingerprint(env["assembly"]) for mode, env in envs.items()}
    for gi, item in enumerate(goal_proposals):
        old_goal = next((g for g in payload.get("existing_goals", []) if g.get("stage_id") == sid
                         and g.get("planning_key") == item["key"] and g["state"] == "completed"), None)
        required_stage = {(c["id"], c["revision"]) for c in stage["acceptance_criteria"]} if gi == len(goal_proposals) - 1 else set()
        proven = {(r["criterion_id"], r["criterion_revision"]) for e in payload.get("evidence", [])
                  if old_goal and e.get("goal_id") == old_goal["id"] and e.get("passed") for r in e.get("verified_criteria", [])}
        if (old_goal and old_goal.get("proposal") == item and old_goal.get("source_key") == source_key
                and old_goal.get("environment_keys") == env_keys and old_goal.get("role_signature") == role_signature_value(agent)
                and old_goal["depends_on"] == [mapping[k] for k in item["depends_on"]]
                and {(c["id"], c["revision"]) for c in old_goal["acceptance_criteria"]} <= proven
                and required_stage <= {(c["id"], c["revision"]) for c in old_goal["acceptance_criteria"]}):
            goals.append(deepcopy(old_goal))
            mapping[item["key"]] = old_goal["id"]
            outputs[item["key"]] = old_goal["output_refs"]
            continue
        gid = "goal_plan_" + digest([sid, item["key"], plan_id])[:32]
        goal = dict(goal_contract(dict(objective=item["objective"], acceptance=item["acceptance"],
                    long_term_objective_id=objective["id"], depends_on=[mapping[k] for k in item["depends_on"]],
                    stop_conditions=stage["stop_conditions"], required_source_ids=stage.get("required_source_ids", [])), agent["role"]),
                    id=gid, agent_id=agent["id"], stage_id=sid, plan_id=plan_id, engine_version=2,
                    revision=1, state="active", created_at=now, producer="unified_planner",
                    planning_key=item["key"], proposal=deepcopy(item), source_key=source_key, environment_keys=env_keys,
                    role_signature=role_signature_value(agent))
        initialize(goal)
        if gi == len(goal_proposals) - 1:
            inherit(goal, stage["acceptance_criteria"])
        # Preserve committed Goal obligations even when the model changes its decomposition.
        if old_stage and gi == len(goal_proposals) - 1:
            for old in payload.get("existing_goals", []):
                if old.get("stage_id") == sid:
                    inherit(goal, old.get("acceptance_criteria", []))
        for wi, item_work in enumerate(item["work_packages"]):
            wid = "work_plan_" + digest([gid, item_work["key"]])[:32]
            env = envs[item_work["environment"]]
            work = dict(id=wid, agent_id=agent["id"], goal_id=gid, stage_id=sid, objective_id=objective["id"],
                        plan_id=plan_id, engine_version=2, contract_revision=1, goal_revision=1,
                        agent_run_id=run_id, role_revision=agent["role_revision"], state="pending", order=wi,
                        objective=item_work["objective"], acceptance=item_work["acceptance"], reason=item_work["reason"],
                        created_at=now, run_ids=[], read_only=item_work["environment"] == "inspect",
                        estimated_seconds=item_work["estimated_seconds"], estimate_basis="model",
                        estimated_tokens=agent["role"]["budget"]["work_tokens"], token_estimate_basis="role_budget_fallback")
            initialize(work)
            if wi == len(item["work_packages"]) - 1:
                inherit(work, goal["acceptance_criteria"])
            plan = AcceptanceController(env["assembly"]).validate_plan(item_work["verification"])
            # validate_plan appends the mandatory full-goal verifier. Its input
            # contains every inherited criterion, even when method checks differ.
            required = set(item_work["execution_tools"])
            for check in plan["criteria"]:
                required.update(check.get("required_tools", []))
                if check["verifier"] in {"command", "tool_call"}:
                    required.add(check["check"]["tool_id"])
                if check["verifier"] == "visual":
                    required.add("browser_capture")
            payload_work = task_payload(agent, work)
            payload_work["task_spec"] = deepcopy(env["validated"]["task_spec"])
            payload_work["objective"] += ("\nStage outcome: " + stage["expected_outcome"] + "\nGoal: " + goal["objective"]
                + "\nScope: " + "; ".join(stage["scope"]) + "\nNon-goals: " + "; ".join(stage["non_goals"])
                + "\nMandatory constraints: " + "; ".join(objective["stage_planning"].get("constraints", [])))
            try:
                AcceptanceController(env["assembly"]).validate_plan(dict(criteria=[c for c in plan["criteria"] if c["id"] != "goal"],
                                                                         unresolved_requirements=[]))
            except ValueError as exc:
                raise CompileError(str(exc), "/stage/goals") from exc
            work.update(required_tools=sorted(required), compiled_payload=payload_work, verification_plan=plan,
                        binding_fingerprint=binding_fingerprint(env["assembly"]))
            if payload.get("prior_attempts"):
                work["prior_attempts"] = deepcopy(payload["prior_attempts"])
            work["input_refs"] = ([dict(kind="verified_result", work_package_id=works[-1]["id"],
                contract_digest=works[-1]["contract_digest"])] if wi else [ref for k in item["depends_on"] for ref in outputs[k]])
            work["expected_outputs"] = [dict(kind="verified_result", name="result")]
            work["contract_digest"] = digest({k: work[k] for k in ("objective", "acceptance_criteria", "compiled_payload",
                "verification_plan", "required_tools", "binding_fingerprint", "input_refs", "expected_outputs")})
            works.append(work)
        outputs[item["key"]] = [dict(kind="verified_result", work_package_id=works[-1]["id"], contract_digest=works[-1]["contract_digest"])]
        goal["output_refs"] = outputs[item["key"]]
        goals.append(goal)
        mapping[item["key"]] = gid
    stage["goal_ids"] = [g["id"] for g in goals]
    stage["roadmap_id"] = "roadmap_" + digest(plan_id)[:32]
    for item in [*goals, *works]:
        if item.get("plan_id") == plan_id:
            item["roadmap_id"] = stage["roadmap_id"]
    roadmap = dict(id=stage["roadmap_id"], agent_id=agent["id"], objective_id=objective["id"], revision=revision,
                   stage_id=sid, baseline=proposal["baseline"], source_refs=proposal["source_refs"], future=proposal.get("future", []),
                   reason=proposal["reason"], created_at=now, input_run_id=run_id)
    return dict(id=plan_id, agent_id=agent["id"], objective_id=objective["id"], stage_id=sid, revision=revision,
                parent_revision=old_stage.get("plan_id") if old_stage else None, created_at=now,
                proposal=proposal, stage=stage, goals=goals, work_packages=works, roadmap=roadmap)


def role_signature_value(agent):
    from loom.autonomy.stages import role_signature

    return role_signature(agent["role"])
