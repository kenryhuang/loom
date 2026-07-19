from __future__ import annotations

import io
import json

import pytest

from loom.optimize.events import OptimizationEventEmitter, ScopedOptimizationTraceSink
from loom.optimize.ui import JsonObserver, OptimizeTuiObserver, TextObserver, create_observer


class RecordingObserver:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class RecordingCollector:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_event_emitter_builds_versioned_monotonic_immutable_envelopes():
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)

    first = await emitter.emit(
        "optimization.trial.started",
        stage="search_running",
        status="running",
        scope={"phase": "discovery", "trial_id": "trial-1"},
        payload={"side": "candidate"},
    )
    second = await emitter.emit(
        "optimization.trial.completed",
        stage="search_running",
        status="completed",
        scope={"phase": "discovery", "trial_id": "trial-1"},
        payload={"side": "candidate"},
    )

    assert first.ok and second.ok
    assert [event["sequence"] for event in observer.events] == [1, 2]
    assert first.value["schema_version"] == "loom.optimization.event.v1"
    assert first.value["type"] == "optimization.trial.started"
    assert first.value["optimization_id"] == "opt_test"
    assert first.value["campaign_id"] == "cmp_test"
    assert first.value["at"].endswith("Z")
    with pytest.raises(TypeError):
        first.value["scope"]["trial_id"] = "changed"


@pytest.mark.asyncio
async def test_holdout_forbidden_payload_is_rejected_before_observer_call():
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)

    result = await emitter.emit(
        "optimization.holdout.status",
        stage="holdout_complete",
        status="running",
        scope={"phase": "holdout"},
        payload={
            "task": "hidden prompt",
            "workspace": "/hidden",
            "expected_output": "secret",
            "judge_rationale": "secret rationale",
        },
    )

    assert not result.ok
    assert result.error.code == "HOLDOUT_EVENT_FORBIDDEN"
    assert [event["type"] for event in observer.events] == ["optimization.observer.warning"]
    assert observer.events[0]["payload"] == {"code": "HOLDOUT_EVENT_FORBIDDEN"}


@pytest.mark.asyncio
async def test_holdout_trial_scope_is_rejected_before_observer_call():
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)

    result = await emitter.emit(
        "optimization.trial.started",
        stage="holdout_complete",
        status="running",
        scope={
            "phase": "holdout",
            "candidate_id": "cand_test",
            "trial_id": "hidden-trial",
            "task_id": "hidden-task",
            "side": "candidate",
            "repetition": 0,
        },
    )

    assert not result.ok
    assert result.error.code == "HOLDOUT_EVENT_FORBIDDEN"
    assert [event["type"] for event in observer.events] == ["optimization.observer.warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "stage", "scope", "payload"),
    [
        (
            "optimization.runtime.event",
            "holdout_complete",
            {"phase": "holdout", "candidate_id": "cand_test", "experiment_id": "exp_test"},
            {"runtime_event": {"type": "tool.completed", "stdout": "hidden workspace output"}},
        ),
        (
            "optimization.stage.failed",
            "holdout_complete",
            {},
            {"operation_id": "op_test", "error_code": "FAILED", "error_message": "hidden task details"},
        ),
    ],
)
async def test_holdout_rejects_runtime_values_and_stage_failure_text(event_type, stage, scope, payload):
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)

    result = await emitter.emit(event_type, stage=stage, status="failed", scope=scope, payload=payload)

    assert not result.ok
    assert result.error.code == "HOLDOUT_EVENT_FORBIDDEN"
    assert [event["type"] for event in observer.events] == ["optimization.observer.warning"]


@pytest.mark.asyncio
async def test_holdout_allows_only_aggregate_experiment_progress():
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)

    result = await emitter.emit(
        "optimization.experiment.completed",
        stage="holdout_complete",
        status="completed",
        scope={"phase": "holdout", "candidate_id": "cand_test", "experiment_id": "exp_test"},
        payload={"status": "completed", "task_side_runs": 6, "solver_tokens": 20, "cost": "0", "wall_time_seconds": 3},
    )

    assert result.ok
    assert len(observer.events) == 1


@pytest.mark.asyncio
async def test_scoped_runtime_sink_wraps_runtime_event_with_trial_scope():
    observer = RecordingObserver()
    emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)
    sink = ScopedOptimizationTraceSink(
        emitter,
        stage="search_running",
        scope={
            "phase": "discovery",
            "candidate_id": "cand_test",
            "trial_id": "trial-1",
            "side": "candidate",
        },
    )

    result = await sink.emit({"type": "llm.requested", "model": "solver", "llm_call_id": "llm-1"})

    assert result.ok
    event = observer.events[0]
    assert event["type"] == "optimization.runtime.event"
    assert event["scope"]["trial_id"] == "trial-1"
    assert event["payload"]["runtime_event"]["type"] == "llm.requested"


@pytest.mark.asyncio
async def test_text_json_and_tui_observers_render_the_same_event():
    event_observer = RecordingObserver()
    event = (
        await OptimizationEventEmitter("opt_test", "cmp_test", event_observer).emit(
            "optimization.stage.completed",
            stage="preflight_complete",
            status="completed",
            payload={"aggregate_version": 2},
        )
    ).unwrap()
    text_stream = io.StringIO()
    json_stream = io.StringIO()
    collector = RecordingCollector()

    TextObserver(text_stream).emit(event)
    JsonObserver(json_stream).emit(event)
    await OptimizeTuiObserver(collector).emit(event)

    assert "optimization.stage.completed" in text_stream.getvalue()
    assert json.loads(json_stream.getvalue())["sequence"] == event["sequence"]
    assert collector.events == [event]


def test_create_observer_requires_tui_lifecycle_instead_of_printing_rich_lines():
    result = create_observer(tui=True, json_output=False)

    assert not result.ok
    assert result.error.code == "TUI_UNAVAILABLE"
