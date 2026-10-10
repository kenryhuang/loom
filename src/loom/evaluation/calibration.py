"""Offline rollout gate report for independently reviewed v2/v3 comparison records.

This command consumes measurements and human annotations; it never manufactures them.
A missing or undersized corpus explicitly leaves rollout blocked.
"""

import argparse
import json
import math
import statistics
from collections import Counter
from pathlib import Path

CATEGORIES = ("intent", "plan", "cycle", "evidence_adoption")
FAMILIES = {"coding", "research", "general"}


def _interval(successes, total):
    if not total:
        return None
    z, p = 1.96, successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [max(0, center - width), min(1, center + width)]


def calibration_report(records):
    if len({r["episode_id"] for r in records}) != len(records):
        raise ValueError("Duplicate calibration episode IDs")
    held = [r for r in records if r.get("split") == "held_out"]
    reviewed = [r for r in held if len(set(r.get("reviewer_ids", []))) >= 2 and r.get("adjudicated") is True]
    counts = Counter()
    by_category = {}
    for category in CATEGORIES:
        values = Counter()
        for r in reviewed:
            for label in r.get("findings", []):
                if label.get("category") != category:
                    continue
                truth, prediction = label.get("human_present"), label.get("v3_present")
                if type(truth) is not bool or type(prediction) is not bool:
                    raise ValueError("Calibration findings require independent boolean labels")
                values["tp" if truth and prediction else "fn" if truth else "fp" if prediction else "tn"] += 1
        tp, fp, fn, tn = (values[k] for k in ("tp", "fp", "fn", "tn"))
        by_category[category] = {
            "counts": dict(values),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "precision_95_interval": _interval(tp, tp + fp),
            "recall_95_interval": _interval(tp, tp + fn),
        }
        counts.update(values)
    outcomes = [v for r in reviewed for v in r.get("outcomes", []) if v.get("sufficient_evidence") is True]
    agreed = sum(v.get("human_status") == v.get("v3_status") and v.get("v3_status") != "unverified" for v in outcomes)
    ratios = [
        r["v3_tokens"] / r["v2_tokens"]
        for r in reviewed
        if r.get("v2_tokens", 0) > 0 and r.get("v3_tokens") is not None and r.get("v3_supported_coverage", -1) >= r.get("v2_supported_coverage", 2)
    ]
    integrity = all(r.get("evidence_audit_passed") is True and r.get("unsupported_achieved") is False for r in reviewed)
    category_gate = all(
        v["precision"] is not None
        and v["precision"] >= 0.9
        and v["recall"] is not None
        and v["recall"] >= 0.8
        and v["false_positive_rate"] is not None
        and v["false_positive_rate"] <= 0.1
        for v in by_category.values()
    )
    adequate_categories = all(
        sum(v["counts"].get(k, 0) for k in ("tp", "fn")) >= 5 and sum(v["counts"].get(k, 0) for k in ("fp", "tn")) >= 5 for v in by_category.values()
    )

    def measured(field):
        values = sorted(r[field] for r in reviewed if type(r.get(field)) in (int, float) and r[field] >= 0)
        return {
            "count": len(values),
            "median": statistics.median(values) if values else None,
            "p90": values[max(0, math.ceil(len(values) * 0.9) - 1)] if values else None,
        }

    gates = {
        "reviewed_corpus": len(reviewed) >= 30 and {r.get("family") for r in reviewed} >= FAMILIES,
        "held_out_disjoint": bool(held) and all(r.get("used_for_prompt_development") is False for r in held),
        "category_quality": category_gate and adequate_categories,
        "evidence_integrity": bool(reviewed) and integrity,
        "outcome_agreement": bool(outcomes) and agreed / len(outcomes) >= 0.9,
        "token_savings_at_comparable_coverage": len(ratios) >= 30 and statistics.median(ratios) <= 0.5,
        "stability_measured": bool(reviewed) and all(r.get("repeat_runs", 0) >= 2 for r in reviewed),
    }
    return {
        "schema_version": "loom.evaluation.calibration.v1",
        "rollout_ready": all(gates.values()),
        "gates": gates,
        "reviewed_held_out_episodes": len(reviewed),
        "categories": by_category,
        "raw_counts": dict(counts),
        "outcome_agreement": {"agreed": agreed, "total": len(outcomes), "interval_95": _interval(agreed, len(outcomes))},
        "cost": {"comparable_cases": len(ratios), "median_v3_v2_ratio": statistics.median(ratios) if ratios else None},
        "measurements": {field: measured(field) for field in ("v2_tokens", "v3_tokens", "v2_wall_seconds", "v3_wall_seconds")},
        "failures": {version: sum(r.get(f"{version}_failed") is True for r in reviewed) for version in ("v2", "v3")},
        "limitations": [
            "Human review and independence are declared by the corpus author; this tool cannot authenticate reviewers.",
            "Rate intervals describe small-sample uncertainty; passing point estimates is not a calibrated probability.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="JSON array of reviewed comparison records")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = calibration_report(json.loads(args.corpus.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Rollout ready: {report['rollout_ready']} · {report['reviewed_held_out_episodes']} reviewed held-out episodes")


if __name__ == "__main__":
    main()
