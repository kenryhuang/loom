"""Authenticated governance review, registry, monitor, and rollback CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.cli import configured_trust_store, load_identity
from loom.campaigns.contracts import ApprovalRecord
from loom.campaigns.serialization import canonical_json_bytes, new_prefixed_id, utc_now
from loom.core import Result, thaw_json
from loom.governance.policy import GovernanceReview
from loom.governance.registry import SQLiteGovernanceStore
from loom.governance.rollback import RollbackController


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_execute(args))
    if result.ok:
        payload = json.loads(canonical_json_bytes(result.value))
        print(json.dumps(payload, sort_keys=True) if args.json else _human(payload))
        return 0
    payload = {
        "ok": False,
        "error": {
            "code": result.error.code,
            "message": result.error.message,
            "retryable": result.error.retryable,
            "metadata": None if result.error.metadata is None else thaw_json(result.error.metadata),
        },
    }
    print(json.dumps(payload, sort_keys=True) if args.json else f"{result.error.code}: {result.error.message}")
    return 1


async def _execute(args) -> Result:
    identity = load_identity(args.identity, configured_trust_store())
    if not identity.ok:
        return identity
    actor, provider = identity.value
    store = SQLiteGovernanceStore(args.governance_dir, provider, ArtifactStore(args.artifact_root))
    if args.command == "active":
        return await store.active_versions(args.surface)
    if args.command == "monitors":
        return await store.monitors(args.status)
    if args.command == "review":
        return await store.reviews(args.candidate_id)
    if args.command in {"approve", "reject"}:
        approval = ApprovalRecord(
            actor.subject,
            "governance_approver",
            utc_now(),
            args.expires_at,
            args.candidate_digest,
            args.baseline_digest,
            args.policy_digest,
            args.risk_rules_digest,
            args.gate_digest,
            "approve" if args.command == "approve" else "reject",
            args.rationale,
        )
        review = GovernanceReview(args.review_id or new_prefixed_id("review_"), args.candidate_id, actor, approval)
        return await store.record_review(review, args.gate_digest)
    active = await store.active_for_decision(args.promotion_id)
    if not active.ok:
        return active
    reason = args.reason if args.command == "rollback" else "promotion TTL expired"
    return await RollbackController(store).rollback(
        active.value.surface_id,
        expected_active_version=active.value.version,
        actor=actor,
        reason=reason,
        operation_id=args.operation_id or new_prefixed_id("op_"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom governance")
    children = parser.add_subparsers(dest="command", required=True)
    active = children.add_parser("active")
    active.add_argument("--surface")
    monitors = children.add_parser("monitors")
    monitors.add_argument("--status")
    review = children.add_parser("review")
    review.add_argument("candidate_id")
    for command in ("approve", "reject"):
        child = children.add_parser(command)
        child.add_argument("candidate_id")
        child.add_argument("--candidate-digest", required=True)
        child.add_argument("--baseline-digest", required=True)
        child.add_argument("--policy-digest", required=True)
        child.add_argument("--risk-rules-digest", required=True)
        child.add_argument("--gate-digest", required=True)
        child.add_argument("--expires-at", required=True)
        child.add_argument("--rationale")
        child.add_argument("--review-id")
    rollback = children.add_parser("rollback")
    rollback.add_argument("promotion_id")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--operation-id")
    expire = children.add_parser("expire")
    expire.add_argument("promotion_id")
    expire.add_argument("--operation-id")
    for child in children.choices.values():
        child.add_argument("--governance-dir", required=True)
        child.add_argument("--artifact-root", required=True)
        child.add_argument("--identity", required=True)
        child.add_argument("--json", action="store_true")
    return parser


def _human(payload) -> str:
    if isinstance(payload, list):
        return "\n".join(json.dumps(item, sort_keys=True) for item in payload)
    if isinstance(payload, dict):
        return "\n".join(f"{key}: {value}" for key, value in payload.items())
    return str(payload)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
