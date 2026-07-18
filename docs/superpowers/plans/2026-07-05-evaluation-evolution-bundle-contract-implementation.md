# Evaluation Evolution Bundle Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working `EvaluationBundle -> EvolutionBundle` handoff so evaluation writes a structured manifest and evolution can generate signals/proposals from evaluation results without re-scoring raw trace.

**Architecture:** Add a focused `loom.evaluation.bundle` module for manifest/loading and normalized findings. Extend evaluation artifact writing to emit `evaluation-bundle.json` and `evidence-index.jsonl`. Add an evolution loader/adapter that consumes the evaluation bundle, converts normalized findings into `EvolutionSignal`, and writes the existing evolution artifacts plus `evolution-bundle.json`.

**Tech Stack:** Python dataclasses, JSON/JSONL artifacts, existing `Result` error contracts, pytest, ruff.

---

### Task 1: Evaluation Bundle Manifest and Normalized Findings

**Files:**
- Create: `src/loom/evaluation/bundle.py`
- Modify: `src/loom/evaluation/artifacts.py`
- Modify: `src/loom/evaluation/analyze.py`
- Test: `tests/evaluation/test_bundle.py`
- Test: `tests/evaluation/test_analyze.py`

- [x] **Step 1: Write failing tests**

Add tests that run evaluation, assert `evaluation-bundle.json` and `evidence-index.jsonl` exist, load the bundle, and assert `findings.jsonl` contains normalized finding records with `surface`, `frequency_key`, `source`, `impact_score`, and evidence refs.

- [x] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/evaluation/test_bundle.py tests/evaluation/test_analyze.py -q`

Expected: fail because `loom.evaluation.bundle` and `evaluation_bundle_path` do not exist.

- [x] **Step 3: Implement bundle schemas and writer**

Create immutable dataclasses for `TraceSource`, `EvaluationSummary`, `EvaluationArtifactRefs`, `EvaluationBundle`, `StepRef`, `RoundRef`, `EvidenceRecord`, `EvaluationFinding`, and `LoadedEvaluationBundle`. Add `write_evaluation_bundle_manifest`, `load_evaluation_bundle`, and helpers to normalize deterministic and judge findings.

- [x] **Step 4: Wire artifacts**

Extend `EvaluationArtifacts` with `evaluation_bundle_path` and `evidence_index_path`. Write normalized `findings.jsonl`, `evidence-index.jsonl`, and `evaluation-bundle.json` from `write_evaluation_artifacts`.

- [x] **Step 5: Verify evaluation tests pass**

Run: `uv run pytest tests/evaluation/test_bundle.py tests/evaluation/test_analyze.py -q`

Expected: pass.

### Task 2: Evolution Consumption of Evaluation Bundle

**Files:**
- Create: `src/loom/evolution/bundle.py`
- Modify: `src/loom/evolution/analyze.py`
- Modify: `src/loom/evolution/artifacts.py`
- Modify: `src/loom/evolution/proposals.py`
- Test: `tests/evolution/test_bundle.py`
- Test: `tests/evolution/test_analyze.py`

- [x] **Step 1: Write failing tests**

Add tests that load an evaluation bundle, create `EvolutionSignal` records from normalized findings, run `evolution.analyze --evaluation-bundle`, and assert no provider is required or called.

- [x] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/evolution/test_bundle.py tests/evolution/test_analyze.py -q`

Expected: fail because `--evaluation-bundle` is unsupported and `evolution-bundle.json` is not written.

- [x] **Step 3: Implement evolution bundle loader and signal adapter**

Create `signals_from_evaluation_bundle()` that groups `EvaluationFinding` by `frequency_key`, derives severity/frequency/confidence/evidence refs, and produces existing `EvolutionSignal` objects.

- [x] **Step 4: Wire CLI and artifacts**

Add `evaluation_bundle_path` to `AnalyzeConfig`. If present, skip raw trace scoring and build signals directly from the loaded evaluation bundle. Extend `EvolutionArtifacts` with `evolution_bundle_path` and write `evolution-bundle.json`.

- [x] **Step 5: Verify evolution tests pass**

Run: `uv run pytest tests/evolution/test_bundle.py tests/evolution/test_analyze.py -q`

Expected: pass.

### Task 3: End-to-End Smoke and Cleanup

**Files:**
- Modify: package exports if needed in `src/loom/evaluation/__init__.py` and `src/loom/evolution/__init__.py`

- [x] **Step 1: Run focused tests**

Run: `uv run pytest tests/evaluation tests/evolution -q`

Expected: pass.

- [x] **Step 2: Run lint and full tests**

Run: `uv run ruff check src tests`

Expected: pass.

Run: `uv run pytest -q`

Expected: pass.

- [x] **Step 3: Run real smoke commands**

Run evaluation:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/smoke-glm-20260703-214154-774875.jsonl \
  --out-dir /tmp/loom-evaluation-bundle-check \
  --judge \
  --config config.yaml \
  --model glm
```

Run evolution:

```bash
uv run python -m loom.evolution.analyze \
  --evaluation-bundle /tmp/loom-evaluation-bundle-check/evaluation-bundle.json \
  --out-dir /tmp/loom-evolution-bundle-check \
  --min-signal-frequency 1
```

Expected: evaluation writes `evaluation-bundle.json`; evolution writes `evolution-bundle.json`, `signals.jsonl`, `proposals.jsonl`, and report.

- [x] **Step 4: Commit**

Commit message: `feat: connect evaluation bundles to evolution`
