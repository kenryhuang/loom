# Trace Evaluation Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-shaped `loom.evaluation` foundation: normalized trace events, episode graph construction, deterministic metrics, persisted evaluation artifacts, and a CLI.

**Architecture:** Add a new `loom.evaluation` package without replacing existing `loom.evolution.analyze`. The package reads existing Loom JSONL trace files, normalizes records into stable events, builds a hierarchical episode graph, calculates deterministic metrics, and writes JSONL/report artifacts. Existing evolution modules remain compatible and can later consume evaluation outputs.

**Tech Stack:** Python 3.11 dataclasses, existing Loom `Result`/`LoomError` contracts, JSONL trace records, pytest, ruff.

---

## Files

- Create `src/loom/evaluation/__init__.py`: explicit exports only.
- Create `src/loom/evaluation/records.py`: `NormalizedEvent`, `TraceIngestResult`, and trace JSONL normalization.
- Create `src/loom/evaluation/episodes.py`: `EpisodeGraph`, `RunEpisode`, `StepGraphEpisode`, `LlmRoundEpisode`, `ToolCallEpisode`, and builder.
- Create `src/loom/evaluation/metrics.py`: deterministic metric engine over `EpisodeGraph`.
- Create `src/loom/evaluation/artifacts.py`: JSONL/report writer for metrics and episode summaries.
- Create `src/loom/evaluation/analyze.py`: CLI and orchestration.
- Create `tests/evaluation/test_records.py`.
- Create `tests/evaluation/test_episodes.py`.
- Create `tests/evaluation/test_metrics.py`.
- Create `tests/evaluation/test_analyze.py`.
- Modify `tests/test_package_structure.py`: allow the new `evaluation` subpackage.
- Modify `README.md`: document the evaluation CLI in one short section.

## Task 1: Normalize Trace Records

**Files:**
- Create: `tests/evaluation/test_records.py`
- Create: `src/loom/evaluation/records.py`
- Create: `src/loom/evaluation/__init__.py`

- [x] **Step 1: Write failing tests**

Add `tests/evaluation/test_records.py`:

