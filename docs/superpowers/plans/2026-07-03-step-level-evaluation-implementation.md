# Step-Level Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a step-level, multi-dimensional evaluation foundation for Loom trace analysis.

**Architecture:** Keep deterministic evaluation in `loom.evaluation` and leave `loom.evolution` as a downstream optimizer. `analyze_trace()` will load normalized trace events, build the existing episode graph, calculate existing global metrics, then produce `StepAssessment` records with dimensions, findings, evidence hashes, and report sections.

**Tech Stack:** Python dataclasses, existing Loom `Result` style, JSONL artifacts, pytest.

---

### Task 1: Step Assessment Model And Deterministic Evaluator

**Files:**
- Create: `src/loom/evaluation/assessments.py`
- Test: `tests/evaluation/test_assessments.py`

- [ ] **Step 1: Write failing tests**

```python
from loom.evaluation.assessments import assess_steps
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.records import NormalizedEvent


def _event(event_type, *, hash, run_id="run-1", loop_id="loop-1", trace_id="trace-1", step_number=0, tool_call_id=None, tool_id=None):
    return NormalizedEvent(
        record_id=hash,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id="llm-1" if event_type.startswith("llm.") else None,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload={"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number},
        hash=hash,
    )


def test_assess_steps_flags_missing_llm_completion_and_tool_failure():
    graph = build_episode_graph((
        _event("step.started", hash="step-start"),
        _event("llm.requested", hash="llm-request"),
        _event("tool.started", tool_call_id="call-1", tool_id="shell_execute", hash="tool-start"),
        _event("tool.failed", tool_call_id="call-1", tool_id="shell_execute", hash="tool-failed"),
        _event("step.completed", hash="step-complete"),
        _event("trace.completed", hash="trace-complete"),
    ))

    assessment = assess_steps(graph)[0]

    assert assessment.status == "fail"
    assert assessment.dimensions["tool_execution"].score < 1.0
    assert assessment.dimensions["llm_response"].score < 1.0
    assert {finding.category for finding in assessment.findings} >= {"tool_failure", "llm_round_incomplete"}
    assert "tool-failed" in assessment.findings[0].evidence_event_hashes
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/evaluation/test_assessments.py -q`
Expected: fail because `loom.evaluation.assessments` does not exist.

- [ ] **Step 3: Implement model and evaluator**

Create dataclasses:
- `DimensionScore`
- `Finding`
- `StepAssessment`

Implement `assess_steps(graph: EpisodeGraph) -> tuple[StepAssessment, ...]` using existing graph steps, LLM rounds, tool calls, event hashes, and deterministic rules.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/evaluation/test_assessments.py -q`
Expected: pass.

### Task 2: Evaluation Artifacts And Report Integration

**Files:**
- Modify: `src/loom/evaluation/analyze.py`
- Modify: `src/loom/evaluation/artifacts.py`
- Modify: `src/loom/evaluation/__init__.py`
- Test: `tests/evaluation/test_analyze.py`

- [ ] **Step 1: Write failing test**

Extend `test_analyze_trace_writes_evaluation_artifacts` to assert:
- `step-assessments.jsonl` exists
- `findings.jsonl` exists
- report contains `## Step Assessments`

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/evaluation/test_analyze.py::test_analyze_trace_writes_evaluation_artifacts -q`
Expected: fail because artifacts do not expose assessment paths.

- [ ] **Step 3: Integrate assessments**

In `analyze_trace()`, call `assess_steps(graph)`.
Extend `EvaluationResult` and `EvaluationArtifacts`.
Write assessment and finding JSONL files.
Render step-level report sections.

- [ ] **Step 4: Run evaluation tests**

Run: `uv run pytest tests/evaluation -q`
Expected: pass.

### Task 3: Verification

**Files:**
- No new production files unless tests reveal a gap.

- [ ] **Step 1: Run lint**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 2: Run full tests**

Run: `uv run pytest -q`
Expected: all tests pass.
