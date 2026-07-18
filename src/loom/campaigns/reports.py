"""Deterministic campaign decision reports and human-readable views."""

from __future__ import annotations

from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.core import Result, ok


def publish_promotion_recommendation(store, payload, finalizer) -> Result:
    core = dict(payload)
    attestation = {
        "subject": finalizer.subject,
        "roles": finalizer.roles,
        "assertion_signature": finalizer.signature,
        "payload_digest": canonical_digest(core),
    }
    markdown = _markdown(core, attestation)
    markdown_ref = store.artifacts.publish_bytes(
        markdown.encode("utf-8"),
        kind="promotion_recommendation_report",
        schema_version="loom.promotion-recommendation-report.v1",
        suffix=".md",
    )
    if not markdown_ref.ok:
        return markdown_ref
    document = {**core, "attestation": attestation, "markdown_ref": markdown_ref.value}
    json_ref = store.artifacts.publish_bytes(
        canonical_json_bytes(document),
        kind="promotion_recommendation",
        schema_version="loom.promotion-recommendation.v1",
        suffix=".json",
    )
    return json_ref if not json_ref.ok else ok((json_ref.value, markdown_ref.value, document))


def _markdown(payload, attestation) -> str:
    candidate = payload.get("candidate_id") or "none"
    return (
        "# Loom Promotion Recommendation\n\n"
        f"- Campaign: `{payload['campaign_id']}`\n"
        f"- Candidate: `{candidate}`\n"
        f"- Disposition: `{payload['disposition']}`\n"
        f"- Baseline digest: `{payload['baseline_digest']}`\n"
        f"- Policy digest: `{payload['promotion_policy_digest']}`\n"
        f"- Finalist digest: `{payload['finalist_digest']}`\n"
        f"- Holdout evidence: `{payload['holdout_results_ref'].sha256}`\n"
        f"- Finalizer: `{attestation['subject']}`\n"
        f"- Evidence digest: `{attestation['payload_digest']}`\n"
        "- Registry activation: `false`\n\n"
        "This Phase 1 report is evidence only. It cannot update an active registry pointer.\n"
    )


__all__ = ["publish_promotion_recommendation"]
