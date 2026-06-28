import asyncio
import json
import sys
from pathlib import Path

from loom.core.models import ok
from loom.evolution.analyze import AnalyzeConfig, analyze_trace
from loom.evolution.scoring import SCORE_DIMENSIONS
from loom.examples.real_project_smoke import RealProjectSmokeConfig, run_real_project_smoke
from loom.llm import LlmResponse, LlmToolCall, TokenUsage


class FakeSmokeProvider:
    model = "fake-smoke-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        if self.calls == 1:
            return ok(
                LlmResponse(
                    content=None,
                    tool_calls=(
                        LlmToolCall("inspect-call", "inspect-project", "{}"),
                        LlmToolCall("smoke-call", "run-smoke-test", "{}"),
                    ),
                    finish_reason="tool_calls",
                )
            )
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "reasoning": "I judged the real smoke evidence.",
                        "action": {
                            "kind": "custom",
                            "description": "Write the smoke audit report",
                            "input": {
                                "report": "# Fake LLM Smoke Report\n\nThe persisted trace contains smoke evidence."
                            },
                        },
                        "alternatives": [],
                        "confidence": 0.84,
                    }
                ),
                usage=TokenUsage(10, 10, 20),
            )
        )


class FakeEvolutionScoreProvider:
    model = "fake-evolution-score-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "overall": 0.35,
                        "dimensions": {dimension: 0.3 for dimension in SCORE_DIMENSIONS},
                        "attribution": {"system_prompt": ["The smoke loop should request clearer evidence."]},
                        "proposed_fixes": ["Clarify what evidence the smoke report should include."],
                        "confidence": 0.9,
                    }
                ),
                usage=TokenUsage(4, 5, 9),
            )
        )


def test_real_project_smoke_trace_can_be_analyzed_for_evolution(tmp_path: Path):
    async def scenario():
        project = tmp_path / "sample"
        project.mkdir()
        (project / "README.md").write_text("# Sample\n\nA tiny sample project.\n", encoding="utf-8")
        (project / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")
        trace_path = tmp_path / "traces" / "smoke.jsonl"
        smoke_provider = FakeSmokeProvider()

        smoke = await run_real_project_smoke(
            RealProjectSmokeConfig(
                target_path=project,
                smoke_command=(sys.executable, "-c", "print('smoke ok')"),
                cli_smoke_enabled=False,
                command_timeout_seconds=10,
                trace_path=trace_path,
            ),
            provider=smoke_provider,
            llm=True,
        )

        assert smoke.ok
        assert trace_path.exists()
        assert smoke_provider.calls == 2

        score_provider = FakeEvolutionScoreProvider()
        analyzed = await analyze_trace(
            AnalyzeConfig(
                trace_path=trace_path,
                out_dir=tmp_path / "evolution",
                min_signal_frequency=1,
            ),
            provider=score_provider,
        )

        assert analyzed.ok
        assert analyzed.value.episodes
        assert analyzed.value.scores
        assert analyzed.value.signals
        assert analyzed.value.proposals
        assert score_provider.calls == len(analyzed.value.episodes)
        assert analyzed.value.artifacts.scores_path.exists()
        assert analyzed.value.artifacts.signals_path.exists()
        assert analyzed.value.artifacts.proposals_path.exists()
        assert analyzed.value.artifacts.report_path.exists()
        assert "Trace Driven Evolution Report" in analyzed.value.artifacts.report_path.read_text(encoding="utf-8")

    asyncio.run(scenario())
