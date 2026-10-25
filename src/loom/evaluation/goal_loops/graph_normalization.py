"""Explicit wire compatibility transformations with an audit for every change.

No repairs to quotes, evidence, observed status or semantic claims belong here.
"""

from copy import deepcopy

from loom.evaluation.goal_loops.graph_contract import UPDATE_FIELDS
from loom.evaluation.issues import RuleViolation, issue, validate_schema


def note(cp, batch_id, kind, raw, rule, **details):
    row = dict(batch_id=batch_id, record_type=kind, record_id=raw.get("card_id", raw.get("key", raw.get('goal_id'))), rule=rule, **details)
    if row not in cp.setdefault("protocol_normalizations", []):
        cp["protocol_normalizations"].append(row)


def normalize_records(cp, records, batch_id):
    errors, items, attributions = [], [], []
    for index, kind, raw in records:
        if not isinstance(raw, dict):
            errors.append(issue("schema.type", "Record must be an object",
                category="format", stage="graph", unit_id=batch_id, record=index, record_index=index + 1, record_type=kind))
            continue
        raw = deepcopy(raw)
        if kind in {"goals", "stages", "attempts"} and "id" in raw:
            if "key" in raw and raw["key"] != raw["id"]:
                errors.append(issue("graph.declaration_alias", "Conflicting declaration id and key", category="format",
                                    stage="graph", unit_id=batch_id, record=index, record_index=index + 1, record_type=kind, path=("key",)))
                continue
            raw["key"] = raw.pop("id")
            note(cp, batch_id, kind, raw, "declaration_id_to_key")
        # Compatibility defaults may look up prior state by identity. Validate
        # those keys before lookup; malformed model IDs are not host exceptions.
        properties = UPDATE_FIELDS[kind]["items"]["properties"]
        try:
            validate_schema(raw, dict(type="object", properties={k: properties[k] for k in ("key", "card_id") if k in properties}))
        except RuleViolation as exc:
            errors.append({**exc.issue, "stage": "graph", "unit_id": batch_id, "record": index,
                           "record_index": index + 1, "record_type": kind})
            continue
        if kind == "goals" and any(g["id"] == raw.get("key") and not g.get("parent_goal_id") for g in cp["entities"]["goals"]):
            # Root is recorded task metadata, never an evaluator-owned declaration.
            # Ignore echoes (including paraphrases) without applying any mutation.
            note(cp, batch_id, kind, raw, "root_echo_ignored")
            continue
        if kind == "goals" and "root_relation" not in raw:
            raw["root_relation"] = ""
            note(cp, batch_id, kind, raw, "missing_parent_relation_unreviewed")
        if kind == "goals" and "conditions" not in raw:
            key = cp.get("record_aliases", {}).get(batch_id, {}).get(raw.get("key"), raw.get("key"))
            old = next((g for g in cp["entities"]["goals"] if g["id"] == key), None)
            raw["conditions"] = deepcopy(old["conditions"]) if old else []
            note(cp, batch_id, kind, raw, "omitted_conditions_preserved_or_empty")
        if kind == "assessments":
            # Backward-compatible input; semantic judgment is independent of its
            # possibly broken goal/stage mapping.
            stage_ids = raw.pop("stage_ids", None)
            raw.setdefault("expected_refs", [])
            raw.setdefault("expected", "unknown")
            if stage_ids:
                attributions.append((index, "attributions", dict(card_id=raw.get("card_id"), stage_ids=stage_ids, refs=raw.get("refs", []))))
            prior, uptake = raw.pop("prior_card_ids", []), raw.pop("uptake_card_ids", [])
            redundancy = raw.pop("redundancy", "not_established")
            if prior or uptake or redundancy != "not_established":
                attributions.append((index, "comparisons", dict(card_id=raw.get("card_id"), prior_card_ids=prior, uptake_card_ids=uptake,
                                                                 redundancy=redundancy, refs=raw.get("refs", []))))
        if kind == "stages":
            raw.setdefault("attempt_id", "")
        if kind == 'closures' and raw.get('evidence_state') == 'unknown' and 'supported_conditions' not in raw:
            # Unknown support asserts no satisfied conditions. Never infer a
            # missing positive claim for a supported/partial closure.
            raw['supported_conditions'] = []
            note(cp, batch_id, kind, raw, 'unknown_closure_empty_support')
        items.append((index, kind, raw))
    items += attributions

    return items, errors


def terminal_metadata_records(records, *, normalizations=None, batch_id=None):
    """Metadata-only review cannot verify hidden outputs. Preserve that uncertainty
    instead of repeatedly fetching source text or asking the model to repair it.
    """
    result = []
    unclear = {raw.get("card_id") for kind, raw in records if kind == "assessments"
               and isinstance(raw, dict) and isinstance(raw.get("card_id"), str) and (raw.get("status") == "needs_evidence"
                                           or (raw.get("status") == "reviewed" and raw.get("refs") == []))}
    for kind, raw in records:
        if isinstance(raw, dict) and "card_id" in raw and not isinstance(raw["card_id"], str):
            result.append((kind, deepcopy(raw)))
            continue
        if kind == "evidence_requests" or (kind == "attributions" and isinstance(raw, dict) and raw.get("card_id") in unclear):
            continue
        raw = deepcopy(raw)
        if kind == "assessments" and isinstance(raw, dict) and raw.get("card_id") in unclear:
            if raw.get("refs") == []:
                raw.update(purpose="Action judgment lacks cited evidence", delta="Contribution not established: no evidence references supplied",
                           expected="unknown", expected_refs=[])
                for field in ("prior_card_ids", "uptake_card_ids", "redundancy"):
                    raw.pop(field, None)
                if normalizations is not None:
                    note = dict(batch_id=batch_id, record_type=kind, record_id=raw.get("card_id"), rule="uncited_judgment_retained_as_unknown")
                    if note not in normalizations:
                        normalizations.append(note)
            raw.update(status="unknown", contribution="unknown", necessity="unknown")
            raw.pop("stage_ids", None)
        result.append((kind, raw))
    return result
