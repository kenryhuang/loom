# Trace Analysis Kernel First Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first implementation slice of the trace analysis kernel: shared normalized trace records, shared episode graph, evidence refs, and evaluation compatibility.

**Architecture:** Add `loom.trace_analysis` as the single fact layer. Move new contracts and logic there, then make `loom.evaluation.records` and `loom.evaluation.episodes` compatibility wrappers so existing callers keep working. Keep evolution untouched except for test compatibility through the existing evaluation surface.

**Tech Stack:** Python 3.11, frozen dataclasses with slots, existing `loom.core.Result`, JSONL trace files, pytest, ruff.

---

## File Map

- Create `src/loom/trace_analysis/__init__.py`: public exports for the new kernel.
- Create `src/loom/trace_analysis/schemas.py`: shared dataclasses for normalized events, evidence refs, and episode graph nodes.
- Create `src/loom/trace_analysis/records.py`: JSONL ingest and normalization.
- Create `src/loom/trace_analysis/graph.py`: episode graph builder.
- Create `src/loom/trace_analysis/evidence.py`: field-path lookup, excerpting, and evidence ref helpers.
- Modify `src/loom/evaluation/records.py`: re-export trace analysis record APIs.
- Modify `src/loom/evaluation/episodes.py`: re-export trace analysis graph APIs.
- Modify `src/loom/evaluation/assessments.py`: use new `runtime_steps` field while preserving `steps` compatibility.
- Modify `src/loom/evaluation/metrics.py`: use new `runtime_steps` field while preserving existing behavior.
- Modify `src/loom/evaluation/__init__.py`: keep old exports and add `EvidenceRef`.
- Modify `tests/test_package_structure.py`: include `trace_analysis`.
- Create `tests/trace_analysis/test_records.py`: tests for normalization.
- Create `tests/trace_analysis/test_graph.py`: tests for round/tool graph granularity.
- Create `tests/trace_analysis/test_evidence.py`: tests for evidence refs.
- Keep existing `tests/evaluation/*` passing without import changes.

## Task 1: Package Skeleton and Normalized Records

**Files:**
- Create: `src/loom/trace_analysis/__init__.py`
- Create: `src/loom/trace_analysis/schemas.py`
- Create: `src/loom/trace_analysis/records.py`
- Create: `tests/trace_analysis/test_records.py`
- Modify: `src/loom/evaluation/records.py`
- Modify: `tests/test_package_structure.py`

- [ ] **Step 1: Write failing record tests**

Create `tests/trace_analysis/test_records.py` with tests equivalent to:

```python
import json

from loom.trace_analysis.records import load_normalized_events, normalize_record
from loom.trace_analysis.schemas import NormalizedEvent


def test_normalize_record_reads_wrapper_and_payload_identity():
    event = normalize_record(
        {
            "type": "event",
            "eventType": "llm.completed",
            "traceId": "trace-1",
            "hash": "hash-1",
            "payload": {
                "type": "llm.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": "2",
                "llm_call_id": "llm-1",
                "response": {"usage": {"total_tokens": 10}},
            },
        },
        line_number=7,
    )

    assert isinstance(event, NormalizedEvent)
    assert event.record_id == "hash-1"
    assert event.event_type == "llm.completed"
    assert event.run_id == "run-1"
    assert event.loop_id == "loop-1"
    assert event.trace_id == "trace-1"
    assert event.step_number == 2
    assert event.llm_call_id == "llm-1"
    assert event.hash == "hash-1"
    assert event.line_number == 7


def test_load_normalized_events_reads_jsonl(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "event",
                "eventType": "run.started",
                "payload": {"type": "run.started", "run_id": "run-1", "loop_id": "loop-1"},
                "hash": "run-start",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = load_normalized_events(path)

    assert result.ok
    assert len(result.value.events) == 1
    assert result.value.events[0].event_type == "run.started"
```

- [ ] **Step 2: Run record tests and verify they fail**

Run: `uv run pytest tests/trace_analysis/test_records.py -q`

Expected: fail with `ModuleNotFoundError: No module named 'loom.trace_analysis'`.

- [ ] **Step 3: Implement record contracts and loader**

Implement `schemas.py` with `NormalizedEvent` and `TraceIngestResult`. Implement
`records.py` by moving the current normalization behavior from
`loom.evaluation.records`, adding `line_number` to `NormalizedEvent`.

- [ ] **Step 4: Re-export evaluation records**

Replace `src/loom/evaluation/records.py` contents with compatibility exports:

