"""Independent semantic records; action judgments and graph attribution commit separately."""

from copy import deepcopy

from jsonschema import Draft202012Validator, ValidationError

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.graph_contract import GROUP, UPDATE_FIELDS
from loom.evaluation.goal_loops.graph_state import commit_group
from loom.evaluation.goal_loops.structure import commit_structure
from loom.llm.api import LlmResponse
from loom.llm.structured import parse_response


def parse_updates(answer):
    """Accept flat structured JSON or independent JSONL records, including complete
    lines before a truncated tail. Never synthesize missing braces or field values.
    Legacy group envelopes remain readable, but no longer define atomicity.
    """
    records, errors = [], []

    def unpack(value):
        if not isinstance(value, dict):
            raise ValueError("Expected an update object")
        if "type" in value:
            kind = value["type"]
            if kind not in UPDATE_FIELDS:
                raise ValueError(f"Unknown update type: {kind}")
            records.append((kind, {k: v for k, v in value.items() if k != "type"}))
            return
        if "groups" in value:
            groups = value["groups"]
            if not isinstance(groups, list) or len(groups) > 8:
                raise ValueError("Expected at most eight legacy groups")
            for group in groups:
                try:
                    unpack(group)
                except ValueError as exc:
                    errors.append(dict(reason=str(exc)))
        for kind, rows in value.items():
            if kind == "groups":
                continue
            if kind not in UPDATE_FIELDS:
                errors.append(dict(record_type=kind, reason="Unknown update field"))
                continue
            if not isinstance(rows, list) or len(rows) > UPDATE_FIELDS[kind]["maxItems"]:
                errors.append(dict(record_type=kind, reason="Invalid update array"))
                continue
            records.extend((kind, row) for row in rows)

    try:
        value, _ = parse_response(answer)
        unpack(value)
    except ValueError as whole_error:
        # JSONL records are self-contained. Pretty-printed fragments of malformed
        # enclosing JSON are deliberately not treated as independent assertions.
        lines = (answer.content or "").splitlines()
        if len(answer.content or "") > 1_000_000 or len(lines) > 256 or answer.finish_reason in {"refusal", "content_filter"}:
            return [], [dict(reason=str(whole_error))]
        recognized = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value, _ = parse_response(LlmResponse(content=line, finish_reason="stop"))
                if not isinstance(value, dict) or "type" not in value:
                    raise ValueError("Expected a complete typed record on this line")
                unpack(value)
                recognized = True
            except ValueError as exc:
                errors.append(dict(response_line=index + 1, reason=str(exc)[:300]))
        if not recognized:
            return [], [dict(reason=str(whole_error)[:450])]
        if answer.finish_reason in {"length", "max_tokens"}:
            errors.append(dict(reason="Response truncated; complete independent records retained"))
    if len(records) > 128:
        return [], [dict(reason="Too many update records")]
    return records, errors


