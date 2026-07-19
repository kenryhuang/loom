from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import ArtifactRef
from loom.campaigns.experiments import TrialExecution, ValidatedCandidate
from loom.campaigns.task_sets import TaskManifestRow
from loom.core import err, make_loom_error, ok
from loom.evaluation.experiments import TrialEntry
from loom.llm import LlmResponse, TokenUsage
from loom.optimize.control import OptimizeControlInterrupt, OptimizeRunControl
from loom.optimize.events import OptimizationEventEmitter
from loom.optimize.task_sets import OptimizeTask, PreparedTask, VerifierSpec, WorkspaceSnapshot
from loom.optimize.trial_executor import OptimizeTrialExecutor
from loom.tasks import TaskHarness


@dataclass(frozen=True)
class RecordingSolverProvider:
    model: str = "solver-model"
    request_options: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})
    calls: list[dict[str, Any]] = field(default_factory=list)
    failure: bool = False

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        del cancellation, tool_choice
        self.calls.append(
            {
                "request_options": self.request_options,
                "tools": tuple(tool["function"]["name"] for tool in tools or ()),
                "system": messages[0].content,
            }
        )
        if self.failure:
            return err(make_loom_error("LLM_FAILED", "provider unavailable", retryable=True))
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "reasoning": "The task was completed.",
                        "action": {"kind": "none", "description": "done", "target": None, "input": {}},
                        "alternatives": [],
                        "confidence": 0.9,
                    }
                ),
                usage=TokenUsage(5, 7, 12),
            )
        )


@dataclass(frozen=True)
class CancellableSolverProvider(RecordingSolverProvider):
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        del messages, tools, tool_choice
        self.started.set()
        while cancellation is None or not cancellation.cancelled:
            await asyncio.sleep(0.005)
        self.cancelled.set()
        return err(make_loom_error("ABORTED", "provider cancelled", retryable=False))


def _prepared_task(tmp_path: Path, *, verifier_exit: int = 0) -> tuple[SimpleNamespace, TrialEntry]:
    snapshot = tmp_path / f"snapshot-{verifier_exit}"
    snapshot.mkdir()
    (snapshot / "README.md").write_text("# Trial workspace\n", encoding="utf-8")
    verifier = VerifierSpec((sys.executable, "-c", f"raise SystemExit({verifier_exit})"), timeout_ms=5_000)
    task = OptimizeTask("task-1", "Inspect the project", snapshot, verifier=verifier)
    workspace = WorkspaceSnapshot("a" * 64, snapshot, 1, 18)
    row = TaskManifestRow("task-1", "tester", "trace:seed", "discovery", workspace.digest, "b" * 64, "sanitizer-v1", task.objective)
    prepared = PreparedTask(task, workspace, row, "fp-task")
    return SimpleNamespace(task_by_fingerprint={"fp-task": prepared}), TrialEntry("fp-task", seed=42, repetition=0)


def _candidate() -> ValidatedCandidate:
    artifact = ArtifactRef("candidate.v1", "candidate", "candidate.json", "c" * 64, 1)
    baseline = ArtifactRef("baseline.v1", "baseline", "baseline.json", "d" * 64, 1)
    return ValidatedCandidate("cmp_test", "cand_test", artifact, baseline, "env", "solver", "tools", "permissions", "evaluator")


def _executor(
    tmp_path: Path,
    *,
    verifier_exit: int = 0,
    provider: RecordingSolverProvider | None = None,
    event_emitter=None,
    control=None,
):
    tasks, entry = _prepared_task(tmp_path, verifier_exit=verifier_exit)
    solver = provider or RecordingSolverProvider()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    executor = OptimizeTrialExecutor(
        tasks,
        artifacts,
        workspace_root=tmp_path / "trials",
        solver_provider=solver,
        candidate_harnesses={
            "cand_test": TaskHarness(
                system_prompt_addendum="Candidate overlay marker.",
                allowed_tools=("read_file",),
                request_options={"enable_thinking": True},
            )
        },
        trial_timeout_seconds=10,
        verifier_timeout_seconds=5,
        event_emitter=event_emitter,
        event_scope={"phase": "discovery", "experiment_id": "exp_test"},
        control=control,
    )
    return executor, solver, artifacts, entry


