from __future__ import annotations

import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from loom.core import err, make_loom_error, ok
from loom.evaluation.judge import ROUND_JUDGE_DIMENSIONS
from loom.llm import LlmResponse, TokenUsage
from loom.optimize.cli import OptimizeCliOptions, run_optimize
from loom.optimize.cli import main as optimize_main
from loom.optimize.tui_state import OptimizeTuiCollector
from loom.optimize.ui import JsonObserver, OptimizeTuiObserver


@dataclass
class FakeProvider:
    role: str
    model: str = "fake-model"
    messages: list[tuple] = field(default_factory=list)
    fail_once: bool = False
    proposal_surface: str = "agent.loop_policy"

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        del tools, cancellation, tool_choice
        self.messages.append(tuple(messages))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(f"interrupted {self.role} call")
        if self.role == "proposer":
            if self.proposal_surface == "agent.system_prompt":
                operation = {
                    "op": "set",
                    "path": "agent.system_prompt.system_prompt_addendum",
                    "value": "Cite the inspected path in the final report.",
                }
            else:
                operation = {
                    "op": "set_limit",
                    "path": "agent.loop_policy.max_history_steps",
                    "value": 4,
                }
            content = {
                "drafts": [
                    {
                        "kind": "declarative_patch",
                        "parent_ids": [],
                        "inspiration_ids": [],
                        "changed_surfaces": [self.proposal_surface],
                        "operations": [operation],
                        "hypothesis": {
                            "problem": "Long histories retain distracting evidence.",
                            "mechanism": "Use a smaller bounded history window.",
                            "expected_improvements": [{"metric_id": "task_success_rate", "expected_delta": 0.0}],
                            "expected_regressions": [{"metric_id": "wall_time_ms", "expected_delta": 0.0}],
                            "preserved_behaviors": ["Task completion remains available."],
                        },
                    }
                ]
            }
        elif self.role == "judge":
            system = (messages[0].content or "").lower()
            dimensions = (
                {name: 0.8 for name in ROUND_JUDGE_DIMENSIONS}
                if "round-level evaluator" in system
                else {
                    "task_progress": 0.8,
                    "instruction_following": 0.8,
                    "tool_selection": 0.8,
                    "tool_arguments": 0.8,
                    "tool_result_handling": 0.8,
                    "evidence_grounding": 0.8,
                    "context_quality": 0.8,
                    "efficiency": 0.8,
                    "recovery": 1.0,
                }
            )
            content = {"overall": 0.8, "dimensions": dimensions, "findings": [], "confidence": 0.9}
        else:
            content = {
                "reasoning": "The isolated fixture is healthy.",
                "action": {"kind": "none", "description": "done", "target": None, "input": {}},
                "alternatives": [],
                "confidence": 0.9,
            }
        return ok(LlmResponse(content=json.dumps(content), usage=TokenUsage(2, 3, 5)))


def _write_seed_trace(path: Path) -> None:
    records = [
        {
            "type": "event",
            "eventType": "run.started",
            "traceId": None,
            "payload": {"type": "run.started", "run_id": "run-seed", "loop_id": "loop-seed"},
            "hash": "run-start",
        },
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-seed",
            "payload": {
                "type": "step.started",
                "run_id": "run-seed",
                "loop_id": "loop-seed",
                "trace_id": "trace-seed",
                "step_number": 0,
            },
            "hash": "step-start",
        },
        {
            "type": "event",
            "eventType": "llm.requested",
            "traceId": "trace-seed",
            "payload": {
                "type": "llm.requested",
                "run_id": "run-seed",
                "loop_id": "loop-seed",
                "trace_id": "trace-seed",
                "step_number": 0,
                "llm_call_id": "llm-seed",
                "messages": [{"role": "user", "content": "Audit a small fixture."}],
            },
            "hash": "llm-request",
        },
        {
            "type": "event",
            "eventType": "llm.completed",
            "traceId": "trace-seed",
            "payload": {
                "type": "llm.completed",
                "run_id": "run-seed",
                "loop_id": "loop-seed",
                "trace_id": "trace-seed",
                "step_number": 0,
                "llm_call_id": "llm-seed",
                "response": {"content": "Fixture audited.", "usage": {"total_tokens": 5}},
            },
            "hash": "llm-completed",
        },
        {
            "type": "event",
            "eventType": "step.completed",
            "traceId": "trace-seed",
            "payload": {
                "type": "step.completed",
                "run_id": "run-seed",
                "loop_id": "loop-seed",
                "trace_id": "trace-seed",
                "step_number": 0,
            },
            "hash": "step-completed",
        },
        {
            "type": "trace",
            "id": "trace-seed",
            "runId": "run-seed",
            "payload": {
                "id": "trace-seed",
                "run_id": "run-seed",
                "loop_id": "loop-seed",
                "step_number": 0,
                "outcome": "pass",
            },
            "hash": "trace-completed",
        },
        {
            "type": "event",
            "eventType": "run.completed",
            "traceId": None,
            "payload": {"type": "run.completed", "run_id": "run-seed", "loop_id": "loop-seed"},
            "hash": "run-completed",
        },
    ]
    path.write_text("\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n", encoding="utf-8")


