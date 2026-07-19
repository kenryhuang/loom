from __future__ import annotations

from loom.campaigns.contracts import ArtifactRef, CandidateKind, CapabilityManifest, RiskLevel
from loom.governance.risk import RiskInput, classify_risk


def _rules() -> ArtifactRef:
    return ArtifactRef("risk-rules.v1", "risk_rules", "risk-rules.json", "a" * 64, 1)


def _capability(**overrides) -> CapabilityManifest:
    values = {
        "imports": (),
        "dependencies": (),
        "filesystem_read_roots": (),
        "filesystem_write_roots": (),
        "callable_tools": (),
        "input_schema": {},
        "output_schema": {},
        "resource_limits": {},
        **overrides,
    }
    return CapabilityManifest(**values)


def test_risk_classification_uses_highest_matching_rule_and_records_all_matches():
    cases = (
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("context_numeric_limit",), ("set_limit",), _capability()), RiskLevel.LOW),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("system_prompt",), ("replace",), _capability()), RiskLevel.MEDIUM),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("completion_policy",), ("set",), _capability()), RiskLevel.MEDIUM),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("agent.loop_policy",), ("set_limit",), _capability()), RiskLevel.LOW),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("agent.system_prompt",), ("replace",), _capability()), RiskLevel.MEDIUM),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("agent.tool_policy",), ("set",), _capability()), RiskLevel.MEDIUM),
        (
            RiskInput(CandidateKind.DECLARATIVE_PATCH, ("models.solver.request_options",), ("set",), _capability()),
            RiskLevel.MEDIUM,
        ),
        (RiskInput(CandidateKind.EXECUTABLE_COMPONENT, ("context_policy",), (), _capability(imports=("json",))), RiskLevel.HIGH),
        (RiskInput(CandidateKind.DECLARATIVE_PATCH, ("evaluator", "context_numeric_limit"), ("set",), _capability()), RiskLevel.FORBIDDEN),
    )

    for index, (risk_input, expected) in enumerate(cases):
        result = classify_risk(f"cand_{index}", risk_input, _rules())
        assert result.level is expected
        assert result.matched_rules
        assert result.rule_set == _rules()


def test_candidate_declared_risk_is_not_an_input_to_authoritative_classifier():
    risk_input = RiskInput(CandidateKind.EXECUTABLE_COMPONENT, ("context_policy",), (), _capability(), declared_risk="low")

    result = classify_risk("cand_exec", risk_input, _rules())

    assert result.level is RiskLevel.HIGH
    assert "executable_component" in result.matched_rules
