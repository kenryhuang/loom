from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

from loom.campaigns.contracts import ObjectiveDirection
from loom.optimize.contracts import ObjectiveConfig, OptimizationSpec
from loom.optimize.runtime import _new_run_id, _objective_specs
from loom.optimize.store import SQLiteOptimizationStore


def _spec(tmp_path: Path, optimization_id: str, optimization_key: str) -> OptimizationSpec:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    return OptimizationSpec(
        "loom.optimization.spec.v1",
        optimization_id,
        optimization_key,
        trace,
        "b" * 64,
        {"discovery": "c" * 64, "validation": "d" * 64, "holdout": "e" * 64},
        {"solver": "f" * 64},
    )


def test_new_run_rejects_disclosed_holdout_in_any_prior_run(tmp_path: Path):
    async def scenario():
        output = tmp_path / "optimize"
        base_id = "opt_test"
        key = "a" * 64
        base_spec = _spec(tmp_path, base_id, key)
        await SQLiteOptimizationStore(output / base_id).create(base_spec)
        suffix_id = f"{base_id}_earlier"
        await SQLiteOptimizationStore(output / suffix_id).create(replace(base_spec, optimization_id=suffix_id))
        with sqlite3.connect(output / suffix_id / "optimization.sqlite") as connection:
            connection.execute("UPDATE events SET stage = 'holdout_complete'")

        result = _new_run_id(output, base_id, key)

        assert result.error.code == "HOLDOUT_REUSE_FORBIDDEN"

    asyncio.run(scenario())


def test_new_run_ignores_directories_without_matching_manifest(tmp_path: Path):
    output = tmp_path / "optimize"
    (output / "opt_test").mkdir(parents=True)

    result = _new_run_id(output, "opt_test", "a" * 64)

    assert result.unwrap() == "opt_test"


def test_objective_constraints_become_fail_closed_hard_gates():
    objectives = _objective_specs(ObjectiveConfig("task_success_rate", 0.05, 0.25, 0.40), minimum_pairs=5)

    assert tuple(item.id for item in objectives) == ("task_success_rate", "total_tokens", "wall_time_ms")
    assert all(item.hard and item.min_valid_pairs == 5 and item.missing_policy == "fail_closed" for item in objectives)
    assert objectives[0].direction is ObjectiveDirection.MAXIMIZE
    assert objectives[0].max_baseline_regression == 0.05
    assert objectives[1].direction is ObjectiveDirection.MINIMIZE
    assert objectives[1].aggregation == "relative_change"
    assert objectives[1].max_baseline_regression == 0.25
    assert objectives[2].max_baseline_regression == 0.40
    assert tuple(item.id for item in _objective_specs(ObjectiveConfig("total_tokens"), minimum_pairs=5)) == (
        "task_success_rate",
        "total_tokens",
        "wall_time_ms",
    )
