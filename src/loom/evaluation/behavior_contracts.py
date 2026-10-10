"""Versioned behavior evaluation contracts. Legacy scores are deliberately separate."""

import hashlib
import json

SCHEMA_VERSION = "loom.evaluation.bundle.v3"
ANALYZER_VERSION = "behavior-evaluation.3.4"
PROMPT_VERSION = "behavior-rubric.3.2"
CHECKPOINT_VERSION = "behavior-checkpoint.1"
DIMENSIONS = ("intent_alignment", "plan_quality", "progress_effectiveness", "investigation_efficiency", "adaptation_recovery")
PATTERNS = (
    "goal_drift",
    "plan_mismatch",
    "repeated_unchanged_result",
    "alternating_search_replan",
    "hypothesis_reopened",
    "evidence_not_adopted",
    "premature_closure",
    "causal_recovery",
    "discriminative_investigation",
    "other",
)
STATUSES = ("effective", "ineffective", "mixed", "unknown")


def stable_id(kind, *parts):
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    return f"{kind}:{digest}"


def outcome(criteria, *, goal_coverage="unknown", ambiguous=False):
    """Aggregate only required, applicable criteria; unknown applicability cannot pass."""
    required = [c for c in criteria if c.get("required", True) and not (c.get("status") == "not_applicable" and c.get("applicability") == "confirmed")]
    if ambiguous or not required:
        return "unverified"
    if any(c.get("status") == "contradicted" for c in required):
        return "not_achieved"
    supported = sum(c.get("status") == "supported" for c in required)
    if supported == len(required) and goal_coverage == "complete":
        return "achieved"
    return "partially_verified" if supported else "unverified"


def validate_bundle(value):
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Expected loom.evaluation.bundle.v3; legacy bundles need their own reader")
    for name in ("source_sha256", "base", "facts", "semantic"):
        if name not in value:
            raise ValueError(f"Missing v3 bundle field: {name}")
    return value
