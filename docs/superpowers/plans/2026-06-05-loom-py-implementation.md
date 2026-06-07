# Loom Python Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete Python implementation of Loom in `/Users/huanggui/workspace/loom_py`, covering current TypeScript behavior plus the planned Phase 1-6 capabilities.

**Architecture:** The Python package mirrors Loom's conceptual modules while using Python-native dataclasses, async functions, tuple-based immutable collections, and pytest. `core` owns serializable contracts and context operations, `runtime` owns loop execution, `observability` owns trace persistence/querying, `composition` owns chain/nest/fork/meta loop composition, `evolution` owns mutation/versioning/shadow evaluation, and `llm` owns provider/prompt/tool logic.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, standard-library dataclasses/asyncio/json/pathlib/urllib.

---

## File Structure

Create these paths under `/Users/huanggui/workspace/loom_py`:

- `pyproject.toml`: package metadata, pytest config, optional ruff config.
- `README.md`: package overview and test commands.
- `src/loom/__init__.py`: public barrel exports.
- `src/loom/core/__init__.py`: IDs, results, errors, immutable JSON helpers, Context, Trace, loop contracts, context patch/boundary/merge.
- `src/loom/runtime/__init__.py`: registry, cancellation token, create/step/done/run, bounded async scheduler.
- `src/loom/observability/__init__.py`: in-memory trace store, trace reader, JSONL store, sampling, archive manifest.
- `src/loom/llm/__init__.py`: LLM contracts, prompt builder, tools, token tracker, LLM step, OpenAI provider.
- `src/loom/composition/__init__.py`: composite trace helpers, chain, nest, fork, meta, composition graph.
- `src/loom/evolution/__init__.py`: mutation data, versioned registry, strategy, evaluator, engine, loop/structure mutation, shadow evaluation.
- `src/loom/examples/__init__.py`: minimal counter, LLM loop, context boundary, chain/nest/fork/evolution/trace examples.
- `tests/`: pytest files grouped by module.

The package is intentionally implemented with focused module files later if any `__init__.py` grows hard to understand; the first implementation may keep each subsystem in one file to reduce import churn while TDD establishes behavior.

## Task 1: Scaffold Package

**Files:**
- Create: `/Users/huanggui/workspace/loom_py/pyproject.toml`
- Create: `/Users/huanggui/workspace/loom_py/README.md`
- Create: `/Users/huanggui/workspace/loom_py/src/loom/__init__.py`
- Create: `/Users/huanggui/workspace/loom_py/tests/test_setup.py`

- [ ] **Step 1: Write the failing test**

```python
def test_package_imports():
    import loom

    assert loom.__version__ == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/test_setup.py -q
```

Expected: fails because package metadata and import do not exist.

- [ ] **Step 3: Implement scaffold**

Create `pyproject.toml` with package name `loom`, `src` layout, Python `>=3.11`, and pytest options. Create `src/loom/__init__.py` with `__version__ = "0.1.0"`.

- [ ] **Step 4: Verify**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/test_setup.py -q
```

Expected: 1 passed.

## Task 2: Phase 0 Core Contracts

**Files:**
- Create/modify: `/Users/huanggui/workspace/loom_py/src/loom/core/__init__.py`
- Modify: `/Users/huanggui/workspace/loom_py/src/loom/__init__.py`
- Test: `/Users/huanggui/workspace/loom_py/tests/core/test_core_contracts.py`

- [ ] **Step 1: Write failing tests**

Tests cover ID prefixes, loop version `v1`, `as_step_number`, `Result`, `LoomError`, immutable Context layer helpers, Trace dataclasses, and loop contract dataclasses.

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/core/test_core_contracts.py -q
```

Expected: import/name failures for missing core API.

- [ ] **Step 3: Implement minimal core**

Implement:

- `new_loop_id`, `new_loop_version`, `new_context_id`, `new_trace_id`, `new_run_id`, `as_step_number`.
- `Result`, `ok`, `err`, `is_ok`, `is_err`.
- `LoomError`, `make_loom_error`, `to_loom_error`.
- Frozen dataclasses for Context layers, Action, Observation, Decision, Trace, MinimalLoopDefinition, LoopHandle, StepResult, RunResult.
- `empty_state`, `empty_knowledge`, `empty_affordances`, `freeze_context`, JSON normalization helpers.

- [ ] **Step 4: Verify**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/core/test_core_contracts.py -q
```

Expected: all core contract tests pass.

## Task 3: Phase 0 Observability and Runtime

**Files:**
- Create/modify: `/Users/huanggui/workspace/loom_py/src/loom/observability/__init__.py`
- Create/modify: `/Users/huanggui/workspace/loom_py/src/loom/runtime/__init__.py`
- Modify: `/Users/huanggui/workspace/loom_py/src/loom/__init__.py`
- Test: `/Users/huanggui/workspace/loom_py/tests/observability/test_in_memory_trace_store.py`
- Test: `/Users/huanggui/workspace/loom_py/tests/runtime/test_runtime.py`

- [ ] **Step 1: Write failing tests**

Tests cover append/query/get/events/children for in-memory traces, `create`, `step`, `done`, `run`, thrown-error mapping, cancellation before step, evaluator-based done, and budget-based done.

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/observability/test_in_memory_trace_store.py tests/runtime/test_runtime.py -q
```

Expected: missing observability/runtime API failures.

- [ ] **Step 3: Implement observability and runtime**

Implement `InMemoryTraceStore`, `create_in_memory_trace_sink`, `create_runtime_registry`, `CancellationToken`, `create`, `step`, `done`, `run`, `step_stream`, and `create_promise_pool`.

- [ ] **Step 4: Verify**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/observability/test_in_memory_trace_store.py tests/runtime/test_runtime.py -q
```

Expected: runtime and in-memory observability tests pass.

## Task 4: Phase 0 LLM and Examples

**Files:**
- Create/modify: `/Users/huanggui/workspace/loom_py/src/loom/llm/__init__.py`
- Create/modify: `/Users/huanggui/workspace/loom_py/src/loom/examples/__init__.py`
- Modify: `/Users/huanggui/workspace/loom_py/src/loom/__init__.py`
- Test: `/Users/huanggui/workspace/loom_py/tests/llm/test_llm.py`
- Test: `/Users/huanggui/workspace/loom_py/tests/integration/test_minimal_loop_example.py`

- [ ] **Step 1: Write failing tests**

Tests cover prompt strings, LLM tool conversion, token accumulation, LLM step structured decisions, tool-call loop, fallback parsing, token budget errors, invalid tool arguments, OpenAI request/parse/error mapping, and minimal counter loop run.

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/llm/test_llm.py tests/integration/test_minimal_loop_example.py -q
```

Expected: missing LLM/example API failures.

- [ ] **Step 3: Implement LLM and examples**

Implement LLM dataclasses, prompt builder, tool conversion, token tracker, LLM step function, injectable OpenAI provider, `make_initial_counter_context`, `make_minimal_counter_loop`, `make_initial_llm_context`, `make_llm_loop_definition`, and `run_llm_loop`.

- [ ] **Step 4: Verify**

Run:

```bash
cd /Users/huanggui/workspace/loom_py
python -m pytest tests/llm/test_llm.py tests/integration/test_minimal_loop_example.py -q
```

Expected: LLM and example tests pass.

## Task 5: Phase 1 Context Patch, Boundary, Knowledge, Merge

... (truncated for brevity)
