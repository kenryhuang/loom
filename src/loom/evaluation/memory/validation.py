"""Pure record rules: shape → scope → citations → epistemic status.

The only normalization attaches known evidence owners; it never changes claims,
quotes, ratings or observed/proposed status. No checkpoint writes or LLM calls.
"""

from copy import deepcopy

from loom.evaluation.goal_loops.packing import serialized
from loom.evaluation.issues import RuleViolation, validate_schema
from loom.evaluation.memory.contracts import FIELDS
from loom.evaluation.memory.evidence import sanitize


def validate_record(kind, row, unit):
    validate_schema(row, FIELDS[kind]["items"])
    if sanitize(row) != row or "[REDACTED]" in serialized(row):
        raise RuleViolation("memory.credentials", "Memory must not contain credentials or redacted values", path=())
    stages = {s["id"]: s for s in unit["scope"]["stages"]}
    goals = {g["id"] for g in unit["scope"]["goals"]}
    actions = {c["id"] for c in unit["scope"]["actions"]}
    if kind == "contributions":
        if not stages:
            raise RuleViolation("memory.stage_scope_empty", "No stages were delivered in this window; return contributions=[] instead of inventing a stage ID",
                path=("stage_id",))
        if row["stage_id"] not in stages or row["goal_id"] != stages[row["stage_id"]]["goal_id"]:
            raise RuleViolation("memory.stage_owner", "Contribution must target the delivered stage and its owning goal", path=("stage_id",))
        if not set(row["key_action_ids"]) <= actions:
            raise RuleViolation("memory.key_action_scope", "Unknown key action", path=("key_action_ids",))
        for cid in row["key_action_ids"]:
            action = next(c for c in unit["scope"]["actions"] if c["id"] == cid)
            if not action.get('framework', {}).get('stage_key_action', action.get('kind') != 'boundary'):
                raise RuleViolation("memory.key_action_framework", "Framework records and root-owned actions cannot be stage key actions",
                                    path=("key_action_ids",))
            if not any(m["stage_id"] == row["stage_id"] for m in action["memberships"]):
                raise RuleViolation("memory.key_action_membership", "Key action is not attributed to this stage", path=("key_action_ids",))
        if row["rating"] == "key" and not row["key_action_ids"]:
            raise RuleViolation("memory.key_actions_required", "Key contribution requires key actions", path=("key_action_ids",))
    else:
        for field, allowed in (("goal_ids", goals), ("stage_ids", set(stages)), ("action_ids", actions)):
            if not set(row[field]) <= allowed:
                raise RuleViolation("memory.scope", "Memory refers to undelivered goal, stage or action", path=(field,))
        if not row["goal_ids"]:
            raise RuleViolation("memory.goal_required", "Memory needs a goal scope", path=("goal_ids",))
    evidence = {e["id"]: e for e in unit["evidence"]}
    refs, cited = [], []
    for index, citation in enumerate(row["citations"], 1):
        entry = evidence.get(citation["ref"])
        if not entry:
            raise RuleViolation("memory.citation_undelivered", f"Citation {index}: ref {citation['ref']} was not delivered in this memory request",
                                path=("citations", index - 1, "ref"), evidence_id=citation["ref"])
        if not citation["quote"].strip() or citation["quote"] not in entry["text"]:
            raise RuleViolation("memory.citation_not_literal", f"Citation {index}, ref {citation['ref']}: must quote evidence verbatim. "
                             "Copy a short unchanged substring from this ref's text; preserve newlines, spaces and comment markers. "
                             "If unsupported, omit this record.", path=("citations", index - 1, "quote"), evidence_id=citation["ref"])
        if entry.get("quotes") and not any(citation["quote"] in q for q in entry["quotes"]):
            raise RuleViolation("memory.synthesis_quote", "Synthesis citation must stay inside an original quote", path=("citations", index - 1, "quote"))
        cited.append(entry)
        if entry["ref"] not in refs:
            refs.append(entry["ref"])
    if not refs and not (kind == "contributions" and row["rating"] == "unknown"):
        raise RuleViolation("memory.evidence_required", "Memory claim requires source evidence", path=("citations",))
    record = deepcopy(row)
    if kind != "contributions":
        owners = {e["action_id"] for e in cited}
        if not owners <= actions:
            raise RuleViolation("memory.evidence_owner_scope", "Cited evidence owner is outside the delivered action scope", path=("action_ids",))
        # Evidence ownership is host metadata, not another semantic judgment for
        # the model to reproduce. In particular, verification boundaries often
        # support a fact alongside the tool actions the model lists explicitly.
        record["action_ids"] = sorted(set(row["action_ids"]) | owners)
        if len(record["action_ids"]) > FIELDS[kind]["items"]["properties"]["action_ids"]["maxItems"]:
            raise RuleViolation("memory.action_limit", "Memory action scope exceeds the record limit", path=("action_ids",))
        if added := sorted(owners - set(row["action_ids"])):
            record["normalizations"] = [dict(rule="include_cited_evidence_owners", added_action_ids=added)]
        observed = (
            kind == "facts"
            and row["basis"] == "observed"
            or kind == "lessons"
            and row["validation"] == "observed_recovery"
            or kind == "workflows"
            and row["validation"] == "observed"
        )
        observation_bases = {"observed_output"}
        if kind == "facts":
            observation_bases.add("observed_runtime_state")
        if observed and not any(e["basis"] in observation_bases for e in cited):
            requirement = "recorded tool output or runtime-state evidence" if kind == "facts" else "recorded tool output"
            raise RuleViolation("memory.observation_required", f"Observed memory requires {requirement}; intent alone is not observation",
                path=("citations",))
    elif cited:
        related = {a["id"] for a in unit["scope"]["actions"] if any(m["stage_id"] == row["stage_id"] for m in a["memberships"])}
        if not any(e["action_id"] in related for e in cited):
            raise RuleViolation("memory.stage_evidence", "Contribution needs evidence from its stage's actions", path=("citations",))
    return {**record, "evidence_refs": deepcopy(refs)}
