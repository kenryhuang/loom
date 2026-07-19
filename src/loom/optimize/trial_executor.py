"""Fresh-workspace task execution for paired Meta-Harness experiments."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.campaigns.experiments import TrialExecution, ValidatedCandidate
from loom.core import Result, make_loom_error
from loom.evaluation import EvaluationConfig, analyze_trace
from loom.evaluation.experiments import TrialEntry
from loom.optimize.task_sets import PreparedTaskSets, VerifierSpec
from loom.tasks import TaskHarness, TaskRequest, TaskRunOptions, run_generic_task


class OptimizeTrialExecutor:
    def __init__(
        self,
        prepared_tasks: PreparedTaskSets,
        artifact_store,
        *,
        workspace_root: str | Path,
        solver_provider: Any,
        candidate_harnesses: Mapping[str, TaskHarness],
        judge_provider: Any | None = None,
        trial_timeout_seconds: int = 900,
        verifier_timeout_seconds: int = 120,
    ):
        if trial_timeout_seconds < 1 or verifier_timeout_seconds < 1:
            raise ValueError("Trial and verifier timeouts must be positive")
        self.prepared_tasks = prepared_tasks
        self.artifact_store = artifact_store
        self.workspace_root = Path(workspace_root).resolve()
        self.solver_provider = solver_provider
        self.judge_provider = judge_provider
        self.candidate_harnesses = dict(candidate_harnesses)
        self.trial_timeout_seconds = trial_timeout_seconds
        self.verifier_timeout_seconds = verifier_timeout_seconds
        self._attempts: dict[tuple[str, str], int] = {}
        self._workspaces: dict[tuple[str, str], Path] = {}

    def workspace_for(self, trial_id: str, side: str) -> Path:
        return self._workspaces[(trial_id, side)]

    async def execute(
        self,
        side: str,
        candidate: ValidatedCandidate,
        entry: TrialEntry,
        trial_id: str,
    ) -> TrialExecution:
        started = time.monotonic()
        if side not in {"baseline", "candidate"}:
            return self._infrastructure(trial_id, side, "TRIAL_SIDE_INVALID", started)
        prepared = self.prepared_tasks.task_by_fingerprint.get(entry.task_fingerprint)
        if prepared is None:
            return self._infrastructure(trial_id, side, "TASK_FINGERPRINT_UNKNOWN", started)
        harness = TaskHarness()
        if side == "candidate":
            harness = self.candidate_harnesses.get(candidate.candidate_id)
            if harness is None:
                return self._infrastructure(trial_id, side, "CANDIDATE_HARNESS_MISSING", started)
        workspace = self._materialize_workspace(trial_id, side, prepared.snapshot.path)
        if isinstance(workspace, str):
            return self._infrastructure(trial_id, side, workspace, started)
        trace_path = workspace.parent / "trace.jsonl"
        evaluation_dir = workspace.parent / "evaluation"
        task = prepared.task
        request = TaskRequest(
            task.objective,
            workspace=workspace,
            profile=task.profile,
            constraints=task.constraints,
            expected_outputs=task.expected_outputs,
            risk_level=task.risk_level,
            metadata=task.metadata,
        )
        try:
            run_result = await asyncio.wait_for(
                run_generic_task(
                    request,
                    provider=self.solver_provider,
                    options=TaskRunOptions(
                        trace_path=trace_path,
                        timeout_ms=self.trial_timeout_seconds * 1000,
                    ),
                    harness=harness,
                ),
                timeout=self.trial_timeout_seconds,
            )
        except TimeoutError:
            return self._infrastructure(trial_id, side, "TIMEOUT", started)
        except Exception:
            return self._infrastructure(trial_id, side, "TASK_RUNNER_FAILED", started)
        task_ok = run_result.ok
        if not task_ok and run_result.error.retryable:
            return self._infrastructure(trial_id, side, run_result.error.code, started)
        if not trace_path.is_file():
            code = "TRACE_MISSING" if task_ok else run_result.error.code
            return self._infrastructure(trial_id, side, code, started)

        verifier = await self._run_verifier(task.verifier, workspace)
        if not verifier.ok:
            return self._infrastructure(trial_id, side, verifier.error.code, started)
        try:
            analyzed = await analyze_trace(
                EvaluationConfig(
                    trace_path,
                    out_dir=evaluation_dir,
                    judge=self.judge_provider is not None,
                ),
                judge_provider=self.judge_provider,
            )
        except Exception:
            return self._infrastructure(trial_id, side, "EVALUATION_FAILED", started)
        if not analyzed.ok:
            return self._infrastructure(trial_id, side, analyzed.error.code, started)
        try:
            trace_bytes = trace_path.read_bytes()
            bundle_path = analyzed.value.artifacts.evaluation_bundle_path
            bundle_bytes = bundle_path.read_bytes()
        except OSError:
            return self._infrastructure(trial_id, side, "EVIDENCE_READ_FAILED", started)
        trace_ref = self.artifact_store.publish_bytes(
            trace_bytes,
            kind="task_trace",
            schema_version="loom.task-trace.v1",
            suffix=".jsonl",
        )
        if not trace_ref.ok:
            return self._infrastructure(trial_id, side, trace_ref.error.code, started)
        evaluation_ref = self.artifact_store.publish_bytes(
            bundle_bytes,
            kind="evaluation_bundle",
            schema_version="loom.evaluation.bundle.v1",
            suffix=".json",
        )
        if not evaluation_ref.ok:
            return self._infrastructure(trial_id, side, evaluation_ref.error.code, started)
        total_tokens = _total_tokens(analyzed.value.metrics)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return TrialExecution.success(
            trial_id,
            side,
            {
                "task_success_rate": 1.0 if task_ok and verifier.value else 0.0,
                "total_tokens": float(total_tokens),
                "wall_time_ms": float(elapsed_ms),
            },
            evaluation_ref.value,
            trace_ref.value,
            solver_tokens=total_tokens,
            wall_time_seconds=max(0, math.ceil(elapsed_ms / 1000)),
        )

    def _materialize_workspace(self, trial_id: str, side: str, snapshot: Path) -> Path | str:
        key = (trial_id, side)
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        digest = hashlib.sha256(trial_id.encode("utf-8")).hexdigest()
        attempt_root = self.workspace_root / digest / side / f"attempt-{attempt}"
        workspace = attempt_root / "workspace"
        try:
            attempt_root.mkdir(parents=True, exist_ok=False)
            shutil.copytree(snapshot, workspace, copy_function=shutil.copy2)
        except (OSError, shutil.Error):
            return "WORKSPACE_MATERIALIZATION_FAILED"
        self._workspaces[key] = workspace
        return workspace

    async def _run_verifier(self, verifier: VerifierSpec | None, workspace: Path) -> Result:
        if verifier is None:
            from loom.core import ok

            return ok(True)
        timeout = min(verifier.timeout_ms / 1000, self.verifier_timeout_seconds)
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT"}}
        try:
            process = await asyncio.create_subprocess_exec(
                *verifier.argv,
                cwd=workspace,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                process.kill()
                await process.communicate()
                from loom.core import ok

                return ok(False)
        except OSError as exc:
            from loom.core import err

            return err(
                make_loom_error(
                    "VERIFIER_INFRASTRUCTURE_FAILED",
                    "Verifier process could not be executed",
                    retryable=True,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        from loom.core import ok

        return ok(process.returncode == verifier.expected_exit_code)

    @staticmethod
    def _infrastructure(trial_id: str, side: str, code: str, started: float) -> TrialExecution:
        elapsed = max(0, math.ceil(time.monotonic() - started))
        return TrialExecution.infrastructure_failure(trial_id, side, code, wall_time_seconds=elapsed)


def _total_tokens(metrics) -> int:
    for metric in metrics:
        if metric.name == "cost.total_tokens" and isinstance(metric.value, int | float) and not isinstance(metric.value, bool):
            return max(0, int(metric.value))
    return 0


__all__ = ["OptimizeTrialExecutor"]
