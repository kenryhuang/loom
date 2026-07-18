"""Read-only comparison of persisted candidate ExperimentBundles."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from loom.campaigns.cli import configured_trust_store, load_identity
from loom.campaigns.experiments import load_experiment_bundle
from loom.campaigns.serialization import canonical_json_bytes
from loom.campaigns.store import SQLiteCampaignStore
from loom.core import Result, err, make_loom_error, ok, thaw_json


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_execute(args))
    if result.ok:
        payload = json.loads(canonical_json_bytes(result.value))
        print(json.dumps(payload, sort_keys=True) if args.json else "\n".join(f"{key}: {value}" for key, value in payload.items()))
        return 0
    payload = {
        "ok": False,
        "error": {
            "code": result.error.code,
            "message": result.error.message,
            "metadata": None if result.error.metadata is None else thaw_json(result.error.metadata),
        },
    }
    print(json.dumps(payload, sort_keys=True) if args.json else f"{result.error.code}: {result.error.message}")
    return 1


async def _execute(args) -> Result:
    identity = load_identity(args.identity, configured_trust_store())
    if not identity.ok:
        return identity
    _, provider = identity.value
    store = SQLiteCampaignStore(args.campaign_dir, provider)
    projection = await store.load(args.campaign_id)
    if not projection.ok:
        return projection
    bundles = []
    for candidate_id in (args.baseline_id, args.candidate_id):
        state = projection.value.candidates.get(candidate_id)
        if state is None or not state.experiment_refs:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Candidate has no persisted experiment evidence",
                    retryable=False,
                    metadata={"candidate_id": candidate_id},
                )
            )
        bundle = load_experiment_bundle(store, state.experiment_refs[-1])
        if not bundle.ok:
            return bundle
        bundles.append(bundle.value)
    left_metrics = {metric.objective_id: metric for metric in bundles[0].metrics}
    right_metrics = {metric.objective_id: metric for metric in bundles[1].metrics}
    objectives = sorted(set(left_metrics) | set(right_metrics))
    return ok(
        {
            "schema_version": "loom.experiment-comparison.v1",
            "campaign_id": args.campaign_id,
            "baseline_id": args.baseline_id,
            "candidate_id": args.candidate_id,
            "metrics": {
                objective: {
                    "baseline_candidate_value": None if objective not in left_metrics else left_metrics[objective].candidate_value,
                    "candidate_value": None if objective not in right_metrics else right_metrics[objective].candidate_value,
                    "difference": _difference(left_metrics.get(objective), right_metrics.get(objective)),
                }
                for objective in objectives
            },
        }
    )


def _difference(left, right):
    if left is None or right is None or left.candidate_value is None or right.candidate_value is None:
        return None
    return right.candidate_value - left.candidate_value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom experiment")
    children = parser.add_subparsers(dest="command", required=True)
    compare = children.add_parser("compare")
    compare.add_argument("campaign_id")
    compare.add_argument("baseline_id")
    compare.add_argument("candidate_id")
    compare.add_argument("--campaign-dir", required=True)
    compare.add_argument("--identity", required=True)
    compare.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
