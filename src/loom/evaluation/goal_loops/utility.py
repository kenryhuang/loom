"""Evidence-bound action utility; factual repetition is not a waste judgment."""

from collections import Counter
from copy import deepcopy

from loom.core import thaw_json
from loom.evaluation.goal_loops.context import window
from loom.evaluation.goal_loops.facts import identity


def action_facts(source, facts, view):
    uses = {r["id"]: r for r in facts["tool_uses"]}
    previous, result = {}, []
    for atom in sorted(view["atoms"], key=lambda a: min(r["line_number"] for r in a["evidence_refs"])):
        lines = [r["line_number"] for r in atom["evidence_refs"]]
        row = {k: atom.get(k) for k in ("id", "kind", "tool", "model", "status", "duration_ms", "total_tokens")}
        row["target_excerpt"] = str(atom.get("target") or "")[:240]
        row.update(start_line=min(lines), end_line=max(lines), prior_same_input_ids=[], same_recorded_output=None)
        root = next((g for g in view["roots"] if g["origin_line"] <= min(lines) <= g["end_line"]), None)
        row["goal_id"] = root["id"] if root else None
        row["goal_start_line"] = root["origin_line"] if root else min(lines)
        row["goal_end_line"] = root["end_line"] if root else max(lines)
        use = uses.get(atom["id"], {})
        if use.get("input_ref"):
            key = (row["goal_id"], atom.get("tool"), identity(thaw_json(source.resolve(use["input_ref"]))))
            output = thaw_json(source.resolve(use["raw_output_ref"])) if use.get("raw_output_ref") else None
            if isinstance(output, dict) and "source" in output and "value" in output:
                output = output["value"]
            digest = identity(output) if output is not None else None
            prior = previous.get(key, [])
            row["prior_same_input_ids"] = [p[0] for p in prior[-3:]]
            if prior and digest is not None and prior[-1][1] is not None:
                row["same_recorded_output"] = prior[-1][1] == digest
            previous.setdefault(key, []).append((row["id"], digest))
        result.append(row)
    return result


def review_units(rows, registry, view, max_chars):
    groups = []
    for row in rows:
        if not groups or len(groups[-1]) == 4 or groups[-1][-1]["goal_id"] != row["goal_id"]:
            groups.append([])
        groups[-1].append(row)
    roots = {g["id"]: g for g in view["roots"]}
    result = []
    for group in groups:
        root = roots.get(group[0]["goal_id"])
        eligible = [p for p in registry.values() if p.get("evidence_ref") and (root is None or root["origin_line"] <= p["line"] <= root["end_line"])]
        own = [
            [p["id"] for p in sorted(eligible, key=lambda p: (not p["type"].endswith((".completed", ".failed")), p["line"])) if r["id"] in p["call_ids"]]
            for r in group
        ]
        # Round-robin keeps a long response from monopolizing the target window.
        ids = [parts[i] for i in range(max(map(len, own), default=0)) for parts in own if i < len(parts)]
        comparison = {i for r in group for i in r["prior_same_input_ids"]}
        comparison.update(r["id"] for r in [r for r in rows if r["end_line"] < group[0]["start_line"] and r["goal_id"] == group[0]["goal_id"]][-3:])
        terminal = [
            p["id"]
            for p in eligible
            if p["type"].startswith(("acceptance.", "verification."))
            or any(r["id"] in p["call_ids"] and r.get("tool") in {"finish", "create_report", "docs_output"} for r in rows)
        ]
        prior = [p["id"] for p in eligible if comparison.intersection(p["call_ids"])]
        following = [p["id"] for p in eligible if p["line"] > group[-1]["end_line"]][:8]
        # Include at least one target page each before final-outcome/comparison evidence.
        priority = [p[0] for p in own if p] + terminal[-4:] + prior + following + ids + terminal
        ids = list(dict.fromkeys(priority))
        selected = window(ids, registry, max_chars)
        result.append(
            dict(
                action_ids=[r["id"] for r in group],
                packet_ids=selected,
                goal_id=group[0]["goal_id"],
                evidence_candidates=len(ids),
                evidence_partial=len(selected) < len(ids),
            )
        )
    return result


