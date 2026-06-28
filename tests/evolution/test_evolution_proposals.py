from dataclasses import replace

from loom.evolution.proposals import ProposalGateConfig, aggregate_step_scores, gate_proposal, generate_evolution_proposals
from loom.evolution.scoring import StepScore
from loom.llm import TokenUsage


def _score(trace_id, surface="system_prompt", confidence=0.8, overall=0.4, explanation="Output contract is unclear.", step_number=0):
    return StepScore(
        run_id="run-1",
        trace_id=trace_id,
        step_number=step_number,
        overall=overall,
        dimensions={"prompt_following": 0.3},
        attribution={surface: (explanation,)},
        proposed_fixes=("Clarify JSON output contract.",),
        evidence_event_hashes=(f"hash-{trace_id}",),
        confidence=confidence,
        evaluator_model="fake-score-model",
        token_usage=TokenUsage(),
    )


def test_aggregate_step_scores_requires_repeated_surface():
    signals = aggregate_step_scores((_score("trace-1"), _score("trace-2"), _score("trace-3", surface="tool_schema")), min_frequency=2)

    assert len(signals) == 1
    assert signals[0].surface == "system_prompt"
    assert signals[0].frequency == 2
    assert signals[0].trace_ids == ("trace-1", "trace-2")
    assert signals[0].confidence == 0.8


def test_generate_evolution_proposals_creates_bounded_prompt_proposal():
    signals = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)

    proposals = generate_evolution_proposals(signals, max_proposals=1)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.surface == "system_prompt"
    assert proposal.kind == "prompt_rule"
    assert proposal.created_from_trace_ids == ("trace-1", "trace-2")
    assert proposal.reversible is True
    assert proposal.patch["operation"] == "add_rule"


def test_gate_proposal_rejects_low_confidence_signal():
    signal = aggregate_step_scores((_score("trace-1", confidence=0.4), _score("trace-2", confidence=0.4)), min_frequency=2)[0]
    proposal = generate_evolution_proposals((signal,), max_proposals=1)[0]

    result = gate_proposal(proposal, ProposalGateConfig(min_confidence=0.7))

    assert not result.ok
    assert result.error.code == "MUTATION_REJECTED"


def test_aggregate_step_scores_ignores_whitespace_only_attribution():
    signals = aggregate_step_scores(
        (
            _score("trace-1", explanation="  "),
            _score("trace-2", explanation="\n\t"),
        ),
        min_frequency=2,
    )

    assert signals == ()


def test_aggregate_step_scores_deduplicates_same_step_score_artifacts():
    score = _score("trace-1")
    duplicate = _score("trace-1")

    signals = aggregate_step_scores((score, duplicate), min_frequency=2)

    assert signals == ()


def test_generate_evolution_proposals_uses_stable_id_for_reordered_evidence():
    forward_signal = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)[0]
    reversed_signal = aggregate_step_scores((_score("trace-2"), _score("trace-1")), min_frequency=2)[0]

    forward = generate_evolution_proposals((forward_signal,), max_proposals=1)[0]
    reversed_ = generate_evolution_proposals((reversed_signal,), max_proposals=1)[0]

    assert forward.id == reversed_.id


def test_gate_proposal_rejects_non_reversible_proposal_when_required():
    signal = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)[0]
    proposal = replace(generate_evolution_proposals((signal,), max_proposals=1)[0], reversible=False)

    result = gate_proposal(proposal, ProposalGateConfig(require_reversible=True))

    assert not result.ok
    assert result.error.code == "MUTATION_REJECTED"


def test_gate_proposal_rejects_high_risk_proposal_above_configured_max():
    signal = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)[0]
    proposal = replace(generate_evolution_proposals((signal,), max_proposals=1)[0], risk="high")

    result = gate_proposal(proposal, ProposalGateConfig(max_risk="medium"))

    assert not result.ok
    assert result.error.code == "MUTATION_REJECTED"


def test_gate_proposal_rejects_invalid_max_risk_config():
    signal = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)[0]
    proposal = replace(generate_evolution_proposals((signal,), max_proposals=1)[0], risk="high")

    result = gate_proposal(proposal, ProposalGateConfig(max_risk="medum"))

    assert not result.ok
    assert result.error.code == "MUTATION_REJECTED"


def test_generate_evolution_proposals_clamps_negative_max_proposals_to_zero():
    signals = aggregate_step_scores(
        (
            _score("trace-1", surface="system_prompt"),
            _score("trace-2", surface="system_prompt"),
            _score("trace-3", surface="tool_schema"),
            _score("trace-4", surface="tool_schema"),
        ),
        min_frequency=2,
    )

    proposals = generate_evolution_proposals(signals, max_proposals=-1)

    assert proposals == ()
