from __future__ import annotations

import asyncio
from pathlib import Path

from loom.optimize.contracts import OptimizationLifecycle, OptimizationSpec, OptimizationStage
from loom.optimize.store import SQLiteOptimizationStore


def _spec(tmp_path: Path) -> OptimizationSpec:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    return OptimizationSpec(
        "loom.optimization.spec.v1",
        "opt_test",
        "a" * 64,
        trace,
        "b" * 64,
        {"discovery": "c" * 64, "validation": "d" * 64, "holdout": "e" * 64},
        {"solver": "f" * 64},
    )


def test_store_replays_completed_operation_without_advancing_version(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        created = (await store.create(spec)).unwrap()
        lease = (await store.begin(spec.optimization_id, "op-preflight", "input-a", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()
        completed = (await store.complete(lease.lease_id, {"task_manifest": "sha256:a"})).unwrap()

        replay = (await store.begin(spec.optimization_id, "op-preflight", "input-a", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()

        assert replay.replayed is True
        assert replay.aggregate_version == completed.aggregate_version
        assert replay.outputs["task_manifest"] == "sha256:a"
        assert created.aggregate_version == 1

    asyncio.run(scenario())


def test_store_rejects_operation_id_with_changed_input(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        await store.create(spec)
        await store.begin(spec.optimization_id, "op", "first", OptimizationStage.PREFLIGHT_COMPLETE)

        result = await store.begin(spec.optimization_id, "op", "changed", OptimizationStage.PREFLIGHT_COMPLETE)

        assert result.error.code == "OPERATION_CONFLICT"

    asyncio.run(scenario())


def test_store_rejects_illegal_stage_transition(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        await store.create(spec)

        result = await store.begin(spec.optimization_id, "op-search", "input", OptimizationStage.SEARCH_RUNNING)

        assert result.error.code == "OPTIMIZATION_TRANSITION_INVALID"

    asyncio.run(scenario())


def test_store_pause_resume_and_rebuild_verified_projection(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        await store.create(spec)
        lease = (await store.begin(spec.optimization_id, "op-preflight", "input", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()
        await store.complete(lease.lease_id, {"manifest": "digest"})

        paused = (await store.pause(spec.optimization_id, "user interrupt")).unwrap()
        resumed = (await store.resume(spec.optimization_id)).unwrap()
        rebuilt = (await store.load(spec.optimization_id)).unwrap()

        assert paused.lifecycle is OptimizationLifecycle.PAUSED
        assert resumed.lifecycle is OptimizationLifecycle.RUNNING
        assert rebuilt == resumed
        assert rebuilt.stage is OptimizationStage.PREFLIGHT_COMPLETE
        assert rebuilt.outputs["manifest"] == "digest"

    asyncio.run(scenario())


def test_store_reconciles_expired_lease_before_retry(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        await store.create(spec)
        first = (await store.begin(spec.optimization_id, "op", "input", OptimizationStage.PREFLIGHT_COMPLETE, lease_seconds=0)).unwrap()

        reconciled = (await store.reconcile_expired(spec.optimization_id)).unwrap()
        second = (await store.begin(spec.optimization_id, "op", "input", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()

        assert first.lease_id in reconciled
        assert second.lease_id != first.lease_id
        assert second.replayed is False

    asyncio.run(scenario())


def test_store_terminal_result_and_exports_are_replayable(tmp_path: Path):
    async def scenario():
        spec = _spec(tmp_path)
        store = SQLiteOptimizationStore(tmp_path / "opt")
        await store.create(spec)
        stages = (
            OptimizationStage.PREFLIGHT_COMPLETE,
            OptimizationStage.SEED_ANALYSIS_COMPLETE,
            OptimizationStage.CAMPAIGN_INITIALIZED,
            OptimizationStage.SEARCH_RUNNING,
            OptimizationStage.SEARCH_SEALED,
            OptimizationStage.VALIDATION_COMPLETE,
            OptimizationStage.FINALISTS_SELECTED,
            OptimizationStage.HOLDOUT_COMPLETE,
            OptimizationStage.GOVERNANCE_COMPLETE,
        )
        for index, stage in enumerate(stages):
            lease = (await store.begin(spec.optimization_id, f"op-{index}", f"input-{index}", stage)).unwrap()
            lifecycle = OptimizationLifecycle.PROMOTED if stage is OptimizationStage.GOVERNANCE_COMPLETE else None
            await store.complete(lease.lease_id, {stage.value: True}, lifecycle=lifecycle)

        state = (await store.load(spec.optimization_id)).unwrap()
        exported = (await store.export(spec.optimization_id)).unwrap()

        assert state.lifecycle is OptimizationLifecycle.PROMOTED
        assert exported["optimization"].is_file()
        assert exported["events"].is_file()

    asyncio.run(scenario())
