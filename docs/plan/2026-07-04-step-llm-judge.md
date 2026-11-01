# Step LLM Judge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add optional step-level LLM judging to trace evaluation without sending raw traces to the judge.

**Architecture:** Deterministic evaluation remains the base layer. The judge layer receives one compact `StepEvidencePack` per runtime step, including deterministic assessment results, concise LLM/tool summaries, metrics, and evidence refs. `loom.evaluation.analyze` can run with or without the judge and writes separate judge artifacts while merging judge findings into the report.

**Tech Stack:** Python dataclasses, existing `Result`/`LlmMessage`/provider API, JSONL artifacts, pytest, ruff.

---

### Task 1: Normalize Token Accounting

**Files:**
- Modify: `src/loom/evaluation/metrics.py`
- Modify: `src/loom/evaluation/assessments.py`
- Test: `tests/evaluation/test_assessments.py`
- Test: `tests/evaluation/test_metrics.py`

- [x] Add failing tests showing step token totals count each `llm.completed.response.usage` once and ignore duplicate wrapper metadata on the same event.
- [x] Implement a shared local helper that extracts token usage from `response.usage` first, then `metadata.tokenUsage`, without recursively double-counting both.
- [x] Run targeted tests for metrics and assessments.

### Task 2: Add Step Evidence Packs

**Files:**
- Create: `src/loom/evaluation/judge.py`
- Test: `tests/evaluation/test_judge.py`

- [x] Add failing tests for `build_step_evidence_pack(graph, step, assessment)` that assert the pack includes step identity, deterministic dimensions/findings, compact LLM round summaries, compact tool call summaries, token totals, and evidence refs.
- [x] Implement pack dataclasses and compact serialization helpers with bounded excerpts.
- [x] Run `uv run pytest tests/evaluation/test_judge.py -q`.

### Task 3: Add Judge Parsing and Provider Call

**Files:**
- Modify: `src/loom/evaluation/judge.py`
- Test: `tests/evaluation/test_judge.py`

- [x] Add failing tests for valid judge JSON, malformed JSON, invalid scores, and provider invocation.
- [x] Implement `build_step_judge_messages`, `parse_step_judge_assessment`, and `LlmStepJudge.judge`.
- [x] Keep judge output strict: `overall`, fixed dimensions, findings, confidence, evaluator model, token usage.
- [x] Run targeted judge tests.

### Task 4: Integrate Analyze CLI and Artifacts

**Files:**
- Modify: `src/loom/evaluation/analyze.py`
- Modify: `src/loom/evaluation/artifacts.py`
- Modify: `src/loom/evaluation/__init__.py`
- Test: `tests/evaluation/test_analyze.py`
- Test: `tests/evaluation/test_artifacts.py`

- [x] Add failing parser tests for `--judge`, `--config`, and `--model`.
- [x] Add failing analyze test using a fake judge provider and asserting `judge-assessments.jsonl` plus report sections are written.
- [x] Implement optional provider construction from `TaskRunnerConfig` when `--judge` is set.
- [x] Extend artifacts with `judge-assessments.jsonl` and merged report/finding rendering.
- [x] Run evaluation tests.

### Task 5: Verify and Commit

**Files:**
- All touched files.

- [x] Run `uv run ruff check src tests`.
- [x] Run `uv run pytest -q`.
- [x] Commit the implementation on `feature/trace-analysis-kernel`.