```python
import json

from loom.evaluation.records import NormalizedEvent, load_normalized_events


def test_load_normalized_events_reads_event_and_trace_records(tmp_path):
    path = tmp_path / "trace.jsonl"
    records = [
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-1",
            "payload": {
                "type": "step.started",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "at": "2026-07-03T00:00:00Z",
            },
            "hash": "hash-step-started",
        },
        {
            "type": "event",
            "eventType": "tool.completed",
            "traceId": "trace-1",
            "payload": {
                "type": "tool.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "tool_id": "read_file",
                "tool_call_id": "call-1",
                "input": {"path": "README.md"},
                "output": {"content": "# Demo"},
            },
            "hash": "hash-tool-completed",
        },
        {
            "type": "trace",
            "id": "trace-1",
            "runId": "run-1",
            "payload": {
                "id": "trace-1",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "step_number": 0,
                "outcome": "pass",
            },
            "hash": "hash-trace",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")

    result = load_normalized_events(path)

    assert result.ok
    events = result.value.events
    assert all(isinstance(event, NormalizedEvent) for event in events)
    assert tuple(event.event_type for event in events) == ("step.started", "tool.completed", "trace.completed")
    assert events[0].run_id == "run-1"
    assert events[0].loop_id == "loop-1"
    assert events[0].trace_id == "trace-1"
    assert events[0].step_number == 0
    assert events[1].tool_id == "read_file"
    assert events[1].tool_call_id == "call-1"
    assert events[2].hash == "hash-trace"
    assert result.value.corrupt_records == ()


def test_load_normalized_events_returns_validation_error_for_malformed_json(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("{not-json\n", encoding="utf-8")

    result = load_normalized_events(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["path"] == str(path)
```

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/evaluation/test_records.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'loom.evaluation'`.

- [x] **Step 3: Implement normalized records**

Create `src/loom/evaluation/records.py`:

```python
"""Trace record normalization for Loom evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    record_id: str
    event_type: str
    run_id: str | None
    loop_id: str | None
    trace_id: str | None
    step_number: int | None
    llm_call_id: str | None
    tool_call_id: str | None
    tool_id: str | None
    at: str | None
    payload: Mapping[str, Any]
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class TraceIngestResult:
    events: tuple[NormalizedEvent, ...]
    corrupt_records: tuple[Mapping[str, Any], ...] = ()


def load_normalized_events(path: str | Path) -> Result:
    trace_path = Path(path)
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not read trace path",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(trace_path)},
            )
        )

    events: list[NormalizedEvent] = []
    try:
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                return err(_load_error(trace_path, "Trace record must be a JSON object", line_number=line_number))
            events.append(normalize_record(raw, line_number=line_number))
    except json.JSONDecodeError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace JSONL is malformed",
                retryable=False,
                cause={"message": str(exc), "line": exc.lineno, "column": exc.colno},
                metadata={"path": str(trace_path)},
            )
        )
    return ok(TraceIngestResult(events=tuple(events)))


def normalize_record(raw: Mapping[str, Any], *, line_number: int | None = None) -> NormalizedEvent:
    payload = _payload(raw)
    nested_trace = payload.get("trace")
    if not isinstance(nested_trace, Mapping):
        nested_trace = {}
    record_type = str(raw.get("type") or "")
    event_type = _event_type(raw, payload, record_type)
    trace_id = _first_str(raw.get("traceId"), payload.get("trace_id"), payload.get("id"), nested_trace.get("id"), raw.get("id"))
    run_id = _first_str(raw.get("runId"), payload.get("run_id"), nested_trace.get("run_id"))
    loop_id = _first_str(payload.get("loop_id"), nested_trace.get("loop_id"))
    step_number = _step_number(payload, nested_trace)
    record_id = _first_str(raw.get("id"), payload.get("id"), raw.get("hash")) or f"line-{line_number or 0}"
    return NormalizedEvent(
        record_id=record_id,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=_first_str(payload.get("llm_call_id")),
        tool_call_id=_first_str(payload.get("tool_call_id")),
        tool_id=_first_str(payload.get("tool_id")),
        at=_first_str(payload.get("at")),
        payload=payload,
        hash=_first_str(raw.get("hash")),
    )


def _payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else raw


def _event_type(raw: Mapping[str, Any], payload: Mapping[str, Any], record_type: str) -> str:
    if record_type == "trace":
        return "trace.completed"
    return str(raw.get("eventType") or payload.get("type") or record_type or "unknown")


def _step_number(payload: Mapping[str, Any], nested_trace: Mapping[str, Any]) -> int | None:
    value = payload.get("step_number") if "step_number" in payload else nested_trace.get("step_number")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is not None:
            return str(value)
    return None


def _load_error(path: Path, message: str, *, line_number: int) -> Any:
    return make_loom_error("VALIDATION_FAILED", message, retryable=False, metadata={"path": str(path), "line": line_number})


__all__ = ["NormalizedEvent", "TraceIngestResult", "load_normalized_events", "normalize_record"]
```

Create `src/loom/evaluation/__init__.py`:

```python
"""Trace evaluation package for Loom."""

from loom.evaluation.records import NormalizedEvent, TraceIngestResult, load_normalized_events, normalize_record

__all__ = [
    "NormalizedEvent",
    "TraceIngestResult",
    "load_normalized_events",
    "normalize_record",
]
```

- [x] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/evaluation/test_records.py -q
```

Expected: 2 passed.

- [x] **Step 5: Commit**

```bash
git add src/loom/evaluation/__init__.py src/loom/evaluation/records.py tests/evaluation/test_records.py
git commit -m "feat: add evaluation trace normalization"
```

## Task 2: Build Episode Graph

**Files:**
- Create: `tests/evaluation/test_episodes.py`
- Create: `src/loom/evaluation/episodes.py`
- Modify: `src/loom/evaluation/__init__.py`

- [ ] **Step 1: Write failing tests**

Add `tests/evaluation/test_episodes.py`:

```python
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.records import NormalizedEvent


def _event(event_type, *, run_id="run-1", loop_id="loop-1", trace_id="trace-1", step_number=0, llm_call_id=None, tool_call_id=None, tool_id=None, hash=None):
    return NormalizedEvent(
        record_id=hash or event_type,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=llm_call_id,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload={"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number},
        hash=hash,
    )


def test_build_episode_graph_groups_run_step_llm_and_tool_episodes():
    graph = build_episode_graph(
        (
            _event("run.started", trace_id=None, step_number=None, hash="run-start"),
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-complete"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
            _event("run.completed", trace_id=None, step_number=None, hash="run-complete"),
        )
    )

    assert len(graph.runs) == 1
    assert graph.runs[0].status == "complete"
    assert len(graph.steps) == 1
    assert graph.steps[0].status == "complete"
    assert graph.steps[0].run_id == "run-1"
    assert graph.steps[0].trace_id == "trace-1"
    assert len(graph.llm_rounds) == 1
    assert graph.llm_rounds[0].status == "complete"
    assert graph.llm_rounds[0].llm_call_id == "llm-1"
    assert len(graph.tool_calls) == 1
    assert graph.tool_calls[0].status == "complete"
    assert graph.tool_calls[0].tool_id == "read_file"
    assert "tool-complete" in graph.event_hashes


def test_build_episode_graph_marks_partial_tool_call():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("tool.started", tool_call_id="call-1", tool_id="shell_execute", hash="tool-start"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assert graph.steps[0].status == "complete"
    assert graph.tool_calls[0].status == "partial"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/evaluation/test_episodes.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'loom.evaluation.episodes'`.

- [ ] **Step 3: Implement episode graph builder**

Create `src/loom/evaluation/episodes.py`:

```python
"""Episode graph construction for Loom evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from loom.evaluation.records import NormalizedEvent


