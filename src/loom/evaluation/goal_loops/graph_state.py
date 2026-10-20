"""Atomic semantic groups over immutable cards; no mandatory synthetic loops."""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.review import initial_state


def revised(previous, row):
    """An earlier retry adds history without erasing a later supported judgment."""
    if not previous:
        return row
    versions = previous.get("history", []) + [{k: v for k, v in previous.items() if k != "history"}, row]
    versions.sort(key=lambda r: r["knowledge_cutoff"])
    return {**versions[-1], "history": versions[:-1]}


def initialize(view):
    return dict(
        entities=initial_state(view),
        stages={},
        assessments={},
        closures={},
        relations=[],
        diagnostics=[],
        applied_groups=[],
        revisions=[],
        graph_revision=0,
        conflicts=[],
        cursor=0,
        reviewed_ranges=[],
        usage=dict(calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0, unreported_calls=0),
        reservations=[],
        responses=[],
    )


def occurrences(cp, cards):
    """Occurrence identity is a source card plus stable stage, never a mutable title."""
    result = []
    for card in cards:
        row = cp["assessments"].get(card["id"])
        if not row:
            continue
        for sid in row["stage_ids"]:
            stage = cp["stages"][sid]
            result.append(
                dict(
                    id="phase:" + identity(sid, card["id"]),
                    stage_id=sid,
                    goal_id=stage["goal_id"],
                    attempt_id=stage["attempt_id"],
                    kind=stage["kind"],
                    question=stage["title"],
                    known_before=row["expected"],
                    actions=row["purpose"],
                    observations=row["delta"],
                    delta=row["delta"],
                    remaining_questions="",
                    next_reason="",
                    allocation="serves",
                    call_ids=card["call_ids"],
                    card_ids=[card["id"]],
                    evidence_refs=row["evidence_refs"],
                    basis="inferred",
                    confidence="unknown",
                    start_line=card["start_line"],
                    end_line=card["end_line"],
                    contribution=row["contribution"],
                    status=card["status"],
                )
            )
    return result


