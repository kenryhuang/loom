"""Fresh-workspace task execution for paired Meta-Harness experiments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loom.campaigns.contracts import ArtifactRef
from loom.campaigns.experiments import TrialExecution, ValidatedCandidate
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.core import Result, err, make_loom_error, ok
from loom.evaluation import EvaluationConfig, analyze_trace
from loom.evaluation.experiments import TrialEntry
from loom.optimize.control import OptimizeControlInterrupt, OptimizeRunControl
from loom.optimize.events import OptimizationEventEmitter, ScopedOptimizationTraceSink
from loom.optimize.task_sets import PreparedTaskSets, VerifierSpec
from loom.runtime import CancellationToken
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
        event_emitter: OptimizationEventEmitter | None = None,
        event_scope: Mapping[str, Any] | None = None,
        control: OptimizeRunControl | None = None,
        checkpoint_root: str | Path | None = None,
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
        self.event_emitter = event_emitter
        self.event_scope = dict(event_scope or {})
        self.control = control
        self.checkpoint_root = None if checkpoint_root is None else Path(checkpoint_root).resolve()
        self._attempts: dict[tuple[str, str], int] = {}
        self._workspaces: dict[tuple[str, str], Path] = {}
        self._started_trial_ids: set[str] = set()

    def workspace_for(self, trial_id: str, side: str) -> Path:
        return self._workspaces[(trial_id, side)]

    async def execute(
        self,
        side: str,
        candidate: ValidatedCandidate,
        entry: TrialEntry,
        trial_id: str,
    ) -> TrialExecution:
        if trial_id not in self._started_trial_ids:
            if self.control is not None:
                self.control.interrupt()
            self._started_trial_ids.add(trial_id)
        prepared = self.prepared_tasks.task_by_fingerprint.get(entry.task_fingerprint)
        scope = {
            **self.event_scope,
            "candidate_id": candidate.candidate_id,
            "trial_id": trial_id,
            "side": side,
            "task_id": entry.task_fingerprint if prepared is None else prepared.task.task_id,
            "repetition": entry.repetition,
        }
        restored = ok(None) if prepared is None else self._restore_trial_side(candidate, entry, trial_id, side, prepared)
        if not restored.ok:
            raise TrialCheckpointError(restored.error)
        if restored.value is not None:
            return restored.value
        await self._emit("optimization.trial.started", "running", scope, {})
        result = await self._execute_once(side, candidate, entry, trial_id, scope)
        if result.failure_kind is None and prepared is not None:
            checkpointed = self._checkpoint_trial_side(candidate, entry, result, prepared)
            if not checkpointed.ok:
                raise TrialCheckpointError(checkpointed.error)
        status = "failed" if result.failure_kind is not None else "completed"
        await self._emit(
            f"optimization.trial.{status}",
            status,
            scope,
            {
                "failure_kind": result.failure_kind,
                "failure_code": result.failure_code,
                "solver_tokens": result.solver_tokens,
                "cost": result.cost,
                "wall_time_seconds": result.wall_time_seconds,
                # Holdout task-level metrics stay sealed.  The dashboard only
                # receives aggregate experiment status for that phase.
                "metrics": {} if scope.get("phase") == "holdout" else result.metrics,
            },
        )
        return result

    def _restore_trial_side(self, candidate, entry, trial_id: str, side: str, prepared) -> Result:
        if self.checkpoint_root is None:
            return ok(None)
        path = self._trial_checkpoint_path(trial_id, side)
        if not path.is_file():
            return ok(None)
        identity = self._trial_checkpoint_identity(candidate, entry, trial_id, side, prepared)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != "loom.optimization.trial-side-checkpoint.v1" or payload.get("identity_digest") != canonical_digest(identity):
                raise ValueError("trial-side checkpoint identity does not match")
            execution = payload["execution"]
            evaluation_ref = ArtifactRef(**execution["evaluation_ref"])
            trace_ref = ArtifactRef(**execution["trace_ref"])
            result = TrialExecution.success(
                execution["trial_id"],
                execution["side"],
                execution["metrics"],
                evaluation_ref,
                trace_ref,
                solver_tokens=execution["solver_tokens"],
                cost=execution["cost"],
                wall_time_seconds=execution["wall_time_seconds"],
            )
            if result.trial_id != trial_id or result.side != side:
                raise ValueError("trial-side checkpoint execution does not match")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return _trial_checkpoint_error("TRIAL_CHECKPOINT_INVALID", "Trial-side checkpoint is malformed", exc, path=path)
        for ref, schema in (
            (evaluation_ref, "loom.evaluation.bundle.v1"),
            (trace_ref, "loom.task-trace.v1"),
        ):
            verified = self.artifact_store.read_bytes(ref, expected_schema=schema)
            if not verified.ok:
                return _trial_checkpoint_error(
                    "TRIAL_CHECKPOINT_EVIDENCE_INVALID",
                    "Trial-side checkpoint evidence is unavailable",
                    path=path,
                )
        return ok(result)

    def _checkpoint_trial_side(self, candidate, entry, result: TrialExecution, prepared) -> Result:
        if self.checkpoint_root is None:
            return ok(None)
        if result.evaluation_ref is None or result.trace_ref is None:
            return _trial_checkpoint_error("TRIAL_CHECKPOINT_INVALID", "Successful trial-side evidence is missing")
        identity = self._trial_checkpoint_identity(candidate, entry, result.trial_id, result.side, prepared)
        path = self._trial_checkpoint_path(result.trial_id, result.side)
        payload = {
            "schema_version": "loom.optimization.trial-side-checkpoint.v1",
            "identity_digest": canonical_digest(identity),
            "identity": identity,
            "execution": {
                "trial_id": result.trial_id,
                "side": result.side,
                "metrics": dict(result.metrics),
                "evaluation_ref": asdict(result.evaluation_ref),
                "trace_ref": asdict(result.trace_ref),
                "solver_tokens": result.solver_tokens,
                "cost": result.cost,
                "wall_time_seconds": result.wall_time_seconds,
            },
        }
        try:
            _atomic_write_json(path, payload)
        except OSError as exc:
            return _trial_checkpoint_error("TRIAL_CHECKPOINT_WRITE_FAILED", "Could not persist completed trial side", exc, path=path)
        return ok(None)

    def _trial_checkpoint_identity(self, candidate, entry, trial_id: str, side: str, prepared) -> dict[str, Any]:
        return {
            "campaign_id": candidate.campaign_id,
            "candidate_id": candidate.candidate_id,
            "candidate_digest": candidate.artifact.sha256,
            "baseline_digest": candidate.baseline.sha256,
            "environment_digest": candidate.environment_digest,
            "solver_digest": candidate.solver_digest,
            "tools_digest": candidate.tools_digest,
            "permissions_digest": candidate.permissions_digest,
            "evaluator_digest": candidate.evaluator_digest,
            "experiment_id": self.event_scope.get("experiment_id"),
            "phase": self.event_scope.get("phase"),
            "trial_id": trial_id,
            "side": side,
            "task_fingerprint": entry.task_fingerprint,
            "seed": entry.seed,
            "repetition": entry.repetition,
            "workspace_digest": prepared.snapshot.digest,
        }

    def _trial_checkpoint_path(self, trial_id: str, side: str) -> Path:
        assert self.checkpoint_root is not None
        name = canonical_digest({"trial_id": trial_id, "side": side})
        return self.checkpoint_root / f"{name}.json"

    async def _execute_once(
        self,
        side: str,
        candidate: ValidatedCandidate,
        entry: TrialEntry,
        trial_id: str,
        event_scope: Mapping[str, Any],
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
        trace_sink = (
            None
            if self.event_emitter is None
            else ScopedOptimizationTraceSink(
                self.event_emitter,
                stage=_stage_for_phase(str(event_scope.get("phase", "discovery"))),
                scope=event_scope,
            )
        )
        cancellation = CancellationToken()

        async def watch_cancel(task: asyncio.Task) -> None:
            while not task.done():
                if self.control is not None and self.control.cancel_requested:
                    cancellation.cancel()
                    return
                await asyncio.sleep(0.01)

        try:
            run_task = asyncio.create_task(
                run_generic_task(
                    request,
                    provider=self.solver_provider,
                    options=TaskRunOptions(
                        trace_path=trace_path,
                        timeout_ms=self.trial_timeout_seconds * 1000,
                    ),
                    harness=harness,
                    trace_sink=trace_sink,
                    cancellation=cancellation,
                ),
            )
            cancel_watcher = asyncio.create_task(watch_cancel(run_task))
            try:
                run_result = await asyncio.wait_for(run_task, timeout=self.trial_timeout_seconds)
            finally:
                cancel_watcher.cancel()
                await asyncio.gather(cancel_watcher, return_exceptions=True)
        except TimeoutError:
            return self._infrastructure(trial_id, side, "TIMEOUT", started)
        except Exception:
            return self._infrastructure(trial_id, side, "TASK_RUNNER_FAILED", started)
        if self.control is not None and self.control.cancel_requested:
            raise OptimizeControlInterrupt("user cancelled active trial")
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
            analyzed = await self._await_cancelable(
                analyze_trace(
                    EvaluationConfig(
                        trace_path,
                        out_dir=evaluation_dir,
                        judge=self.judge_provider is not None,
                    ),
                    judge_provider=self.judge_provider,
                    event_sink=trace_sink,
                )
            )
        except OptimizeControlInterrupt:
            raise
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

    async def _emit(self, event_type: str, status: str, scope: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        if self.event_emitter is None:
            return
        await self.event_emitter.emit(
            event_type,
            stage=_stage_for_phase(str(scope.get("phase", "discovery"))),
            status=status,
            scope=scope,
            payload=payload,
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
            communicate = asyncio.create_task(process.communicate())
            deadline = asyncio.get_running_loop().time() + timeout
            while not communicate.done():
                if self.control is not None and self.control.cancel_requested:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.gather(communicate, return_exceptions=True)
                    raise OptimizeControlInterrupt("user cancelled active verifier")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.gather(communicate, return_exceptions=True)
                    return ok(False)
                await asyncio.wait((communicate,), timeout=min(0.01, remaining))
            await communicate
        except OSError as exc:
            return err(
                make_loom_error(
                    "VERIFIER_INFRASTRUCTURE_FAILED",
                    "Verifier process could not be executed",
                    retryable=True,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        return ok(process.returncode == verifier.expected_exit_code)

    async def _await_cancelable(self, awaitable):
        task = asyncio.create_task(awaitable)
        try:
            while not task.done():
                if self.control is not None and self.control.cancel_requested:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise OptimizeControlInterrupt("user cancelled active evaluation")
                await asyncio.wait((task,), timeout=0.01)
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _infrastructure(trial_id: str, side: str, code: str, started: float) -> TrialExecution:
        elapsed = max(0, math.ceil(time.monotonic() - started))
        return TrialExecution.infrastructure_failure(trial_id, side, code, wall_time_seconds=elapsed)


def _total_tokens(metrics) -> int:
    for metric in metrics:
        if metric.name == "cost.total_tokens" and isinstance(metric.value, int | float) and not isinstance(metric.value, bool):
            return max(0, int(metric.value))
    return 0


def _stage_for_phase(phase: str) -> str:
    return {
        "discovery": "search_running",
        "validation": "validation_complete",
        "holdout": "holdout_complete",
    }.get(phase, "search_running")


class TrialCheckpointError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__("Trial-side checkpoint operation failed" if error is None else error.message)


class DurableBaselineRunCache:
    """Exact-condition baseline cache backed by verified immutable artifacts."""

    def __init__(self, root: str | Path, artifact_store):
        self.root = Path(root).resolve()
        self.artifact_store = artifact_store
        self._items: dict[str, TrialExecution] = {}

    def get(self, key: str) -> TrialExecution | None:
        cached = self._items.get(key)
        if cached is not None:
            return cached
        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != "loom.optimization.baseline-run-checkpoint.v1" or payload.get("cache_key") != key:
                raise ValueError("baseline checkpoint identity does not match")
            execution = payload["execution"]
            evaluation_ref = ArtifactRef(**execution["evaluation_ref"])
            trace_ref = ArtifactRef(**execution["trace_ref"])
            result = TrialExecution.success(
                execution["trial_id"],
                "baseline",
                execution["metrics"],
                evaluation_ref,
                trace_ref,
                solver_tokens=execution["solver_tokens"],
                cost=execution["cost"],
                wall_time_seconds=execution["wall_time_seconds"],
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TrialCheckpointError(
                _trial_checkpoint_error("BASELINE_CHECKPOINT_INVALID", "Durable baseline checkpoint is malformed", exc, path=path).error
            ) from exc
        for ref, schema in (
            (evaluation_ref, "loom.evaluation.bundle.v1"),
            (trace_ref, "loom.task-trace.v1"),
        ):
            if not self.artifact_store.read_bytes(ref, expected_schema=schema).ok:
                raise TrialCheckpointError(
                    _trial_checkpoint_error(
                        "BASELINE_CHECKPOINT_EVIDENCE_INVALID",
                        "Durable baseline checkpoint evidence is unavailable",
                        path=path,
                    ).error
                )
        self._items[key] = result
        return result

    def put(self, key: str, value: TrialExecution) -> None:
        if value.failure_kind is not None:
            return
        if value.evaluation_ref is None or value.trace_ref is None:
            raise TrialCheckpointError(_trial_checkpoint_error("BASELINE_CHECKPOINT_INVALID", "Successful baseline evidence is missing").error)
        path = self.root / f"{key}.json"
        try:
            _atomic_write_json(
                path,
                {
                    "schema_version": "loom.optimization.baseline-run-checkpoint.v1",
                    "cache_key": key,
                    "execution": {
                        "trial_id": value.trial_id,
                        "metrics": dict(value.metrics),
                        "evaluation_ref": asdict(value.evaluation_ref),
                        "trace_ref": asdict(value.trace_ref),
                        "solver_tokens": value.solver_tokens,
                        "cost": value.cost,
                        "wall_time_seconds": value.wall_time_seconds,
                    },
                },
            )
        except OSError as exc:
            raise TrialCheckpointError(
                _trial_checkpoint_error("BASELINE_CHECKPOINT_WRITE_FAILED", "Could not persist baseline run", exc, path=path).error
            ) from exc
        self._items[key] = value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _trial_checkpoint_error(code: str, message: str, cause: BaseException | None = None, **metadata: Any) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
            metadata={key: str(value) for key, value in metadata.items()},
        )
    )


__all__ = ["DurableBaselineRunCache", "OptimizeTrialExecutor", "TrialCheckpointError"]