@dataclass(frozen=True, slots=True)
class RunEpisode:
    id: str
    run_id: str
    loop_id: str | None
    step_ids: tuple[str, ...]
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StepGraphEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    status: str
    llm_round_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LlmRoundEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    llm_call_id: str
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    tool_call_id: str | None
    tool_id: str
    status: str
    event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EpisodeGraph:
    events: tuple[NormalizedEvent, ...]
    runs: tuple[RunEpisode, ...]
    steps: tuple[StepGraphEpisode, ...]
    llm_rounds: tuple[LlmRoundEpisode, ...]
    tool_calls: tuple[ToolCallEpisode, ...]
    orphaned_events: tuple[NormalizedEvent, ...]
    event_hashes: tuple[str, ...]


def build_episode_graph(events: Iterable[NormalizedEvent]) -> EpisodeGraph:
    event_items = tuple(events)
    run_buckets: dict[str, dict[str, object]] = {}
    step_buckets: dict[tuple[str, str, int], dict[str, object]] = {}
    llm_buckets: dict[tuple[str, str, int, str], dict[str, object]] = {}
    tool_buckets: dict[tuple[str, str, int, str], dict[str, object]] = {}
    orphaned: list[NormalizedEvent] = []

    for event in event_items:
        if event.run_id:
            run_bucket = run_buckets.setdefault(event.run_id, {"events": [], "started": False, "completed": False})
            run_bucket["events"].append(event)
            if event.event_type == "run.started":
                run_bucket["started"] = True
            if event.event_type == "run.completed":
                run_bucket["completed"] = True

        step_key = _step_key(event)
        if step_key is not None:
            step_bucket = step_buckets.setdefault(step_key, {"events": [], "started": False, "completed": False, "trace_completed": False})
            step_bucket["events"].append(event)
            if event.event_type == "step.started":
                step_bucket["started"] = True
            if event.event_type == "step.completed":
                step_bucket["completed"] = True
            if event.event_type == "trace.completed":
                step_bucket["trace_completed"] = True
        elif event.event_type not in {"run.started", "run.completed"}:
            orphaned.append(event)

        if event.llm_call_id and step_key is not None:
            key = (*step_key, event.llm_call_id)
            bucket = llm_buckets.setdefault(key, {"events": [], "requested": False, "completed": False, "failed": False})
            bucket["events"].append(event)
            if event.event_type == "llm.requested":
                bucket["requested"] = True
            if event.event_type == "llm.completed":
                bucket["completed"] = True
            if event.event_type == "llm.failed":
                bucket["failed"] = True

        if event.event_type.startswith("tool.") and step_key is not None:
            call_identity = event.tool_call_id or event.tool_id or event.record_id
            key = (*step_key, call_identity)
            bucket = tool_buckets.setdefault(key, {"events": [], "started": False, "completed": False, "failed": False, "tool_id": event.tool_id or "unknown", "tool_call_id": event.tool_call_id})
            bucket["events"].append(event)
            if event.event_type == "tool.started":
                bucket["started"] = True
            if event.event_type == "tool.completed":
                bucket["completed"] = True
            if event.event_type == "tool.failed":
                bucket["failed"] = True

    llm_rounds = tuple(_llm_episode(key, bucket) for key, bucket in sorted(llm_buckets.items()))
    tool_calls = tuple(_tool_episode(key, bucket) for key, bucket in sorted(tool_buckets.items()))
    steps = tuple(_step_episode(key, bucket, llm_rounds, tool_calls) for key, bucket in sorted(step_buckets.items()))
    runs = tuple(_run_episode(run_id, bucket, steps) for run_id, bucket in sorted(run_buckets.items()))
    return EpisodeGraph(
        events=event_items,
        runs=runs,
        steps=steps,
        llm_rounds=llm_rounds,
        tool_calls=tool_calls,
        orphaned_events=tuple(orphaned),
        event_hashes=_hashes(event_items),
    )