```python
"""Compatibility exports for trace record normalization."""

from loom.trace_analysis.records import load_normalized_events, normalize_record
from loom.trace_analysis.schemas import NormalizedEvent, TraceIngestResult

__all__ = ["NormalizedEvent", "TraceIngestResult", "load_normalized_events", "normalize_record"]
```

- [ ] **Step 5: Add package structure expectation**

Modify `tests/test_package_structure.py` so `submodule_names` includes
`"trace_analysis"`.

- [ ] **Step 6: Run record tests and existing evaluation record tests**

Run:

```bash
uv run pytest tests/trace_analysis/test_records.py tests/evaluation/test_records.py tests/test_package_structure.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/loom/trace_analysis src/loom/evaluation/records.py tests/trace_analysis/test_records.py tests/test_package_structure.py
git commit -m "feat: add trace analysis record normalization"
```

## Task 2: Shared Episode Graph

**Files:**
- Create: `src/loom/trace_analysis/graph.py`
- Modify: `src/loom/trace_analysis/schemas.py`
- Modify: `src/loom/evaluation/episodes.py`
- Create: `tests/trace_analysis/test_graph.py`

- [ ] **Step 1: Write failing graph tests**

Create `tests/trace_analysis/test_graph.py` with:

```python
from loom.trace_analysis.graph import build_episode_graph
from loom.trace_analysis.schemas import NormalizedEvent


def _event(event_type, *, hash, run_id="run-1", loop_id="loop-1", trace_id="trace-1", step_number=0, llm_call_id=None, tool_call_id=None, tool_id=None):
    return NormalizedEvent(
        record_id=hash,
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


def test_episode_graph_exposes_runtime_steps_and_steps_alias():
    graph = build_episode_graph(
        (
            _event("run.started", hash="run-start", trace_id=None, step_number=None),
            _event("step.started", hash="step-start"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
            _event("run.completed", hash="run-complete", trace_id=None, step_number=None),
        )
    )

    assert len(graph.runs) == 1
    assert len(graph.runtime_steps) == 1
    assert graph.steps == graph.runtime_steps
    assert graph.runtime_steps[0].status == "complete"


def test_episode_graph_groups_multiple_llm_rounds_inside_one_runtime_step():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-1-request"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-1-complete"),
            _event("tool.started", tool_call_id="tool-1", tool_id="read_file", hash="tool-1-start"),
            _event("tool.completed", tool_call_id="tool-1", tool_id="read_file", hash="tool-1-complete"),
            _event("llm.requested", llm_call_id="llm-2", hash="llm-2-request"),
            _event("llm.completed", llm_call_id="llm-2", hash="llm-2-complete"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assert len(graph.runtime_steps) == 1
    assert len(graph.llm_rounds) == 2
    assert len(graph.tool_calls) == 1
    assert graph.llm_rounds[0].requested_event is not None
    assert graph.llm_rounds[0].completed_event is not None
    assert graph.tool_calls[0].started_event is not None
    assert graph.tool_calls[0].completed_event is not None
```

- [ ] **Step 2: Run graph tests and verify they fail**

Run: `uv run pytest tests/trace_analysis/test_graph.py -q`

Expected: fail because `loom.trace_analysis.graph` does not exist.

- [ ] **Step 3: Implement graph dataclasses and builder**

Add these dataclasses to `schemas.py`: `EpisodeRef`, `RunEpisode`,
`RuntimeStepEpisode`, `LlmRoundEpisode`, `ToolCallEpisode`, `DecisionEpisode`,
`ObservationEpisode`, and `EpisodeGraph`.

Implement `graph.py` by adapting the current `loom.evaluation.episodes`
algorithm. Preserve the `steps` alias on `EpisodeGraph` as a property returning
`runtime_steps` so existing evaluation code keeps working.

- [ ] **Step 4: Re-export evaluation episodes**

Replace `src/loom/evaluation/episodes.py` with compatibility exports from
`loom.trace_analysis.graph` and `loom.trace_analysis.schemas`.

- [ ] **Step 5: Run graph and existing evaluation episode tests**

Run:

```bash
uv run pytest tests/trace_analysis/test_graph.py tests/evaluation/test_episodes.py tests/evaluation/test_metrics.py tests/evaluation/test_assessments.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/loom/trace_analysis src/loom/evaluation/episodes.py tests/trace_analysis/test_graph.py
git commit -m "feat: add shared trace episode graph"
```

## Task 3: Evidence References