def commit_updates(cp, records, *, batch_id, cards, delivered, cutoff, target_ids):
    """Commit each valid record without rolling back independent siblings.

    Declarations are dependency ordered; action assessments need no declared goal.
    Attribution and causal links are separately validated, never inferred from
    sequence/caller metadata alone. Alias mappings are local to one review unit.
    """
    errors, requests, items, attributions = [], [], [], []

    def normalized(kind, raw, rule, **details):
        row = dict(batch_id=batch_id, record_type=kind, record_id=raw.get("card_id", raw.get("key")), rule=rule, **details)
        if row not in cp.setdefault("protocol_normalizations", []):
            cp["protocol_normalizations"].append(row)

    for index, (kind, raw) in enumerate(records):
        if not isinstance(raw, dict):
            errors.append(dict(record=index, record_type=kind, reason="Record must be an object"))
            continue
        raw = deepcopy(raw)
        if kind in {"goals", "stages", "attempts"} and "id" in raw:
            if "key" in raw and raw["key"] != raw["id"]:
                errors.append(dict(record=index, record_type=kind, reason="Conflicting declaration id and key"))
                continue
            raw["key"] = raw.pop("id")
            normalized(kind, raw, "declaration_id_to_key")
        if kind == "goals" and any(g["id"] == raw.get("key") and not g.get("parent_goal_id") for g in cp["entities"]["goals"]):
            # Root is recorded task metadata, never an evaluator-owned declaration.
            # Ignore echoes (including paraphrases) without applying any mutation.
            normalized(kind, raw, "root_echo_ignored")
            continue
        if kind == "goals" and "root_relation" not in raw:
            raw["root_relation"] = ""
            normalized(kind, raw, "missing_parent_relation_unreviewed")
        if kind == "goals" and "conditions" not in raw:
            key = cp.get("record_aliases", {}).get(batch_id, {}).get(raw.get("key"), raw.get("key"))
            old = next((g for g in cp["entities"]["goals"] if g["id"] == key), None)
            raw["conditions"] = deepcopy(old["conditions"]) if old else []
            normalized(kind, raw, "omitted_conditions_preserved_or_empty")
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
        items.append((index, kind, raw))
    items += attributions

    def failure(index, kind, raw, exc):
        return dict(record=index, record_type=kind, record_id=raw.get("card_id", raw.get("key")),
                    path=list(exc.absolute_path) if isinstance(exc, ValidationError) else [],
                    reason=(exc.message if isinstance(exc, ValidationError) else str(exc))[:450])

    def apply(item):
        index, kind, raw = item
        Draft202012Validator(UPDATE_FIELDS[kind]["items"]).validate(raw)
        if kind == "evidence_requests":
            requests.append(raw)
            return
        aliases = cp.setdefault("record_aliases", {}).setdefault(batch_id, {})
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
                        normalized(kind, raw, "goal_title_echo_ignored", retained_objective=old["objective"],
                                   proposed_objective=row["objective"])
                        row["objective"] = old["objective"]
                elif old["objective"] != row["objective"]:
                    # An explicit new objective + conditions under an occupied
                    # ID creates a new object. Only this batch's references move.
                    forked_from = old["id"]
                    row["key"] = "new-goal:" + identity(batch_id, raw)
        if kind == "stages" and row["key"] in cp["stages"]:
            old = cp["stages"][row["key"]]
            remapped_goal = aliases.get(old["goal_id"], old["goal_id"])
            if row["goal_id"] != old["goal_id"] and row["goal_id"] == remapped_goal:
                forked_from = old["id"]
                row["key"] = "new-stage:" + identity(batch_id, raw, row["goal_id"])
        if kind == "stages" and row["key"] in cp["stages"]:
            old = cp["stages"][row["key"]]
            if old["knowledge_cutoff"] <= cutoff and all(old[k] == row[k] for k in ("goal_id", "title", "kind")) and old["attempt_id"] == (
                row["attempt_id"] or "attempt:" + identity(row["goal_id"], "unestablished")
            ):
                normalized(kind, raw, "unchanged_stage_reused")
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
                raise ValueError("Relationship awaits an accepted action assessment")
            if not row["refs"]:
                if kind == "attributions":
                    normalized(kind, raw, "unsupported_attribution_omitted")
                    return
                raise ValueError("Relationship requires delivered evidence")
            if any(key not in delivered or delivered[key]["line"] > cutoff for key in row["refs"]):
                raise ValueError("Relationship cites unknown, undelivered or future evidence")
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
                normalized(kind, raw, "declaration_forked", previous_id=forked_from, new_id=canonical)

    # Retry declarations only when a dependency was actually resolved; unrelated
    # malformed declarations cannot prevent other goals or stages from committing.
    declarations = [item for item in items if item[1] in {"goals", "attempts", "stages"}]
    while declarations:
        rejected, accepted = [], 0
        for item in declarations:
            try:
                apply(item)
                accepted += 1
            except (ValueError, KeyError, ValidationError) as exc:
                rejected.append((item, failure(*item, exc)))
        if not accepted:
            errors.extend(e for _, e in rejected)
            break
        declarations = [item for item, _ in rejected]
    for kind in ("assessments", "attributions", "comparisons", "relations", "closures", "diagnostics", "structure", "evidence_requests"):
        for item in (item for item in items if item[1] == kind):
            try:
                apply(item)
            except (ValueError, KeyError, ValidationError) as exc:
                errors.append(failure(*item, exc))
    if len(requests) > 3:
        errors.append(dict(record_type="evidence_requests", reason="At most three evidence requests per call"))
        requests = []
    return errors, requests