def _step_key(event: NormalizedEvent) -> tuple[str, str, int] | None:
    if event.run_id is None or event.loop_id is None or event.trace_id is None or event.step_number is None:
        return None
    return (event.run_id, event.trace_id, event.step_number)


def _status(*, started: bool, completed: bool, failed: bool = False) -> str:
    if failed:
        return "failed"
    if started and completed:
        return "complete"
    return "partial"


def _llm_episode(key: tuple[str, str, int, str], bucket: dict[str, object]) -> LlmRoundEpisode:
    run_id, trace_id, step_number, llm_call_id = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return LlmRoundEpisode(
        id=f"llm:{run_id}:{trace_id}:{step_number}:{llm_call_id}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=llm_call_id,
        status=_status(started=bool(bucket["requested"]), completed=bool(bucket["completed"]), failed=bool(bucket["failed"])),
        event_hashes=_hashes(events),
    )


def _tool_episode(key: tuple[str, str, int, str], bucket: dict[str, object]) -> ToolCallEpisode:
    run_id, trace_id, step_number, _call_identity = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return ToolCallEpisode(
        id=f"tool:{run_id}:{trace_id}:{step_number}:{_call_identity}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        tool_call_id=bucket["tool_call_id"],
        tool_id=str(bucket["tool_id"]),
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]), failed=bool(bucket["failed"])),
        event_hashes=_hashes(events),
    )