def _build_fixture(root: Path) -> tuple[Path, Path, Path]:
    trace = root / "seed.jsonl"
    tasks = root / "tasks.jsonl"
    config = root / "config.yaml"
    _write_seed_trace(trace)
    rows = []
    objectives = (
        "Confirm the atlas catalog includes an ownership heading.",
        "Check the bakery ledger documents its retry convention.",
        "Review the comet parser for a stated input boundary.",
        "Inspect the delta worker for a shutdown guarantee.",
        "Verify the ember index names its storage format.",
        "Assess the forest queue for ordering documentation.",
        "Examine the glacier cache for an eviction statement.",
        "Audit the harbor client for timeout guidance.",
        "Validate the iris service describes its health probe.",
    )
    for index in range(9):
        workspace = root / "projects" / f"fixture-{index}"
        workspace.mkdir(parents=True)
        (workspace / "README.md").write_text(f"# Independent fixture {index}\n", encoding="utf-8")
        rows.append(
            {
                "task_id": f"fixture-{index}",
                "objective": objectives[index],
                "workspace": str(workspace),
                "profile": "project_audit",
                "expected_outputs": [f"Evidence note {chr(ord('a') + index)}."],
            }
        )
    tasks.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    config.write_text(
        """
default_model: solver
models:
  proposer:
    provider: openai
    model: fake-proposer
    base_url: https://example.invalid/v1
    api_key: test
  solver:
    provider: openai
    model: fake-solver
    base_url: https://example.invalid/v1
    api_key: test
  judge:
    provider: openai
    model: fake-judge
    base_url: https://example.invalid/v1
    api_key: test
meta_harness:
  proposer_model: proposer
  solver_model: solver
  judge_model: judge
  search:
    iterations: 1
    candidates_per_iteration: 1
    candidate_kinds: [declarative_patch]
    editable_surfaces: [agent.loop_policy]
  tasks:
    seed: 42
    repetitions: 3
    minimum_pairs: 5
  objectives:
    primary: task_success_rate
    constraints:
      max_regression_rate: 0.05
      max_cost_increase_ratio: 1000
      max_latency_increase_ratio: 1000
  budgets:
    max_candidates: 1
    max_llm_calls: 200
    max_wall_clock_minutes: 5
  execution:
    max_parallel_trials: 1
    trial_timeout_seconds: 30
    verifier_timeout_seconds: 5
  governance:
    mode: local
    auto_promote_max_risk: low
    require_approval_for: [medium, high, executable]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return trace, tasks, config


def test_optimize_command_runs_trace_to_governed_result(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    monkeypatch.setattr("loom.optimize.runtime.create_provider_from_task_config", fake_provider_factory)
    output = tmp_path / ".loom" / "optimize"

    exit_code = optimize_main(
        [
            "--trace",
            str(trace),
            "--tasks",
            str(tasks),
            "--config",
            str(config),
            "--output-dir",
            str(output),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0, captured.out + captured.err
    results = list(output.glob("opt_*/result.json"))
    assert len(results) == 1
    result = json.loads(results[0].read_text(encoding="utf-8"))
    assert result["disposition"] == "promoted"
    assert Path(result["report_path"]).is_file()
    report = Path(result["report_path"]).read_text(encoding="utf-8")
    assert '"seed_analysis"' in report
    assert '"task_side_runs"' in report
    assert '"risk": "low"' in report
    proposer_context = "\n".join(message.content or "" for call in providers["proposer"].messages for message in call)
    holdout_manifest = next(output.glob("opt_*/inputs/task-sets/holdout.jsonl"))
    holdout_rows = [json.loads(line) for line in holdout_manifest.read_text(encoding="utf-8").splitlines()]
    assert all(row["content"] not in proposer_context for row in holdout_rows)
    assert providers["solver"].messages
    ledger = {role: len(provider.messages) for role, provider in providers.items()}
    assert (
        optimize_main(
            [
                *[
                    "--trace",
                    str(trace),
                    "--tasks",
                    str(tasks),
                    "--config",
                    str(config),
                    "--output-dir",
                    str(output),
                    "--json",
                ],
                "--new-run",
            ]
        )
        == 1
    )
    assert "HOLDOUT_REUSE_FORBIDDEN" in capsys.readouterr().out
    assert {role: len(provider.messages) for role, provider in providers.items()} == ledger


def test_optimize_dry_run_prepares_inputs_without_model_or_campaign_calls(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    monkeypatch.setattr("loom.optimize.runtime.create_provider_from_task_config", fake_provider_factory)
    output = tmp_path / ".loom" / "optimize"

    assert (
        optimize_main(
            [
                "--trace",
                str(trace),
                "--tasks",
                str(tasks),
                "--config",
                str(config),
                "--output-dir",
                str(output),
                "--json",
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["disposition"] == "dry_run"
    assert payload["task_counts"] == {"discovery": 5, "validation": 2, "holdout": 2}
    assert all(not provider.messages for provider in providers.values())
    assert not list(output.glob("opt_*/campaign/campaign.json"))
    assert not list(output.glob("opt_*/result.json"))


def test_optimize_command_resumes_without_repeating_completed_model_calls(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}
    providers["proposer"].fail_once = True

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    monkeypatch.setattr("loom.optimize.runtime.create_provider_from_task_config", fake_provider_factory)
    output = tmp_path / ".loom" / "optimize"
    command = [
        "--trace",
        str(trace),
        "--tasks",
        str(tasks),
        "--config",
        str(config),
        "--output-dir",
        str(output),
        "--json",
    ]

    assert optimize_main(command) == 1
    capsys.readouterr()
    completed_seed_calls = [tuple(message.content for message in call) for call in providers["judge"].messages]
    assert completed_seed_calls

    assert optimize_main(command) == 0
    captured = capsys.readouterr()
    assert '"disposition":"promoted"' in captured.out
    assert '"type":"optimization.snapshot.loaded"' in captured.out
    assert '"type":"optimization.stage.replayed"' in captured.out
    all_judge_calls = [tuple(message.content for message in call) for call in providers["judge"].messages]
    assert all(all_judge_calls.count(call) == 1 for call in completed_seed_calls)
    proposer_context = "\n".join(message.content or "" for call in providers["proposer"].messages for message in call)
    holdout_manifest = next(output.glob("opt_*/inputs/task-sets/holdout.jsonl"))
    holdout_rows = [json.loads(line) for line in holdout_manifest.read_text(encoding="utf-8").splitlines()]
    assert all(row["content"] not in proposer_context for row in holdout_rows)

    ledger = {role: len(provider.messages) for role, provider in providers.items()}
    assert optimize_main(command) == 0
    capsys.readouterr()
    assert {role: len(provider.messages) for role, provider in providers.items()} == ledger


@pytest.mark.asyncio
async def test_tui_and_json_modes_receive_equivalent_safe_end_to_end_event_streams(tmp_path: Path, monkeypatch):
    async def execute(root: Path, observer, *, tui: bool, json_output: bool):
        root.mkdir()
        trace, tasks, config = _build_fixture(root)
        providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}

        def fake_provider_factory(task_config, *, model_name=None, **kwargs):
            del task_config, kwargs
            return ok(providers[model_name])

        monkeypatch.setattr("loom.optimize.runtime.create_provider_from_task_config", fake_provider_factory)
        result = await run_optimize(
            OptimizeCliOptions(
                "run",
                trace=trace,
                tasks=tasks,
                config=config,
                tui=tui,
                json=json_output,
                output_dir=root / "runs",
            ),
            observer=observer,
        )
        return result

    collector = OptimizeTuiCollector()
    tui_result = await execute(tmp_path / "tui", OptimizeTuiObserver(collector), tui=True, json_output=False)
    json_stream = io.StringIO()
    json_result = await execute(tmp_path / "json", JsonObserver(json_stream), tui=False, json_output=True)
    json_events = [json.loads(line) for line in json_stream.getvalue().splitlines()]

    assert tui_result.ok and json_result.ok
    assert tui_result.value.disposition == json_result.value.disposition
    assert [event["type"] for event in collector.state.recent_events] == [event["type"] for event in json_events]
    assert any(event["type"] == "optimization.candidate.admitted" for event in json_events)
    assert any(event["type"] == "optimization.trial.started" for event in json_events)
    forbidden = {"task", "workspace", "expected_output", "verifier", "score", "judge_rationale", "rationale"}
    for event in json_events:
        if event.get("scope", {}).get("phase") == "holdout" or event["type"].startswith("optimization.holdout"):
            assert not forbidden.intersection(_nested_keys(event["payload"]))


def _nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(_nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_nested_keys(item) for item in value)) if value else set()
    return set()


def test_optimize_resume_reuses_completed_seed_evaluation(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    import loom.optimize.runtime as runtime_module

    original_evolution = runtime_module.analyze_evolution_trace
    fail_once = True

    async def interrupted_evolution(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            return err(make_loom_error("INJECTED_INTERRUPTION", "interrupt after seed evaluation", retryable=True))
        return await original_evolution(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "create_provider_from_task_config", fake_provider_factory)
    monkeypatch.setattr(runtime_module, "analyze_evolution_trace", interrupted_evolution)
    output = tmp_path / ".loom" / "optimize"
    command = [
        "--trace",
        str(trace),
        "--tasks",
        str(tasks),
        "--config",
        str(config),
        "--output-dir",
        str(output),
        "--json",
    ]

    assert optimize_main(command) == 1
    capsys.readouterr()
    completed_seed_calls = Counter(tuple(message.content for message in call) for call in providers["judge"].messages)
    assert completed_seed_calls

    assert optimize_main(command) == 0
    capsys.readouterr()
    all_judge_calls = Counter(tuple(message.content for message in call) for call in providers["judge"].messages)
    assert all(all_judge_calls[call] == count for call, count in completed_seed_calls.items())


def test_optimize_resume_reuses_published_validation_experiment(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    import loom.optimize.runtime as runtime_module

    original_publish = runtime_module.publish_phase_results
    fail_validation = True

    def publish_with_interruption(store, spec, phase, cohort_digest, experiment_refs):
        nonlocal fail_validation
        if phase == "validation" and fail_validation:
            fail_validation = False
            return err(make_loom_error("INJECTED_INTERRUPTION", "interrupt after validation experiment", retryable=True))
        return original_publish(store, spec, phase, cohort_digest, experiment_refs)

    monkeypatch.setattr(runtime_module, "create_provider_from_task_config", fake_provider_factory)
    monkeypatch.setattr(runtime_module, "publish_phase_results", publish_with_interruption)
    output = tmp_path / ".loom" / "optimize"
    command = [
        "--trace",
        str(trace),
        "--tasks",
        str(tasks),
        "--config",
        str(config),
        "--output-dir",
        str(output),
        "--json",
    ]

    assert optimize_main(command) == 1
    capsys.readouterr()
    completed_solver_calls = Counter(tuple(message.content for message in call) for call in providers["solver"].messages)
    assert completed_solver_calls
    call_count = len(providers["solver"].messages)

    assert optimize_main(command) == 0
    capsys.readouterr()
    all_solver_calls = Counter(tuple(message.content for message in call) for call in providers["solver"].messages)
    assert all(all_solver_calls[call] == count for call, count in completed_solver_calls.items())
    assert len(providers["solver"].messages) - call_count == 12


def test_optimize_approval_is_explicit_and_resumes_to_promotion(tmp_path: Path, monkeypatch, capsys):
    trace, tasks, config = _build_fixture(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "editable_surfaces: [agent.loop_policy]",
            "editable_surfaces: [agent.system_prompt]",
        ),
        encoding="utf-8",
    )
    providers = {role: FakeProvider(role) for role in ("proposer", "solver", "judge")}
    providers["proposer"].proposal_surface = "agent.system_prompt"

    def fake_provider_factory(task_config, *, model_name=None, **kwargs):
        del task_config, kwargs
        return ok(providers[model_name])

    monkeypatch.setattr("loom.optimize.runtime.create_provider_from_task_config", fake_provider_factory)
    output = tmp_path / ".loom" / "optimize"
    command = [
        "--trace",
        str(trace),
        "--tasks",
        str(tasks),
        "--config",
        str(config),
        "--output-dir",
        str(output),
        "--json",
    ]

    assert optimize_main(command) == 2
    capsys.readouterr()
    result_path = next(output.glob("opt_*/result.json"))
    pending = json.loads(result_path.read_text(encoding="utf-8"))
    assert pending["disposition"] == "awaiting_approval"
    optimization_id = pending["optimization_id"]
    candidate_id = pending["selected_candidate_id"]

    assert (
        optimize_main(
            [
                "approve",
                optimization_id,
                "--candidate",
                candidate_id,
                "--identity",
                "approver",
                "--reason",
                "Reviewed bounded prompt wording and evidence.",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    promoted = json.loads(result_path.read_text(encoding="utf-8"))
    assert promoted["disposition"] == "promoted"
    ledger = {role: len(provider.messages) for role, provider in providers.items()}
    assert optimize_main(command) == 0
    capsys.readouterr()
    assert {role: len(provider.messages) for role, provider in providers.items()} == ledger