def commit_group(cp, group, *, group_id, base_revision, cards, delivered, cutoff, target_ids):
    if group_id in cp["applied_groups"]:
        return False
    if cp["graph_revision"] != base_revision:
        raise ValueError("Stale graph revision")
    # Transactions copy semantic state, not raw extraction, responses or request
    # accounting. Independent records must not repeatedly copy the entire trace.
    state = dict(cp)
    for key in ("entities", "stages", "assessments", "closures"):
        state[key] = deepcopy(cp[key])
    for key in ("relations", "diagnostics", "applied_groups", "revisions"):
        state[key] = list(cp[key])
    entities = state["entities"]
    objects = {r["id"]: r for kind in ("goals", "attempts") for r in entities[kind]}
    card_map = {c["id"]: c for c in cards}
    names = {}

    def resolve(key):
        return names.get(key, key)

    def refs(keys, required=True):
        if required and not keys:
            raise ValueError("A semantic assertion requires delivered evidence")
        if any(k not in delivered or delivered[k]["line"] > cutoff for k in keys):
            raise ValueError("Unknown, undelivered or future evidence reference")
        return [deepcopy(delivered[k]["ref"]) for k in dict.fromkeys(keys)]

    def root_of(gid):
        seen = set()
        while gid in objects and gid not in seen:
            seen.add(gid)
            obj = objects[gid]
            parent = obj.get("parent_goal_id") or obj.get("goal_id")
            if not parent:
                return obj
            gid = parent
        raise ValueError("Unknown or cyclic goal parent")

    def scoped(gid, evidence):
        root = root_of(gid)
        if any(not root["origin_line"] <= r["line_number"] <= root["end_line"] for r in evidence):
            raise ValueError("Evidence crosses an effective task revision")

    def allocate(kind, key):
        if not key or key in names:
            raise ValueError("Duplicate or empty local declaration key")
        identifier = kind + ":" + identity(group_id, key)
        names[key] = identifier
        return identifier

    for raw in group["goals"]:
        evidence = refs(raw["refs"])
        parent = resolve(raw["parent_id"])
        if parent not in {g["id"] for g in entities["goals"]}:
            raise ValueError("Goal parent must be a supplied goal or an earlier declaration")
        scoped(parent, evidence)
        if raw["key"] in objects:
            old = objects[raw["key"]]
            if not old.get("parent_goal_id") or old["parent_goal_id"] != parent:
                raise ValueError("Recorded roots and goal parents cannot be rewritten")
            if old["objective"] != raw["objective"] or old["conditions"] != raw["conditions"]:
                raise ValueError("Stable goal definitions cannot be rewritten; declare a changed objective separately")
            names[raw["key"]] = raw["key"]
            continue
        gid = allocate("goal", raw["key"])
        row = dict(
            id=gid,
            parent_goal_id=parent,
            root_relation=raw.get("root_relation", "").strip(),
            parent_relation_basis="inferred" if raw.get("root_relation", "").strip() else "unreviewed",
            objective=raw["objective"],
            conditions=raw["conditions"],
            evidence_refs=evidence,
            origin_line=min(r["line_number"] for r in evidence),
            end_line=root_of(parent)["end_line"],
            basis="inferred",
            goal_disposition="open",
            completion_claim="unknown",
            confidence="unknown",
            knowledge_cutoff=cutoff,
        )
        objects[gid] = row
        entities["goals"].append(row)
    for raw in group["attempts"]:
        goal = resolve(raw["goal_id"])
        evidence = refs(raw["refs"])
        if goal not in {g["id"] for g in entities["goals"]}:
            raise ValueError("Unknown attempt goal")
        scoped(goal, evidence)
        if raw["key"] in objects:
            old = objects[raw["key"]]
            if old.get("goal_id") != goal or old.get("approach") != raw["approach"]:
                raise ValueError("Stable attempt cannot change goal or approach")
            names[raw["key"]] = raw["key"]
            continue
        aid = allocate("attempt", raw["key"])
        row = dict(
            id=aid,
            goal_id=goal,
            approach=raw["approach"],
            outcome="unknown",
            evidence_refs=evidence,
            start_line=min(r["line_number"] for r in evidence),
            end_line=max(r["line_number"] for r in evidence),
            basis="inferred",
        )
        objects[aid] = row
        entities["attempts"].append(row)
    for raw in group["stages"]:
        goal, attempt = resolve(raw["goal_id"]), resolve(raw["attempt_id"])
        evidence = refs(raw["refs"])
        if goal not in {g["id"] for g in entities["goals"]}:
            raise ValueError("Unknown stage goal")
        scoped(goal, evidence)
        if attempt and (attempt not in objects or objects[attempt].get("goal_id") != goal):
            raise ValueError("Stage attempt belongs to another goal")
        if not attempt:
            attempt = "attempt:" + identity(goal, "unestablished")
            if attempt not in objects:
                row = dict(id=attempt, goal_id=goal, approach="Approach not established", outcome="unknown", evidence_refs=[], basis="unknown")
                objects[attempt] = row
                entities["attempts"].append(row)
        sid = raw["key"] if raw["key"] in state["stages"] else allocate("stage", raw["key"])
        if sid in state["stages"] and any(state["stages"][sid][k] != v for k, v in (("goal_id", goal), ("attempt_id", attempt), ("kind", raw["kind"]))):
            raise ValueError("Stable stage cannot change goal, approach or kind")
        names[raw["key"]] = sid
        if sid not in state["stages"]:
            state["stages"][sid] = dict(
                id=sid, goal_id=goal, attempt_id=attempt, title=raw["title"], kind=raw["kind"], evidence_refs=evidence, knowledge_cutoff=cutoff
            )
    seen = set()
    for raw in group["assessments"]:
        cid = raw["card_id"]
        if cid in seen or cid not in card_map or card_map[cid]["end_line"] > cutoff:
            raise ValueError("Duplicate, unknown or future card")
        seen.add(cid)
        old = state["assessments"].get(cid)
        if cid not in target_ids and not old:
            raise ValueError("Card outside the current review window")
        stage_ids = list(dict.fromkeys(resolve(s) for s in raw["stage_ids"]))
        if any(s not in state["stages"] for s in stage_ids):
            raise ValueError("Unknown stage reference")
        if raw["status"] == "unknown" and stage_ids:
            raise ValueError("Unknown attribution must not assign cost ownership")
        evidence = refs(raw["refs"], required=raw["status"] != "unknown")
        own = set(card_map[cid]["refs"]) | set(card_map[cid]["raw_refs"])
        if (
            raw["status"] != "unknown"
            and not own.intersection(raw["refs"])
            and not any(r["line_number"] in (card_map[cid]["start_line"], card_map[cid]["end_line"]) for r in evidence)
        ):
            raise ValueError("Assessment needs evidence from that card")
        for sid in stage_ids:
            scoped(state["stages"][sid]["goal_id"], evidence)
        for other in raw["prior_card_ids"] + raw["uptake_card_ids"]:
            if other not in card_map or card_map[other]["end_line"] > cutoff:
                raise ValueError("Unknown or future comparison/uptake card")
        if any(card_map[o]["end_line"] > card_map[cid]["start_line"] for o in raw["prior_card_ids"]):
            raise ValueError("Redundancy cannot use evidence acquired after the action")
        for other in raw["uptake_card_ids"]:
            target = card_map[other]
            if target["start_line"] < card_map[cid]["end_line"] or not any(target["start_line"] <= r["line_number"] <= target["end_line"] for r in evidence):
                raise ValueError("Adoption requires a later card and cited evidence of its use")
        if raw["redundancy"] == "supported" and (
            not raw["prior_card_ids"] or any(not any(p["line"] <= card_map[cid]["start_line"] for p in card_map[o]["presented"]) for o in raw["prior_card_ids"])
        ):
            raise ValueError("Supported redundancy requires prior presented evidence")
        row = {**deepcopy(raw), "stage_ids": stage_ids, "evidence_refs": evidence, "knowledge_cutoff": cutoff}
        expected_evidence = refs(raw.get("expected_refs", []), required=False)
        if any(r["line_number"] > card_map[cid]["start_line"] for r in expected_evidence):
            raise ValueError("Expected value must cite evidence available before the action")
        row["expected_evidence_refs"] = expected_evidence
        if not expected_evidence:
            row["expected"] = "unknown"
        if old:
            # Later evidence may change retrospective value, never what was expected then.
            row["expected"], row["purpose"] = old["expected"], old["purpose"]
            row["expected_refs"] = old.get("expected_refs", [])
            row["expected_evidence_refs"] = old.get("expected_evidence_refs", [])
        state["assessments"][cid] = revised(old, row)
    for raw in group["closures"]:
        goal = resolve(raw["goal_id"])
        if goal not in {g["id"] for g in entities["goals"]}:
            raise ValueError("Unknown closure goal")
        evidence = refs(raw["refs"])
        scoped(goal, evidence)
        if raw["evidence_state"] == "supported" and not raw["supported_conditions"]:
            raise ValueError("Supported closure must name its supported conditions")
        previous = state["closures"].get(goal)
        closure = {**deepcopy(raw), "goal_id": goal, "evidence_refs": evidence, "knowledge_cutoff": cutoff}
        state["closures"][goal] = closure = revised(previous, closure)
        objects[goal].update(goal_disposition=closure["disposition"], completion_claim=closure["claim"], open_questions=closure["open_questions"])
    phases = occurrences(state, cards)

    def endpoint(key, latest=True):
        key = resolve(key)
        matches = [p for p in phases if p["id"] == key or p["stage_id"] == key or p["goal_id"] == key or key in p["card_ids"]]
        if not matches:
            raise ValueError("Relation endpoint has no established occurrence")
        return max(matches, key=lambda p: p["end_line"]) if latest else min(matches, key=lambda p: p["start_line"])

    for raw in group["relations"]:
        source, target = endpoint(raw["source_id"]), endpoint(raw["target_id"])
        evidence = refs(raw["refs"])
        if source["id"] == target["id"] or source["end_line"] > target["start_line"]:
            raise ValueError("Occurrence dependency must point forward; do not connect overlapping or earlier work")
        lines = {r["line_number"] for r in evidence}
        if any(not lines.intersection(r["line_number"] for r in node["evidence_refs"]) for node in (source, target)):
            raise ValueError("A dependency requires cited evidence from both endpoints")
        if not raw["result"].strip():
            raise ValueError("A dependency must name the transmitted result")
        state["relations"].append(
            dict(
                id="relation:" + identity(group_id, raw),
                source_id=source["id"],
                target_id=target["id"],
                kind=raw["kind"],
                layer="semantic",
                result=raw["result"],
                rationale=raw["result"],
                evidence_refs=evidence,
                basis="inferred",
            )
        )
    for raw in group["diagnostics"]:
        goal = resolve(raw["goal_id"])
        evidence = refs(raw["refs"])
        scoped(goal, evidence)
        if any(c not in card_map or card_map[c]["end_line"] > cutoff for c in raw["card_ids"]):
            raise ValueError("Diagnostic references an unknown/future card")
        state["diagnostics"].append(
            {
                **raw,
                "id": "diagnostic:" + identity(group_id, raw),
                "goal_id": goal,
                "evidence_refs": evidence,
                "call_ids": list(dict.fromkeys(c for i in raw["card_ids"] for c in card_map[i]["call_ids"])),
            }
        )
    state["applied_groups"].append(group_id)
    state["graph_revision"] += 1
    state["revisions"].append(dict(id=group_id, revision=state["graph_revision"], base_revision=base_revision, knowledge_cutoff=cutoff, patch=deepcopy(group)))
    cp.clear()
    cp.update(state)
    return True
