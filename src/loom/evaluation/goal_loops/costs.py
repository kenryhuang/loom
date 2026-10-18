"""One cost owner per recorded invocation; related evidence never duplicates money/time."""

from collections import defaultdict
from math import isclose

from loom.evaluation.base_metrics import timestamp

FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "model_duration_ms", "tool_duration_ms")


def summarize(atoms):
    result = {"model_calls": sum(a["kind"] == "model" for a in atoms), "tool_calls": sum(a["kind"] == "tool" for a in atoms)}
    for field in FIELDS:
        kind = "tool" if field == "tool_duration_ms" else "model"
        selected = [a for a in atoms if a["kind"] == kind]
        values = [a.get("duration_ms") if field.endswith("_duration_ms") else a.get(field) for a in selected]
        result[field] = dict(
            known_sum=sum(v for v in values if v is not None),
            measured_count=sum(v is not None for v in values),
            eligible_count=len(values),
            conflict_count=sum(a.get("usage_status") == "conflicting" for a in selected) if not field.endswith("_duration_ms") else 0,
        )
    intervals = sorted(
        (timestamp(a["started_at"]), timestamp(a["ended_at"]))
        for a in atoms
        if timestamp(a.get("started_at")) is not None and timestamp(a.get("ended_at")) is not None and timestamp(a["ended_at"]) >= timestamp(a["started_at"])
    )
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    result.update(
        wall_span_ms=intervals[-1][1] - intervals[0][0] if intervals else None, observed_busy_union_ms=sum(b - a for a, b in merged) if intervals else None
    )
    if intervals:
        result["wall_span_ms"] = max(b for _, b in intervals) - min(a for a, _ in intervals)
    return result


def ledger(view, state):
    entities = {r["id"]: r for k in ("goals", "attempts", "phases", "activities") for r in state[k] if not r.get("superseded_by")}

    def ancestors(identifier):
        chain, seen = [], set()
        while identifier in entities and identifier not in seen:
            chain.append(identifier)
            seen.add(identifier)
            row = entities[identifier]
            identifier = row.get("parent_goal_id") or row.get("goal_id") or row.get("attempt_id")
        return chain + ["analysis:shared"]

    related, serving, uncertain = defaultdict(set), defaultdict(set), set()
    for kind in ("phases", "activities"):
        for row in state[kind]:
            for call in row["call_ids"]:
                related[call].add(row["id"])
                if row.get("allocation", "serves") == "serves":
                    serving[call].add(row["id"])
                elif row.get("allocation") == "uncertain":
                    uncertain.add(call)
    assignments, exclusive, inclusive = [], defaultdict(list), defaultdict(list)
    for atom in view["atoms"]:
        owners = sorted(serving[atom["id"]])
        if atom["id"] in uncertain:
            owners = []
        if not owners:
            owner, basis = "analysis:unassigned", "unknown"
        else:
            chains = [ancestors(owner) for owner in owners]
            owner = next(candidate for candidate in chains[0] if all(candidate in c for c in chains))
            basis = "inferred_unique_activity" if len(owners) == 1 else "inferred_shared_call"
        assignments.append(
            dict(
                cost_atom_id=atom["id"],
                owner=owner,
                related_ids=sorted(related[atom["id"]]),
                allocation_basis=basis,
                uncertainty_reason="Candidate ownership is unresolved" if atom["id"] in uncertain else None,
            )
        )
        exclusive[owner].append(atom)
        for ancestor in ancestors(owner) if owner in entities else [owner]:
            if ancestor == "analysis:shared" and owner != "analysis:shared":
                continue
            inclusive[ancestor].append(atom)
    totals = summarize(view["atoms"])
    by_owner = {
        key: dict(exclusive=summarize(exclusive[key]), inclusive=summarize(inclusive[key]))
        for key in entities.keys() | {"analysis:unassigned", "analysis:shared"}
    }
    roots = [g["id"] for g in state["goals"] if not g.get("parent_goal_id") and not g.get("superseded_by")]
    for field in FIELDS:
        summed = sum(by_owner[k]["inclusive"][field]["known_sum"] for k in roots + ["analysis:unassigned", "analysis:shared"])
        if not isclose(summed, totals[field]["known_sum"], rel_tol=1e-12, abs_tol=1e-6):
            raise ValueError("Cost conservation failed: " + field)
    return dict(atoms=view["atoms"], assignments=assignments, totals=totals, by_owner=by_owner, conserved=True)
