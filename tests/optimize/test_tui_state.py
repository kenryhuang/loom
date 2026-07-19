from __future__ import annotations

import pytest

from loom.optimize.tui_state import OptimizeTuiCollector


def _event(sequence: int, event_type: str, *, stage="search_running", status="event", scope=None, payload=None):
    return {
        "schema_version": "loom.optimization.event.v1",
        "type": event_type,
        "sequence": sequence,
        "at": f"2026-07-19T00:00:{sequence:02d}.000Z",
        "optimization_id": "opt_test",
        "campaign_id": "cmp_test",
        "stage": stage,
        "status": status,
        "scope": scope or {},
        "payload": payload or {},
    }


@pytest.mark.asyncio
async def test_collector_reduces_pipeline_candidate_trial_budget_and_governance():
    collector = OptimizeTuiCollector(max_recent_events=20)
    events = [
        _event(
            1,
            "optimization.snapshot.loaded",
            stage="seed_analysis_complete",
            status="replayed",
            payload={"lifecycle": "running", "completed_stages": ["preflight_complete"]},
        ),
        _event(2, "optimization.stage.started", stage="search_running", status="running"),
        _event(
            3,
            "optimization.candidate.admitted",
            scope={"phase": "discovery", "candidate_id": "cand_1"},
            payload={"surface": "system_prompt"},
        ),
        _event(
            4,
            "optimization.trial.started",
            status="running",
            scope={
                "phase": "discovery",
                "candidate_id": "cand_1",
                "trial_id": "trial_1",
                "task_id": "task_1",
                "repetition": 0,
                "side": "candidate",
            },
        ),
        _event(
            5,
            "optimization.budget.updated",
            payload={
                "used": {"candidates": 1, "llm_calls": 4, "tokens": 123, "cost": "0.2"},
                "limits": {"candidates": 3, "llm_calls": 20, "tokens": None, "cost": "4.0"},
            },
        ),
        _event(
            6,
            "optimization.frontier.updated",
            payload={"candidate_ids": ["cand_1"]},
        ),
        _event(
            7,
            "optimization.holdout.status",
            stage="holdout_complete",
            status="running",
            scope={"phase": "holdout", "candidate_id": "cand_1"},
            payload={"sealed": True, "evaluated_candidates": 1},
        ),
        _event(
            8,
            "optimization.governance.awaiting_approval",
            stage="governance_complete",
            status="awaiting_approval",
            payload={"candidate_id": "cand_1"},
        ),
    ]

    for event in events:
        await collector.emit(event)

    state = collector.state
    assert state.optimization_id == "opt_test"
    assert state.stage == "governance_complete"
    assert state.lifecycle == "awaiting_approval"
    assert state.pipeline["preflight_complete"] == "completed"
    assert state.pipeline["search_running"] == "running"
    assert state.candidates["cand_1"].surface == "system_prompt"
    assert state.candidates["cand_1"].frontier
    assert state.active_trial is not None
    assert state.active_trial.task_id == "task_1"
    assert state.budget.used["tokens"] == 123
    assert state.holdout_status == "sealed"
    assert state.governance_status == "awaiting_approval"


@pytest.mark.asyncio
async def test_collector_bounds_recent_events_but_retains_derived_lifecycle_state():
    collector = OptimizeTuiCollector(max_recent_events=3)
    await collector.emit(_event(1, "optimization.stage.completed", stage="preflight_complete", status="completed"))
    await collector.emit(
        _event(
            2,
            "optimization.candidate.admitted",
            scope={"candidate_id": "cand_1", "phase": "discovery"},
        )
    )
    for sequence in range(3, 10):
        await collector.emit(_event(sequence, "optimization.proposal.started", payload={"iteration": sequence}))

    assert len(collector.state.recent_events) == 3
    assert collector.state.pipeline["preflight_complete"] == "completed"
    assert "cand_1" in collector.state.candidates
    assert collector.state.sequence == 9


@pytest.mark.asyncio
async def test_collector_coalesces_repeated_runtime_deltas_and_never_blocks_updates():
    collector = OptimizeTuiCollector(max_recent_events=10)
    for sequence, text in enumerate(("a", "ab", "abc"), start=1):
        await collector.emit(
            _event(
                sequence,
                "optimization.runtime.event",
                scope={"trial_id": "trial_1"},
                payload={
                    "runtime_event": {
                        "type": "llm.delta",
                        "llm_call_id": "llm_1",
                        "content": text,
                    }
                },
            )
        )

    assert len(collector.state.recent_events) == 1
    assert collector.state.recent_events[0]["payload"]["runtime_event"]["content"] == "abc"
    assert collector.updates.qsize() == 1


@pytest.mark.asyncio
async def test_collector_ignores_duplicate_or_out_of_order_session_events():
    collector = OptimizeTuiCollector()
    await collector.emit(_event(2, "optimization.stage.started", stage="search_running", status="running"))
    await collector.emit(_event(1, "optimization.stage.failed", stage="search_running", status="failed"))

    assert collector.state.sequence == 2
    assert collector.state.pipeline["search_running"] == "running"
