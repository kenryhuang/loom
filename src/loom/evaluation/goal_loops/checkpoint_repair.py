"""Reconcile saved failures through explicit, versioned compatibility rules."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.evaluation.goal_loops.attribution import root_owned_card
from loom.evaluation.goal_loops.coverage import window_issues
from loom.evaluation.goal_loops.graph_contract import UPDATE_FIELDS
from loom.evaluation.goal_loops.graph_replay import replay_saved_response
from loom.evaluation.goal_loops.graph_updates import commit_updates, parse_updates
from loom.evaluation.goal_loops.judge_metadata import VERSION, terminal_metadata_records
from loom.evaluation.legacy_issues import graph_issue
from loom.llm.api import LlmResponse

MISSING = "Cards need a terminal assessment (reviewed or explicit unknown)"


def complete_framework_boundaries(cp, cards, unit):
    """Host events need no invented semantic utility judgment when omitted."""
    recovered = []
    for card in cards:
        if card["id"] not in unit["cards"] or card["id"] in cp["assessments"] or card["kind"] != "boundary" or not root_owned_card(card):
            continue
        row = dict(card_id=card["id"], status="unknown", purpose=f"Framework event: {card['name']}",
                   delta="Recorded framework transition; semantic contribution not assessed",
                   contribution="unknown", necessity="unknown", refs=[])
        errors, _ = commit_updates(cp, [("assessments", row)], batch_id=unit["id"], cards=cards, delivered={},
                                   cutoff=unit["cutoff"], target_ids=set(unit["cards"]))
        if not errors:
            recovered.append(card["id"])
            cp.setdefault("protocol_normalizations", []).append(dict(
                batch_id=unit["id"], record_type="assessments", record_id=card["id"], rule="framework_boundary_recorded_without_semantic_judgment"))
    return recovered


def reconcile_checkpoint(cp, cards, *, metadata=None):
    """Replay supported validator upgrades, then apply legacy metadata recovery.

    Versioned replay uses only originally delivered references. Current-validator
    failures remain pending; legacy recovery cannot authorize unseen evidence.
    """
    latest = {r["batch_id"]: r for r in cp.get("responses", [])}
    for unit in cp.get("units", {}).values():
        unit["feedback"] = [graph_issue(e) for e in unit.get("feedback", [])]
        if unit.get("complete"):
            continue
        response = latest.get(unit["id"])
        if response and replay_saved_response(cp, unit, response, cards):
            continue
        if response and response.get("revalidated_with", response.get("validation_version")):
            # Versioned responses require a supported replay with original
            # delivery. Legacy recovery must not bypass this boundary.
            continue
        records = parse_updates(LlmResponse(content=response["text"], finish_reason=response.get("finish_reason")))[0] if response else []
        recovered_boundaries = complete_framework_boundaries(cp, cards, unit)
        declaration_errors = [e for e in unit.get("feedback", []) if e.get("record_type") == "goals"
                              and e.get("code") == "graph.goal_definition_immutable"]
        replayed = False
        if (declaration_errors and metadata is not None and response
                and (response.get("delivered_ref_ids") is not None or cp.get("input_policy", {}).get("version") == VERSION)):
            # Reconstruct only the bounded card fields for the current metadata
            # version. Never authorize arbitrary refs from the full registry.
            registry = cp["extraction"]["registry"]
            ids = {ref for cid in unit["cards"] for ref in metadata[cid]["refs"]}
            allowed = response.get("delivered_ref_ids", ids)
            delivered = {ref: registry[ref] for ref in ids.intersection(allowed) if ref in registry}
            replay = [(kind, row) for kind, row in records if kind in {"goals", "stages", "attributions"}]
            errors, _ = commit_updates(cp, replay, batch_id=unit["id"], cards=cards, delivered=delivered,
                                       cutoff=unit["cutoff"], target_ids=set(unit["cards"]))
            replayed = bool(replay) and not errors
        candidates = [(kind, row) for kind, row in records if kind == "assessments" and isinstance(row, dict)
                      and row.get("card_id") in unit["cards"] and row.get("card_id") not in cp["assessments"]
                      and row.get("status") == "reviewed" and row.get("refs") == []]
        converted = terminal_metadata_records(candidates, normalizations=cp.setdefault("protocol_normalizations", []), batch_id=unit["id"])
        commit_updates(cp, converted, batch_id=unit["id"], cards=cards, delivered={}, cutoff=unit["cutoff"], target_ids=set(unit["cards"]))
        recovered = {row["card_id"] for _, row in candidates if row.get("card_id") in cp["assessments"]} | set(recovered_boundaries)
        omitted = {row.get("card_id") for kind, row in records if kind == "attributions" and isinstance(row, dict)
                   and row.get("card_id") in recovered and row.get("refs") == []
                   and Draft202012Validator(UPDATE_FIELDS[kind]["items"]).is_valid(row)}
        # Atomic semantic commits replace nested state; always reacquire it.
        unit = cp["units"][unit["id"]]
        coverage = window_issues(cp, unit)
        retained = []
        for error in unit.get("feedback", []):
            if replayed and error in declaration_errors:
                continue
            if error.get("category") == "coverage":
                continue
            if error.get("record_type") == "assessments" and error.get("record_id") in recovered:
                continue
            if (error.get("record_type") == "attributions" and error.get("record_id") in omitted
                    and error.get("code") in {"graph.assessment_dependency", "graph.relationship_evidence_required"}):
                continue
            retained.append(error)
        retained = coverage + retained
        completed = not retained
        if recovered or retained != unit.get("feedback", []):
            cp.setdefault("checkpoint_reconciliations", []).append(dict(
                batch_id=unit["id"], recovered_unknown_cards=sorted(recovered), complete=completed,
                removed_issues=len(unit.get("feedback", [])) - len(retained), model_calls=0,
            ))
            unit.update(feedback=retained, complete=completed)
            cp["conflicts"] = [e for e in cp["conflicts"] if e["batch_id"] != unit["id"]]
            cp["conflicts"] += [dict(batch_id=unit["id"], **deepcopy(e)) for e in retained]