**Files:**
- Create: `src/loom/trace_analysis/evidence.py`
- Modify: `src/loom/trace_analysis/schemas.py`
- Create: `tests/trace_analysis/test_evidence.py`
- Modify: `src/loom/trace_analysis/__init__.py`
- Modify: `src/loom/evaluation/__init__.py`

- [ ] **Step 1: Write failing evidence tests**

Create `tests/trace_analysis/test_evidence.py` with:

```python
from loom.trace_analysis.evidence import evidence_ref_for_event, value_at_path
from loom.trace_analysis.schemas import NormalizedEvent


def _event():
    return NormalizedEvent(
        record_id="hash-1",
        event_type="tool.completed",
        run_id="run-1",
        loop_id="loop-1",
        trace_id="trace-1",
        step_number=0,
        llm_call_id=None,
        tool_call_id="tool-1",
        tool_id="shell_execute",
        at=None,
        payload={"type": "tool.completed", "output": {"stderr": "alpha\nbeta\ngamma"}},
        hash="hash-1",
    )


def test_value_at_path_reads_nested_dicts():
    assert value_at_path(_event().payload, "output.stderr") == "alpha\nbeta\ngamma"


def test_evidence_ref_for_event_keeps_compact_excerpt():
    ref = evidence_ref_for_event(_event(), field_path="output.stderr", max_excerpt_chars=10)

    assert ref.event_hash == "hash-1"
    assert ref.event_type == "tool.completed"
    assert ref.subject_id == "tool:run-1:trace-1:0:tool-1"
    assert ref.field_path == "output.stderr"
    assert ref.excerpt == "alpha..."
```

- [ ] **Step 2: Run evidence tests and verify they fail**

Run: `uv run pytest tests/trace_analysis/test_evidence.py -q`

Expected: fail because `loom.trace_analysis.evidence` does not exist.

- [ ] **Step 3: Implement evidence helpers**

Add `EvidenceRef` to `schemas.py`. Implement `value_at_path`,
`evidence_subject_id`, and `evidence_ref_for_event` in `evidence.py`.

- [ ] **Step 4: Export evidence helpers**

Export `EvidenceRef`, `evidence_ref_for_event`, and `value_at_path` from
`loom.trace_analysis.__init__`. Export `EvidenceRef` from `loom.evaluation`.

- [ ] **Step 5: Run evidence tests**

Run: `uv run pytest tests/trace_analysis/test_evidence.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/loom/trace_analysis src/loom/evaluation/__init__.py tests/trace_analysis/test_evidence.py
git commit -m "feat: add trace evidence references"
```

## Task 4: Evaluation Compatibility Verification

**Files:**
- Modify only if tests reveal compatibility gaps:
  - `src/loom/evaluation/assessments.py`
  - `src/loom/evaluation/metrics.py`
  - `src/loom/evaluation/artifacts.py`

- [ ] **Step 1: Run all evaluation tests**

Run: `uv run pytest tests/evaluation -q`

Expected: all evaluation tests pass.

- [ ] **Step 2: Fix compatibility gaps with focused tests first**

If a compatibility failure appears, add a focused regression test to the
relevant `tests/evaluation/test_*.py` file, watch it fail, then update the
minimal production code.

- [ ] **Step 3: Run evolution analysis tests**

Run: `uv run pytest tests/evolution/test_evolution_analyze.py tests/integration/test_trace_driven_evolution.py -q`

Expected: existing evolution analyzer behavior remains compatible.

- [ ] **Step 4: Commit compatibility fixes if any**

If files changed:

```bash
git add src/loom/evaluation tests/evaluation tests/evolution tests/integration
git commit -m "fix: preserve evaluation compatibility"
```

If no files changed, skip this commit.

## Task 5: Full Verification

**Files:**
- No production files unless verification reveals a defect.

- [ ] **Step 1: Run lint**

Run: `uv run ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Inspect git status**

Run: `git status --short`

Expected: clean or only intentional plan/doc changes staged for commit.

- [ ] **Step 4: Commit remaining plan or verification docs**

If the implementation plan file has not been committed:

```bash
git add docs/superpowers/plans/2026-07-04-trace-analysis-kernel-first-slice.md
git commit -m "docs: plan trace analysis kernel first slice"
```

## Scope Gaps Left For Later Plans

- LLM judge evaluator over bounded `EvidencePack`.
- `findings.jsonl` as the primary evolution input.
- `loom.evolution.run` consuming evaluation artifacts.
- Config-driven evaluation/evolution model selection.
- Experiment and mutation registry.