def accept_assessments(raw_rows, delivered, rows, target_ids, saved):
    known = {r["id"]: r for r in rows}
    result, issues, referenced = deepcopy(saved), [], set()
    seen = set()
    for raw in raw_rows:
        cid = raw["call_id"]
        if cid in saved:
            continue
        try:
            if cid not in target_ids or cid not in known or cid in seen:
                raise ValueError("Assessment must name a unique supplied action")
            seen.add(cid)
            row = known[cid]
            keys = ("refs", "contribution_refs", "comparison_refs", "acceptance_refs")
            ids = set(p for key in keys for p in raw[key])
            if any(p not in delivered or not delivered[p].get("evidence_ref") for p in ids):
                raise ValueError("Assessment cites undelivered evidence")
            if any(not row["goal_start_line"] <= delivered[p]["line"] <= row["goal_end_line"] for p in ids):
                raise ValueError("Assessment crosses the effective goal revision")
            if not raw["refs"] or not any(cid in delivered[p]["call_ids"] for p in raw["refs"]):
                raise ValueError("Assessment needs the action's own evidence")
            if raw["value"] in {"decisive", "material"} and not raw["contribution_refs"]:
                raise ValueError("High contribution needs result or downstream evidence")
            if any(delivered[p]["line"] < row["start_line"] for p in raw["contribution_refs"]):
                raise ValueError("Earlier evidence cannot be this action's resulting contribution")
            if any(
                not (
                    delivered[p]["type"].endswith((".completed", ".failed"))
                    or delivered[p]["type"] == "artifact.created"
                    or delivered[p]["type"].startswith("acceptance.gate.")
                )
                for p in raw["contribution_refs"]
            ):
                raise ValueError("Contribution must cite an observed result, not only a request")
            comparisons = raw["comparison_call_ids"]
            if any(i not in known or known[i]["end_line"] >= row["start_line"] or known[i]["goal_id"] != row["goal_id"] for i in comparisons):
                raise ValueError("Comparison must name earlier actions in the same goal revision")
            if any(not any(i in delivered[p]["call_ids"] for p in raw["comparison_refs"]) for i in comparisons):
                raise ValueError("Comparison needs evidence of each earlier action")
            if raw["novelty"] == "duplicate" and not comparisons:
                raise ValueError("Duplicate judgment needs a cited prior action")
            if raw["comparison_refs"] and not comparisons:
                raise ValueError("Comparison evidence must name its earlier action")
            if any(not delivered[p]["type"].startswith(("acceptance.", "verification.")) for p in raw["acceptance_refs"]):
                raise ValueError("Acceptance links must cite recorded conditions or verification")
            if raw["role"] == "verification" and raw["value"] in {"decisive", "material"} and not raw["acceptance_refs"]:
                raise ValueError("Effective verification needs recorded acceptance/verification evidence")
            result[cid] = {k: deepcopy(v) for k, v in raw.items() if k not in keys}
            result[cid].update(status="reviewed", basis="inferred")
            for key in keys:
                result[cid]["evidence_refs" if key == "refs" else key] = [deepcopy(delivered[p]["evidence_ref"]) for p in raw[key]]
            referenced.update(ids)
        except ValueError as exc:
            issues.append(dict(field="assessments", key=cid, reason=str(exc)))
    return result, referenced, issues


def project_utility(rows, saved):
    assessments = []
    for row in rows:
        assessment = saved.get(row["id"], dict(call_id=row["id"], status="not_reviewed", necessity="unknown", value="unknown", novelty="unknown"))
        assessments.append({**deepcopy(assessment), "facts": deepcopy(row)})
    reviewed = [r for r in assessments if r["status"] == "reviewed"]
    return dict(
        version="action-utility.1",
        rows=assessments,
        coverage=dict(eligible=len(rows), reviewed=len(reviewed), pending=len(rows) - len(reviewed), unknown=sum(r["value"] == "unknown" for r in reviewed)),
        value_counts=dict(Counter(r["value"] for r in reviewed)),
    )