class RecordingObserver:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_trial_executor_materializes_fresh_paired_workspaces_and_applies_only_candidate_overlay(tmp_path: Path):
    executor, provider, artifacts, entry = _executor(tmp_path)
    candidate = _candidate()

    baseline = await executor.execute("baseline", candidate, entry, "trial-1")
    changed = await executor.execute("candidate", candidate, entry, "trial-1")

    assert isinstance(baseline, TrialExecution) and baseline.failure_kind is None
    assert changed.failure_kind is None
    assert baseline.metrics["task_success_rate"] == 1.0
    assert changed.metrics["task_success_rate"] == 1.0
    assert executor.workspace_for("trial-1", "baseline") != executor.workspace_for("trial-1", "candidate")
    assert (executor.workspace_for("trial-1", "baseline") / "README.md").is_file()
    assert (executor.workspace_for("trial-1", "candidate") / "README.md").is_file()
    assert "Candidate overlay marker." not in provider.calls[0]["system"]
    assert provider.calls[0]["request_options"] == {"enable_thinking": False}
    assert provider.calls[1]["request_options"] == {"enable_thinking": True}
    assert provider.calls[1]["tools"] == ("read_file",)
    assert baseline.trace_ref is not None and artifacts.read_bytes(baseline.trace_ref).ok
    assert baseline.evaluation_ref is not None
    evaluation = json.loads(artifacts.read_bytes(baseline.evaluation_ref).unwrap())
    assert evaluation["schema_version"] == "loom.evaluation.bundle.v1"
    assert baseline.metrics["total_tokens"] == 12.0
    assert baseline.metrics["wall_time_ms"] >= 0.0


@pytest.mark.asyncio
async def test_trial_executor_emits_scoped_trial_and_nested_runtime_events(tmp_path: Path):
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)
    executor, _provider, _artifacts, entry = _executor(tmp_path, event_emitter=emitter)

    result = await executor.execute("candidate", _candidate(), entry, "trial-1")

    assert result.failure_kind is None
    started = next(event for event in observer.events if event["type"] == "optimization.trial.started")
    assert dict(started["scope"]) == {
        "phase": "discovery",
        "experiment_id": "exp_test",
        "candidate_id": "cand_test",
        "trial_id": "trial-1",
        "side": "candidate",
        "task_id": "task-1",
        "repetition": 0,
    }
    assert any(event["type"] == "optimization.runtime.event" for event in observer.events)
    assert any(event["type"] == "optimization.trial.completed" for event in observer.events)


@pytest.mark.asyncio
async def test_trial_executor_stops_before_scheduling_a_new_pair_when_pause_requested(tmp_path: Path):
    control = OptimizeRunControl()
    control.request_pause("operator pause")
    executor, _provider, _artifacts, entry = _executor(tmp_path, control=control)

    with pytest.raises(OptimizeControlInterrupt):
        await executor.execute("baseline", _candidate(), entry, "trial-1")

    assert not (tmp_path / "trials").exists()


@pytest.mark.asyncio
async def test_trial_executor_cancels_in_flight_provider_without_trial_failure_evidence(tmp_path: Path):
    control = OptimizeRunControl()
    provider = CancellableSolverProvider()
    executor, _provider, _artifacts, entry = _executor(tmp_path, provider=provider, control=control)

    running = asyncio.create_task(executor.execute("candidate", _candidate(), entry, "trial-1"))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    control.request_cancel("operator cancel")

    with pytest.raises(OptimizeControlInterrupt):
        await asyncio.wait_for(running, timeout=1)
    assert provider.cancelled.is_set()


@pytest.mark.asyncio
async def test_verifier_failure_is_scored_behavior_not_infrastructure(tmp_path: Path):
    executor, _provider, artifacts, entry = _executor(tmp_path, verifier_exit=3)

    result = await executor.execute("candidate", _candidate(), entry, "trial-fail")

    assert result.failure_kind is None
    assert result.metrics["task_success_rate"] == 0.0
    assert result.trace_ref is not None and artifacts.read_bytes(result.trace_ref).ok
    assert result.evaluation_ref is not None and artifacts.read_bytes(result.evaluation_ref).ok


@pytest.mark.asyncio
async def test_retryable_provider_error_is_infrastructure_failure(tmp_path: Path):
    executor, _provider, _artifacts, entry = _executor(tmp_path, provider=RecordingSolverProvider(failure=True))

    result = await executor.execute("baseline", _candidate(), entry, "trial-provider-error")

    assert result.failure_kind == "infrastructure"
    assert result.failure_code == "LLM_FAILED"


@pytest.mark.asyncio
async def test_verifier_uses_argv_without_a_shell_and_repeated_attempts_get_fresh_workspaces(tmp_path: Path, monkeypatch):
    executor, _provider, _artifacts, entry = _executor(tmp_path)
    calls = []
    original = asyncio.create_subprocess_exec

    async def recording_exec(*argv, **kwargs):
        calls.append((argv, kwargs))
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)

    await executor.execute("baseline", _candidate(), entry, "trial-repeat")
    first = executor.workspace_for("trial-repeat", "baseline")
    await executor.execute("baseline", _candidate(), entry, "trial-repeat")
    second = executor.workspace_for("trial-repeat", "baseline")

    assert first != second
    assert all(call[0][:2] == (sys.executable, "-c") for call in calls)
    assert all("shell" not in call[1] for call in calls)
    assert all(call[1]["stdin"] is asyncio.subprocess.DEVNULL for call in calls)
