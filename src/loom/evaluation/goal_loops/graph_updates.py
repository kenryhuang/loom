"""Independent semantic records; action judgments and graph attribution commit separately."""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.graph_contract import GROUP, UPDATE_FIELDS
from loom.evaluation.goal_loops.graph_decode import parse_updates
from loom.evaluation.goal_loops.graph_normalization import normalize_records, note
from loom.evaluation.goal_loops.graph_state import commit_group
from loom.evaluation.goal_loops.structure import commit_structure
from loom.evaluation.issues import RuleViolation, issue, validate_schema

__all__ = ["parse_updates", "commit_updates"]
VALIDATION_VERSION = "graph-validation.3"


def commit_updates(cp, records, *, batch_id, cards, delivered, cutoff, target_ids, allowed_fields=None, visible_ids=None):
    """Commit each valid record without rolling back independent siblings.

    Declarations are dependency ordered; action assessments need no declared goal.
    Attribution and causal links are separately validated, never inferred from
    sequence/caller metadata alone. Alias mappings are local to one review unit.
    """
    allowed = set(UPDATE_FIELDS) if allowed_fields is None else set(allowed_fields)
    admitted, rejected = [], []
    for index, (kind, raw) in enumerate(records):
        if kind not in allowed:
            rejected.append(issue("response.record_type", "Record type is not allowed in this request", category="format",
                                  phase="decode", stage="graph", unit_id=batch_id, record_type=kind,
                                  record=index, record_index=index + 1, details=dict(allowed_types=sorted(allowed))))
        else:
            admitted.append((index, kind, raw))
    items, errors = normalize_records(cp, admitted, batch_id)
    errors = rejected + errors
    requests = []

    def failure(index, kind, raw, exc):
        return {**exc.issue, "stage": "graph", "unit_id": batch_id,
                "record": index, "record_index": index + 1, "record_type": kind,
                "record_id": raw.get("card_id", raw.get("key", raw.get("goal_id")))}

    def apply(item):
        index, kind, raw = item
        validate_schema(raw, UPDATE_FIELDS[kind]["items"])
        if kind == "evidence_requests":
            requests.append(raw)
            return
        aliases = cp.setdefault("record_aliases", {}).setdefault(batch_id, {})
        if kind == 'reuse':
            if visible_ids is not None and raw['target_id'] not in visible_ids:
                raise RuleViolation('graph.reuse_scope', 'Reused object was not delivered in this request', path=('target_id',))
            objects = cp['stages'] if raw['kind'] == 'stage' else {r['id']: r for r in cp['entities'][raw['kind'] + 's']}
            target = objects.get(raw['target_id'])
            if target is None or target.get('knowledge_cutoff', target.get('origin_line', 0)) > cutoff:
                raise RuleViolation('graph.reuse_scope', 'Reused object is unknown or outside this time boundary', path=('target_id',))
            previous = aliases.get(raw['key'])
            if previous and previous != raw['target_id']:
                raise RuleViolation('graph.reuse_alias', 'An existing local alias cannot be redirected by reuse', path=('key',))
            aliases[raw['key']] = raw['target_id']
            note(cp, batch_id, kind, raw, 'object_reused', target_id=raw['target_id'])
            return
        row = deepcopy(raw)
        for key in ("key", "parent_id", "goal_id", "attempt_id", "source_id", "target_id"):
            if key in row:
                row[key] = aliases.get(row[key], row[key])
        forked_from = None
        if kind == "goals":
            old = next((g for g in cp["entities"]["goals"] if g["id"] == row["key"]), None)
            if old and old.get("parent_goal_id") == row["parent_id"] and old.get("knowledge_cutoff", 0) <= cutoff:
                if old["conditions"] == row["conditions"] and (old["conditions"] or old["objective"] == row["objective"]):
                    # A repeated definition is not an edit operation. Keep the
                    # canonical wording; conditions and parent must still match.
                    if old["objective"] != row["objective"]:
                        note(cp, batch_id, kind, raw, "goal_title_echo_ignored", retained_objective=old["objective"],
                                   proposed_objective=row["objective"])
                        row["objective"] = old["objective"]
                elif old["objective"] != row["objective"]:
                    # An explicit new objective + conditions under an occupied
                    # ID creates a new object. Only this batch's references move.
                    forked_from = old["id"]
                    row["key"] = "new-goal:" + identity(batch_id, raw)
        if kind == "stages" and row["key"] in cp["stages"]:
            old = cp["stages"][row["key"]]
            attempt = row["attempt_id"] or "attempt:" + identity(row["goal_id"], "unestablished")
            if (row["goal_id"], attempt, row["kind"]) != (old["goal_id"], old["attempt_id"], old["kind"]):
                # A declaration creates/reuses an identity; it is not an edit.
                # Preserve the old stage and move only this batch's alias.
                forked_from = old["id"]
                row["key"] = "new-stage:" + identity(batch_id, raw, row["goal_id"], attempt)
        if kind == "stages" and row["key"] in cp["stages"]:
            old = cp["stages"][row["key"]]
            if old["knowledge_cutoff"] <= cutoff and all(old[k] == row[k] for k in ("goal_id", "title", "kind")) and old["attempt_id"] == (
                row["attempt_id"] or "attempt:" + identity(row["goal_id"], "unestablished")
            ):
                note(cp, batch_id, kind, raw, "unchanged_stage_reused")
                return
        if kind == "structure":
            commit_structure(cp, row, batch_id=batch_id, delivered=delivered, cutoff=cutoff, cards=cards, target_ids=target_ids)
            return
        field = kind
        validation_evidence = delivered
        if kind == "assessments":
            old = cp["assessments"].get(row["card_id"], {})
            row["stage_ids"] = old.get("stage_ids", []) if row["status"] != "unknown" else []
            row.update(prior_card_ids=[], uptake_card_ids=[], redundancy="not_established")
        elif kind in {"attributions", "comparisons"}:
            old = cp["assessments"].get(row["card_id"])
            if not old:
                raise RuleViolation("graph.assessment_dependency", "Relationship awaits an accepted action assessment", path=("card_id",), category="semantic")
            if not row["refs"]:
                if kind == "attributions":
                    note(cp, batch_id, kind, raw, "unsupported_attribution_omitted")
                    return
                raise RuleViolation("graph.relationship_evidence_required", "Relationship requires delivered evidence", path=("refs",), category="semantic")
            if any(key not in delivered or delivered[key]["line"] > cutoff for key in row["refs"]):
                raise RuleViolation("graph.relationship_evidence_scope", "Relationship cites unknown, undelivered or future evidence",
                    path=("refs",), category="semantic")
            # Revalidate the saved judgment with its already accepted evidence.
            # This host-only ledger cannot authorize new model assertions: their
            # references were checked against this request's delivery above.
            validation_evidence = dict(delivered)
            for keys, pointers in ((old["refs"], old["evidence_refs"]),
                                   (old.get("expected_refs", []), old.get("expected_evidence_refs", []))):
                for key, pointer in zip(dict.fromkeys(keys), pointers, strict=True):
                    if pointer["line_number"] <= min(cutoff, old["knowledge_cutoff"]):
                        validation_evidence.setdefault(key, dict(id=key, ref=pointer, line=pointer["line_number"]))
            field = "assessments"
            row = {k: deepcopy(old[k]) for k in GROUP["properties"]["assessments"]["items"]["properties"]}
            if kind == "attributions":
                row["stage_ids"] = [aliases.get(s, s) for s in raw["stage_ids"]]
            else:
                row.update({key: raw[key] for key in ("prior_card_ids", "uptake_card_ids", "redundancy")})
            row["refs"] = list(dict.fromkeys([*row["refs"], *raw["refs"]]))
        group = {k: [] for k in GROUP["properties"]}
        group[field] = [row]
        # A relationship replay after a declaration fork must be applied to its
        # new targets, even if the original raw alias text was already accepted.
        fingerprint = [batch_id, kind, raw]
        if forked_from:
            fingerprint.append(row["key"])
        elif kind == "goals" and any(n.get("rule") == "declaration_forked" and n.get("batch_id") == batch_id
                                     and n.get("record_id") == raw["key"] and n.get("new_id") == row["key"]
                                     for n in cp.get("protocol_normalizations", [])):
            fingerprint.append("new-goal:" + identity(batch_id, raw))
        elif kind == "attributions":
            fingerprint.append(row["stage_ids"])
        identifier = "record:" + identity(*fingerprint)
        commit_group(cp, group, group_id=identifier, base_revision=cp["graph_revision"], cards=cards,
                     delivered=validation_evidence, cutoff=cutoff, target_ids=target_ids)
        if kind in {"goals", "stages", "attempts"}:
            prefix = {"goals": "goal", "stages": "stage", "attempts": "attempt"}[kind]
            known = cp["stages"] if kind == "stages" else {g["id"] for g in cp["entities"][kind]}
            canonical = row["key"] if row["key"] in known else prefix + ":" + identity(identifier, row["key"])
            cp.setdefault("record_aliases", {}).setdefault(batch_id, {})[raw["key"]] = canonical
            if forked_from:
                cp["record_aliases"][batch_id][forked_from] = canonical
                note(cp, batch_id, kind, raw, "declaration_forked", previous_id=forked_from, new_id=canonical)

    # Retry declarations only when a dependency was actually resolved; unrelated
    # malformed declarations cannot prevent other goals or stages from committing.
    declarations = sorted([item for item in items if item[1] in {"reuse", "goals", "attempts", "stages"}], key=lambda item: item[1] != 'reuse')
    while declarations:
        rejected, accepted = [], 0
        for item in declarations:
            try:
                apply(item)
                accepted += 1
            except RuleViolation as exc:
                rejected.append((item, failure(*item, exc)))
        if not accepted:
            errors.extend(e for _, e in rejected)
            break
        declarations = [item for item, _ in rejected]
    for kind in ("assessments", "attributions", "comparisons", "relations", "closures", "diagnostics", "structure", "evidence_requests"):
        for item in (item for item in items if item[1] == kind):
            try:
                apply(item)
            except RuleViolation as exc:
                errors.append(failure(*item, exc))
    if len(requests) > 3:
        errors.append(issue("response.evidence_request_limit", "At most three evidence requests per call",
            category="format", record_type="evidence_requests", stage="graph", unit_id=batch_id))
        requests = []
    return errors, requests
