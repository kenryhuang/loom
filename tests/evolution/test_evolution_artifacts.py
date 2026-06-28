import json
from pathlib import Path

from loom.evolution.artifacts import EvolutionArtifacts, render_evolution_report, write_evolution_artifacts
from loom.evolution.proposals import EvolutionProposal, EvolutionSignal
from loom.evolution.scoring import StepScore
from loom.llm import TokenUsage


def _score():
    return StepScore(
        run_id="run-1",
        trace_id="trace-1",
        step_number=0,
        overall=0.5,
        dimensions={"prompt_following": 0.4},
        attribution={"system_prompt": ("Output contract unclear.",)},
        proposed_fixes=("Clarify output contract.",),
        evidence_event_hashes=("hash-1",),
        confidence=0.8,
        evaluator_model="fake-score-model",
        token_usage=TokenUsage(1, 2, 3),
    )


def _signal():
    return EvolutionSignal(
        "repeated_attribution",
        "system_prompt",
        0.5,
        2,
        ("trace-1", "trace-2"),
        "Output contract unclear.",
        0.8,
        ("hash-1",),
    )


def _proposal():
    return EvolutionProposal(
        id="proposal-1",
        surface="system_prompt",
        kind="prompt_rule",
        title="Improve system prompt",
        rationale="Output contract unclear.",
        created_from_trace_ids=("trace-1",),
        expected_impact={"severity": 0.5},
        risk="low",
        reversible=True,
        ttl_runs=10,
        patch={"operation": "add_rule", "text": "Return valid JSON."},
        confidence=0.8,
        evidence_event_hashes=("hash-1",),
    )


def test_write_evolution_artifacts_writes_jsonl_streams(tmp_path):
    artifacts = write_evolution_artifacts(tmp_path, (_score(),), (_signal(),), (_proposal(),))

    assert isinstance(artifacts, EvolutionArtifacts)
    assert artifacts.scores_path.exists()
    assert artifacts.signals_path.exists()
    assert artifacts.proposals_path.exists()
    score_record = json.loads(artifacts.scores_path.read_text(encoding="utf-8").splitlines()[0])
    assert score_record["trace_id"] == "trace-1"
    assert score_record["token_usage"]["total_tokens"] == 3


def test_render_evolution_report_summarizes_counts_and_proposals():
    report = render_evolution_report((_score(),), (_signal(),), (_proposal(),))

    assert "Trace Driven Evolution Report" in report
    assert "scores: 1" in report
    assert "signals: 1" in report
    assert "proposals: 1" in report
    assert "Improve system prompt" in report


def test_render_evolution_report_includes_operator_context():
    report = render_evolution_report((_score(),), (_signal(),), (_proposal(),))

    assert "## Signals" in report
    assert "surface: system_prompt" in report
    assert "kind: repeated_attribution" in report
    assert "severity: 0.5" in report
    assert "frequency: 2" in report
    assert "confidence: 0.8" in report
    assert "trace_ids: trace-1, trace-2" in report
    assert "evidence_event_hashes: hash-1" in report
    assert "Output contract unclear." in report
    assert "created_from_trace_ids: trace-1" in report
    assert '"severity": 0.5' in report
    assert "reversible: True" in report
    assert "ttl_runs: 10" in report
    assert "```json" in report
    assert '"operation": "add_rule"' in report
    assert '"text": "Return valid JSON."' in report


def test_write_evolution_artifacts_accepts_single_pass_generators(tmp_path):
    artifacts = write_evolution_artifacts(
        tmp_path,
        (_score() for _ in range(1)),
        (_signal() for _ in range(1)),
        (_proposal() for _ in range(1)),
    )

    assert len(artifacts.scores_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(artifacts.signals_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(artifacts.proposals_path.read_text(encoding="utf-8").splitlines()) == 1
    report = artifacts.report_path.read_text(encoding="utf-8")
    assert "scores: 1" in report
    assert "signals: 1" in report
    assert "proposals: 1" in report


def test_jsonl_serialization_handles_nested_paths_and_sequences(tmp_path):
    proposal = EvolutionProposal(
        id="proposal-1",
        surface="system_prompt",
        kind="prompt_rule",
        title="Improve system prompt",
        rationale="Output contract unclear.",
        created_from_trace_ids=("trace-1",),
        expected_impact={"severity": 0.5, "paths": (Path("one"), Path("two"))},
        risk="low",
        reversible=True,
        ttl_runs=10,
        patch={
            "operation": "add_rule",
            "text": "Return valid JSON.",
            "metadata": {"paths": [Path("rules/system.md")], "tags": ("json", "contract")},
        },
        confidence=0.8,
        evidence_event_hashes=("hash-1",),
    )

    artifacts = write_evolution_artifacts(tmp_path, (_score(),), (_signal(),), (proposal,))
    proposal_record = json.loads(artifacts.proposals_path.read_text(encoding="utf-8").splitlines()[0])

    assert proposal_record["expected_impact"]["paths"] == ["one", "two"]
    assert proposal_record["patch"]["metadata"]["paths"] == ["rules/system.md"]
    assert proposal_record["patch"]["metadata"]["tags"] == ["json", "contract"]