def _step_episode(
    key: tuple[str, str, int],
    bucket: dict[str, object],
    llm_rounds: tuple[LlmRoundEpisode, ...],
    tool_calls: tuple[ToolCallEpisode, ...],
) -> StepGraphEpisode:
    run_id, trace_id, step_number = key
    events = tuple(bucket["events"])
    loop_id = next(event.loop_id for event in events if event.loop_id is not None)
    return StepGraphEpisode(
        id=f"step:{run_id}:{trace_id}:{step_number}",
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"]) and bool(bucket["trace_completed"])),
        llm_round_ids=tuple(item.id for item in llm_rounds if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        tool_call_ids=tuple(item.id for item in tool_calls if item.run_id == run_id and item.trace_id == trace_id and item.step_number == step_number),
        event_hashes=_hashes(events),
    )


def _run_episode(run_id: str, bucket: dict[str, object], steps: tuple[StepGraphEpisode, ...]) -> RunEpisode:
    events = tuple(bucket["events"])
    step_ids = tuple(step.id for step in steps if step.run_id == run_id)
    loop_id = next((event.loop_id for event in events if event.loop_id is not None), None)
    return RunEpisode(
        id=f"run:{run_id}",
        run_id=run_id,
        loop_id=loop_id,
        step_ids=step_ids,
        status=_status(started=bool(bucket["started"]), completed=bool(bucket["completed"])),
        event_hashes=_hashes(events),
    )


def _hashes(events: tuple[NormalizedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.hash for event in events if event.hash is not None)


__all__ = ["EpisodeGraph", "LlmRoundEpisode", "RunEpisode", "StepGraphEpisode", "ToolCallEpisode", "build_episode_graph"]
```

Modify `src/loom/evaluation/__init__.py` to export episode graph types.

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/evaluation/test_episodes.py -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/loom/evaluation/__init__.py src/loom/evaluation/episodes.py tests/evaluation/test_episodes.py
git commit -m "feat: build evaluation episode graph"
```

## Task 3: Add Deterministic Metrics

**Files:**
- Create: `tests/evaluation/test_metrics.py`
- Create: `src/loom/evaluation/metrics.py`
- Modify: `src/loom/evaluation/__init__.py`

- [ ] **Step 1: Write failing tests**

Add `tests/evaluation/test_metrics.py`:

```python
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.metrics import calculate_metrics
from loom.evaluation.records import NormalizedEvent


def _event(event_type, *, run_id="run-1", loop_id="loop-1", trace_id="trace-1", step_number=0, tool_call_id=None, tool_id=None, payload=None, hash=None):
    event_payload = {"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number}
    if payload:
        event_payload.update(payload)
    return NormalizedEvent(
        record_id=hash or event_type,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=None,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload=event_payload,
        hash=hash,
    )


def test_calculate_metrics_reports_trace_and_tool_quality():
    graph = build_episode_graph(
        (
            _event("run.started", trace_id=None, step_number=None, hash="run-start"),
            _event("step.started", hash="step-start"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", payload={"output": {"content": "abc"}}, hash="tool-complete"),
            _event("tool.failed", tool_call_id="call-2", tool_id="shell_execute", hash="tool-failed"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", payload={"outcome": "pass", "metadata": {"tokenUsage": {"totalTokens": 42}}}, hash="trace-complete"),
            _event("run.completed", trace_id=None, step_number=None, hash="run-complete"),
        )
    )

    metrics = calculate_metrics(graph)
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["trace.completeness"].value == 1.0
    assert by_name["tool.call_count"].value == 2
    assert by_name["tool.failure_count"].value == 1
    assert by_name["tool.success_rate"].value == 0.5
    assert by_name["cost.total_tokens"].value == 42
    assert by_name["episode.partial_count"].value == 0
    assert by_name["episode.orphaned_count"].value == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/evaluation/test_metrics.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'loom.evaluation.metrics'`.

- [ ] **Step 3: Implement metrics**

Create `src/loom/evaluation/metrics.py`:

```python
"""Deterministic metrics for Loom evaluation episode graphs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.core import JsonValue
from loom.evaluation.episodes import EpisodeGraph


@dataclass(frozen=True, slots=True)
class MetricResult:
    id: str
    scope: str
    subject_id: str
    name: str
    value: JsonValue
    unit: str | None
    severity: str
    evidence_event_hashes: tuple[str, ...]


def calculate_metrics(graph: EpisodeGraph) -> tuple[MetricResult, ...]:
    metrics = [
        _metric("run", "all", "trace.completeness", _trace_completeness(graph), "ratio", "info", graph.event_hashes),
        _metric("episode", "all", "episode.partial_count", _partial_count(graph), "count", _severity_for_count(_partial_count(graph)), graph.event_hashes),
        _metric("episode", "all", "episode.orphaned_count", len(graph.orphaned_events), "count", _severity_for_count(len(graph.orphaned_events)), graph.event_hashes),
        _metric("llm_round", "all", "llm.request_count", len(graph.llm_rounds), "count", "info", graph.event_hashes),
        _metric("llm_round", "all", "llm.failed_count", sum(1 for item in graph.llm_rounds if item.status == "failed"), "count", _severity_for_count(sum(1 for item in graph.llm_rounds if item.status == "failed")), graph.event_hashes),
        _metric("tool_call", "all", "tool.call_count", len(graph.tool_calls), "count", "info", graph.event_hashes),
        _metric("tool_call", "all", "tool.failure_count", sum(1 for item in graph.tool_calls if item.status == "failed"), "count", _severity_for_count(sum(1 for item in graph.tool_calls if item.status == "failed")), graph.event_hashes),
        _metric("tool_call", "all", "tool.success_rate", _tool_success_rate(graph), "ratio", "info", graph.event_hashes),
        _metric("cost", "all", "cost.total_tokens", _total_tokens(graph), "tokens", "info", graph.event_hashes),
    ]
    return tuple(metrics)


def _metric(scope: str, subject_id: str, name: str, value: JsonValue, unit: str | None, severity: str, hashes: tuple[str, ...]) -> MetricResult:
    return MetricResult(
        id=f"{scope}:{subject_id}:{name}",
        scope=scope,
        subject_id=subject_id,
        name=name,
        value=value,
        unit=unit,
        severity=severity,
        evidence_event_hashes=hashes,
    )


def _trace_completeness(graph: EpisodeGraph) -> float:
    if not graph.runs and not graph.steps:
        return 0.0
    complete_runs = sum(1 for run in graph.runs if run.status == "complete")
    complete_steps = sum(1 for step in graph.steps if step.status == "complete")
    total = len(graph.runs) + len(graph.steps)
    return (complete_runs + complete_steps) / total if total else 0.0


def _partial_count(graph: EpisodeGraph) -> int:
    return (
        sum(1 for run in graph.runs if run.status == "partial")
        + sum(1 for step in graph.steps if step.status == "partial")
        + sum(1 for item in graph.llm_rounds if item.status == "partial")
        + sum(1 for item in graph.tool_calls if item.status == "partial")
    )


def _tool_success_rate(graph: EpisodeGraph) -> float:
    if not graph.tool_calls:
        return 1.0
    complete = sum(1 for item in graph.tool_calls if item.status == "complete")
    return complete / len(graph.tool_calls)


def _total_tokens(graph: EpisodeGraph) -> int:
    total = 0
    for event in graph.events:
        total += _tokens_from_value(event.payload.get("metadata", {}))
    return total


def _tokens_from_value(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    token_usage = value.get("tokenUsage") or value.get("token_usage")
    if isinstance(token_usage, Mapping):
        value = token_usage.get("totalTokens") or token_usage.get("total_tokens") or 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _severity_for_count(value: int) -> str:
    return "warning" if value else "info"


__all__ = ["MetricResult", "calculate_metrics"]
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/evaluation/test_metrics.py -q
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/loom/evaluation/__init__.py src/loom/evaluation/episodes.py src/loom/evaluation/metrics.py tests/evaluation/test_metrics.py
git commit -m "feat: add deterministic evaluation metrics"
```

## Task 4: Write Evaluation Artifacts and CLI

**Files:**
- Create: `tests/evaluation/test_analyze.py`
- Create: `src/loom/evaluation/artifacts.py`
- Create: `src/loom/evaluation/analyze.py`
- Modify: `src/loom/evaluation/__init__.py`

- [ ] **Step 1: Write failing tests**

Add `tests/evaluation/test_analyze.py`:

```python
import asyncio
import json
import subprocess
import sys

from loom.evaluation.analyze import EvaluationConfig, analyze_trace, parse_args


def _write_trace(path):
    records = [
        {
            "type": "event",
            "eventType": "run.started",
            "traceId": None,
            "payload": {"type": "run.started", "run_id": "run-1", "loop_id": "loop-1"},
            "hash": "run-start",
        },
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-1",
            "payload": {"type": "step.started", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0},
            "hash": "step-start",
        },
        {
            "type": "event",
            "eventType": "step.completed",
            "traceId": "trace-1",
            "payload": {"type": "step.completed", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0},
            "hash": "step-complete",
        },
        {
            "type": "trace",
            "id": "trace-1",
            "runId": "run-1",
            "payload": {"id": "trace-1", "run_id": "run-1", "loop_id": "loop-1", "step_number": 0, "outcome": "pass"},
            "hash": "trace-complete",
        },
        {
            "type": "event",
            "eventType": "run.completed",
            "traceId": None,
            "payload": {"type": "run.completed", "run_id": "run-1", "loop_id": "loop-1"},
            "hash": "run-complete",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def test_parse_args_accepts_trace_and_out_dir(tmp_path):
    config = parse_args(("--trace-path", str(tmp_path / "trace.jsonl"), "--out-dir", str(tmp_path / "eval")))

    assert config.trace_path == tmp_path / "trace.jsonl"
    assert config.out_dir == tmp_path / "eval"


def test_analyze_trace_writes_evaluation_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir))

        assert result.ok
        assert result.value.artifacts.metrics_path.exists()
        assert result.value.artifacts.episodes_path.exists()
        assert result.value.artifacts.report_path.exists()
        assert "Trace Evaluation Report" in result.value.report

    asyncio.run(scenario())


def test_evaluation_analyze_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "loom.evaluation.analyze", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Analyze Loom trace JSONL" in result.stdout
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/evaluation/test_analyze.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'loom.evaluation.analyze'`.

- [ ] **Step 3: Implement artifacts and CLI**

Create `src/loom/evaluation/artifacts.py`:

```python
"""Artifacts for deterministic Loom trace evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.evaluation.episodes import EpisodeGraph
from loom.evaluation.metrics import MetricResult


@dataclass(frozen=True, slots=True)
class EvaluationArtifacts:
    out_dir: Path
    episodes_path: Path
    metrics_path: Path
    report_path: Path


def write_evaluation_artifacts(out_dir: str | os.PathLike[str], graph: EpisodeGraph, metrics: Iterable[MetricResult]) -> EvaluationArtifacts:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    metric_items = tuple(metrics)
    artifacts = EvaluationArtifacts(
        out_dir=out_path,
        episodes_path=out_path / "episodes.jsonl",
        metrics_path=out_path / "metrics.jsonl",
        report_path=out_path / "report.md",
    )
    _write_jsonl(artifacts.episodes_path, (*graph.runs, *graph.steps, *graph.llm_rounds, *graph.tool_calls))
    _write_jsonl(artifacts.metrics_path, metric_items)
    artifacts.report_path.write_text(render_evaluation_report(graph, metric_items), encoding="utf-8")
    return artifacts


def render_evaluation_report(graph: EpisodeGraph, metrics: tuple[MetricResult, ...]) -> str:
    lines = [
        "# Trace Evaluation Report",
        "",
        "## Summary",
        "",
        f"- runs: {len(graph.runs)}",
        f"- steps: {len(graph.steps)}",
        f"- llm_rounds: {len(graph.llm_rounds)}",
        f"- tool_calls: {len(graph.tool_calls)}",
        f"- orphaned_events: {len(graph.orphaned_events)}",
        f"- metrics: {len(metrics)}",
        "",
        "## Metrics",
    ]
    for metric in metrics:
        lines.append(f"- {metric.name}: {metric.value} {metric.unit or ''}".rstrip())
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, items: Iterable[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(_to_plain(item), separators=(",", ":"), sort_keys=True))
            handle.write("\n")


def _to_plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return value


__all__ = ["EvaluationArtifacts", "render_evaluation_report", "write_evaluation_artifacts"]
```

Create `src/loom/evaluation/analyze.py`:

```python
"""Deterministic trace evaluation analyzer and CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from loom.core import Result, err, make_loom_error, ok
from loom.evaluation.artifacts import EvaluationArtifacts, write_evaluation_artifacts
from loom.evaluation.episodes import EpisodeGraph, build_episode_graph
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.evaluation.records import load_normalized_events


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    trace_path: Path
    out_dir: Path = Path(".loom/evaluation")

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        object.__setattr__(self, "out_dir", Path(self.out_dir))


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    graph: EpisodeGraph
    metrics: tuple[MetricResult, ...]
    artifacts: EvaluationArtifacts
    report: str


async def analyze_trace(config: EvaluationConfig) -> Result:
    if not config.trace_path.exists():
        return err(make_loom_error("VALIDATION_FAILED", "Trace path does not exist", retryable=False, metadata={"trace_path": str(config.trace_path)}))
    loaded = load_normalized_events(config.trace_path)
    if not loaded.ok:
        return loaded
    graph = build_episode_graph(loaded.value.events)
    metrics = calculate_metrics(graph)
    try:
        artifacts = write_evaluation_artifacts(config.out_dir, graph, metrics)
    except OSError as exc:
        return err(make_loom_error("VALIDATION_FAILED", "Could not write evaluation artifacts", retryable=False, cause={"name": type(exc).__name__, "message": str(exc)}, metadata={"out_dir": str(config.out_dir)}))
    report = artifacts.report_path.read_text(encoding="utf-8")
    return ok(EvaluationResult(graph=graph, metrics=metrics, artifacts=artifacts, report=report))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL with deterministic evaluation metrics.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evaluation"), type=Path)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> EvaluationConfig:
    args = _build_parser().parse_args(argv)
    return EvaluationConfig(trace_path=args.trace_path, out_dir=args.out_dir)


def main(argv: Sequence[str] | None = None) -> None:
    result = asyncio.run(analyze_trace(parse_args(argv)))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Trace evaluation failed")
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = ["EvaluationConfig", "EvaluationResult", "analyze_trace", "main", "parse_args"]
```

Modify `src/loom/evaluation/__init__.py` to export `EvaluationConfig`, `EvaluationResult`, and `analyze_trace`.

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/evaluation/test_analyze.py -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/loom/evaluation/__init__.py src/loom/evaluation/artifacts.py src/loom/evaluation/analyze.py tests/evaluation/test_analyze.py
git commit -m "feat: add deterministic evaluation analyzer"
```

## Task 5: Package Structure, Docs, and Verification

**Files:**
- Modify: `tests/test_package_structure.py`
- Modify: `README.md`

- [ ] **Step 1: Update package structure test**

Modify `tests/test_package_structure.py` so the allowed top-level submodules include `"evaluation"`:

```python
submodule_names = {"composition", "core", "evaluation", "evolution", "examples", "llm", "observability", "runtime", "tasks", "tools", "tui"}
```

- [ ] **Step 2: Update README**

Add this section after the generic task runner section:

```markdown
## Trace Evaluation

Generic task runs write JSONL traces under `runs/` by default. Analyze a trace
with deterministic evaluation metrics:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/loom-task-xxx.jsonl \
  --out-dir .loom/evaluation
```

The first evaluation phase builds normalized events, episode summaries, metrics,
and a markdown report. Later evolution phases consume these artifacts for
proposal generation and low-risk auto-apply.
```
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run pytest tests/evaluation tests/test_package_structure.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run lint**

Run:

```bash
uv run ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 5: Run full tests**

Run:

```bash
uv run pytest
```

Expected: all tests pass, with existing live LLM skips still skipped.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/test_package_structure.py
git commit -m "docs: document deterministic trace evaluation"
```

## Scope Gaps Deferred to Later Plans

- Versioned rubrics and LLM judge upgrade.
- Calibration and contradiction checks.
- Diagnosis signals and proposal registry.
- Risk gates connected to evaluation outputs.
- Shadow evaluation and replay-safe fixtures.
- Active overrides, auto-apply, monitor, and rollback.
