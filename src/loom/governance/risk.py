"""Deterministic candidate risk classification."""

from __future__ import annotations

from dataclasses import dataclass

from loom.campaigns.contracts import ArtifactRef, CandidateKind, CapabilityManifest, RiskAssessment, RiskLevel
from loom.campaigns.serialization import utc_now

_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.FORBIDDEN: 3}
_FORBIDDEN = frozenset({"evaluator", "permissions", "provider", "secrets", "governance", "runtime_engine"})
_MEDIUM = frozenset(
    {
        "system_prompt",
        "user_prompt",
        "few_shot_examples",
        "tool_description",
        "required_schema",
        "enum_schema",
        "resolver_priority",
        "completion_policy",
    }
)
_LOW = frozenset({"context_numeric_limit", "token_numeric_limit", "documentation_wording"})


@dataclass(frozen=True, slots=True)
class RiskInput:
    kind: CandidateKind
    changed_surfaces: tuple[str, ...]
    patch_operations: tuple[str, ...]
    capability_manifest: CapabilityManifest
    declared_risk: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_surfaces", tuple(self.changed_surfaces))
        object.__setattr__(self, "patch_operations", tuple(self.patch_operations))


def classify_risk(candidate_id: str, value: RiskInput, rule_set: ArtifactRef) -> RiskAssessment:
    matches: list[tuple[str, RiskLevel]] = []
    for operation in value.patch_operations:
        if operation not in {"set", "set_limit", "replace", "append_rule"}:
            matches.append((f"unknown_patch_operation:{operation}", RiskLevel.HIGH))
    for surface in value.changed_surfaces:
        if surface in _FORBIDDEN:
            matches.append((f"forbidden_surface:{surface}", RiskLevel.FORBIDDEN))
        elif surface in _MEDIUM:
            matches.append((f"instruction_or_policy:{surface}", RiskLevel.MEDIUM))
        elif surface in _LOW:
            matches.append((f"bounded_declarative:{surface}", RiskLevel.LOW))
        else:
            matches.append((f"unknown_surface:{surface}", RiskLevel.HIGH))
    if value.kind is CandidateKind.EXECUTABLE_COMPONENT:
        matches.append(("executable_component", RiskLevel.HIGH))
    if value.capability_manifest.network_required or value.capability_manifest.subprocess_required:
        matches.append(("privileged_capability", RiskLevel.HIGH))
    if not matches:
        matches.append(("empty_change", RiskLevel.HIGH))
    level = max((risk for _, risk in matches), key=_RANK.__getitem__)
    return RiskAssessment(
        "loom.risk-assessment.v1",
        candidate_id,
        level,
        tuple(rule for rule, _ in matches),
        rule_set,
        utc_now(),
    )


__all__ = ["RiskInput", "classify_risk"]
