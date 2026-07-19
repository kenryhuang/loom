# Optimize Operations TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make `loom optimize --tui` launch a real Textual Operations Dashboard with scoped live stage/trial/runtime events, durable resume-aware controls, bounded display state, and holdout-safe output.

**Architecture:** Add a versioned optimization-event emitter between the state machine and all observers. A dedicated Optimize TUI runner injects a bounded collector and control surface into the existing runtime, while a dedicated Textual app renders a read-only projection. SQLite, campaign artifacts, and task traces remain authoritative; TUI failure is fail-open after startup.

**Tech Stack:** Python 3.11, asyncio, immutable Loom `Result`/`LoomError` contracts, SQLite optimization store, existing runtime trace sinks, Textual >= 1.0, Rich >= 13.0, pytest/pytest-asyncio.

## Global Constraints

- `--tui` must never change provider streaming, candidate validation, evaluation, holdout, or governance semantics.
- Text, JSON, and TUI receive the same `loom.optimization.event.v1` envelopes.
- Holdout task content, paths, expected outputs, verifier details, per-task scores, and judge rationales never enter observer events.
- TUI startup failure occurs before new model calls; post-start rendering failure is fail-open.
- `q` detaches only; `p` and confirmed `c` leave a durable resumable paused optimization.
- TUI state is bounded and non-authoritative.
- Existing `loom task`, evaluation/evolution TUI, and generic `LoomTuiApp` behavior remain compatible.
- Every production behavior change follows RED/GREEN TDD.

---

### Task 1: Versioned optimization event stream

**Files:**
- Create: `src/loom/optimize/events.py`
- Modify: `src/loom/optimize/ui.py`
- Create: `tests/optimize/test_events.py`
- Modify: `tests/optimize/test_cli.py`

**Interfaces:**
- Produces: `OptimizationEventEmitter(optimization_id, campaign_id, observer)`, `emit(event_type, *, stage, status, scope, payload) -> Result`, `ScopedOptimizationTraceSink`, `OptimizationSnapshot`, `OptimizeTuiObserver`.
- Preserves: `TextObserver.emit(mapping)` and `JsonObserver.emit(mapping)`.

- [x] **Step 1: Write failing event-contract and observer tests**

Add tests that create a recording observer, emit two events, and assert exact schema, types, sequence `1, 2`, timestamp presence, immutable scope, and matching text/JSON/TUI event order. Add a holdout payload containing `task`, `workspace`, `expected_output`, and `judge_rationale` and assert `HOLDOUT_EVENT_FORBIDDEN` before the observer is called.

```python
emitter = OptimizationEventEmitter("opt_test", "cmp_test", observer)
first = await emitter.emit(
    "optimization.trial.started",
    stage="search_running",
    status="running",
    scope={"phase": "discovery", "trial_id": "trial-1"},
    payload={"side": "candidate"},
)
assert first.ok
assert observer.events[0]["schema_version"] == "loom.optimization.event.v1"
assert observer.events[0]["sequence"] == 1
```

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/optimize/test_events.py tests/optimize/test_cli.py -q
```

Expected: import failure for `loom.optimize.events` and missing real TUI observer contract.

- [x] **Step 3: Implement immutable envelopes, redaction, and scoped trace forwarding**

Implement:

```python
@dataclass(slots=True)
class OptimizationEventEmitter:
    optimization_id: str
    campaign_id: str | None
    observer: Any | None
    _sequence: int = 0

    async def emit(self, event_type: str, *, stage=None, status="event", scope=None, payload=None) -> Result:
        validated = build_optimization_event(...)
        if not validated.ok:
            return validated
        self._sequence += 1
        event = {**validated.value, "sequence": self._sequence}
        if self.observer is not None:
            value = self.observer.emit(event)
            if inspect.isawaitable(value):
                await value
        return ok(event)
```

`ScopedOptimizationTraceSink.emit()` must retain the nested runtime type in `payload.runtime_event`, add the frozen trial scope, and forward it as `optimization.runtime.event`. Redaction happens before any observer call and recursively rejects forbidden holdout keys.

- [x] **Step 4: Replace the Rich line printer with a collector adapter**

`OptimizeTuiObserver.emit()` forwards validated mappings into a collector that exposes `emit(mapping)`. `create_observer(tui=True, ...)` no longer constructs `rich.console.Console`; without an injected TUI lifecycle it returns `TUI_UNAVAILABLE`.

- [x] **Step 5: Verify Task 1 GREEN and commit**

Run:

```bash
uv run pytest tests/optimize/test_events.py tests/optimize/test_cli.py -q
uv run ruff check src/loom/optimize/events.py src/loom/optimize/ui.py tests/optimize/test_events.py tests/optimize/test_cli.py
```

Commit:

```bash
git add src/loom/optimize/events.py src/loom/optimize/ui.py tests/optimize/test_events.py tests/optimize/test_cli.py
git commit -m "feat: add optimize observer event stream"
```

---

### Task 2: Stage lifecycle, snapshot, and pause checkpoints

**Files:**
- Create: `src/loom/optimize/control.py`
- Modify: `src/loom/optimize/orchestrator.py`
- Modify: `src/loom/optimize/store.py`
- Modify: `src/loom/optimize/contracts.py`
- Modify: `tests/optimize/test_orchestrator.py`
- Modify: `tests/optimize/test_store.py`

**Interfaces:**
- Consumes: `OptimizationEventEmitter` from Task 1.
- Produces: `OptimizeRunControl.request_pause()`, `request_cancel()`, `checkpoint()`, and `SQLiteOptimizationStore.snapshot()`.

- [x] **Step 1: Write failing lifecycle-order tests**

Update orchestrator tests to require:

```python
assert [(event["type"], event["status"]) for event in observer.events[:2]] == [
    ("optimization.stage.started", "running"),
    ("optimization.stage.completed", "completed"),
]
```

Add tests for `started -> failed`, and for a resumed run emitting `replayed` without another service call. Add a pause test that requests pause after preflight, expects persisted lifecycle `paused`, prevents seed calls, and returns `OPTIMIZATION_PAUSED` to the orchestration boundary.

- [x] **Step 2: Run lifecycle tests and verify RED**

```bash
uv run pytest tests/optimize/test_orchestrator.py tests/optimize/test_store.py -q
```

Expected: only completed events exist and no control/snapshot APIs exist.

- [x] **Step 3: Implement stage lifecycle emission at durable boundaries**

Inject an emitter and optional `OptimizeRunControl` into `OptimizeOrchestrator`. In `_stage()`:

1. call a control checkpoint before `store.begin()`;
2. emit `optimization.stage.replayed` after stored-output validation;
3. emit `optimization.stage.started` only after a new lease is acquired;
4. emit `optimization.stage.failed` only after lease cancellation;
5. emit `optimization.stage.completed` only after `store.complete()`.

Event payloads include operation ID, lease ID, aggregate version, and sanitized output summary rather than unrestricted stage output.

- [x] **Step 4: Implement durable pause/resume control**

`OptimizeRunControl` stores asyncio-safe pause/cancel requests. A checkpoint calls `store.pause()` once and returns `OPTIMIZATION_PAUSED`. When `run_campaign()` starts from lifecycle `paused`, an ordinary identical command explicitly calls `store.resume()` before the next operation.

Confirmed cancellation sets the same durable paused lifecycle and cancels an active lease where one exists. It must never create experiment failure evidence.

- [x] **Step 5: Implement sanitized snapshot loading**

`SQLiteOptimizationStore.snapshot()` returns stage, lifecycle, active/completed operations, aggregate version, and timestamps. `OptimizationSnapshot` combines this with configured budget limits; campaign/trial detail remains empty until Task 3 adds checkpoint projections.

- [x] **Step 6: Verify Task 2 GREEN and commit**

```bash
uv run pytest tests/optimize/test_orchestrator.py tests/optimize/test_store.py tests/optimize/test_events.py -q
uv run ruff check src/loom/optimize/control.py src/loom/optimize/orchestrator.py src/loom/optimize/store.py src/loom/optimize/contracts.py
git add src/loom/optimize/control.py src/loom/optimize/orchestrator.py src/loom/optimize/store.py src/loom/optimize/contracts.py tests/optimize/test_orchestrator.py tests/optimize/test_store.py
git commit -m "feat: expose durable optimize lifecycle progress"
```

---

### Task 3: Scoped task, trial, candidate, and budget events

**Files:**
- Modify: `src/loom/tasks/runner.py`
- Modify: `src/loom/optimize/trial_executor.py`
- Modify: `src/loom/optimize/runtime.py`
- Modify: `tests/tasks/test_task_runner.py`
- Modify: `tests/optimize/test_trial_executor.py`
- Modify: `tests/integration/test_optimize_end_to_end.py`

**Interfaces:**
- Consumes: `ScopedOptimizationTraceSink`, `OptimizationEventEmitter`.
- Produces: `run_generic_task(..., trace_sink=None)`, trial lifecycle events, candidate lifecycle events, and cumulative `optimization.budget.updated` events.

- [x] **Step 1: Write failing generic-task sink test**

Run one fake generic task with a recording sink and assert it receives normal `run.started`, `llm.requested`, `llm.completed`, and `run.completed` events while the JSONL trace remains present.

- [x] **Step 2: Write failing trial-scope test**

Construct `OptimizeTrialExecutor(..., event_emitter=emitter, event_scope={"phase": "discovery", "experiment_id": "exp_test"})`, execute one side, and assert:

```python
trial = next(event for event in events if event["type"] == "optimization.trial.started")
assert trial["scope"] == {
    "phase": "discovery",
    "experiment_id": "exp_test",
    "candidate_id": "cand_test",
    "trial_id": "trial-1",
    "side": "candidate",
    "task_id": "task-1",
    "repetition": 0,
}
assert any(event["type"] == "optimization.runtime.event" for event in events)
```

- [x] **Step 3: Run Task 3 tests and verify RED**

```bash
uv run pytest tests/tasks/test_task_runner.py tests/optimize/test_trial_executor.py tests/integration/test_optimize_end_to_end.py -q
```

- [x] **Step 4: Add optional trace-sink composition to generic tasks**

Add keyword-only `trace_sink: Any | None = None` to `run_generic_task`. Pass it to `run()` or `run_with_plugins()`; the existing `JsonlTraceStore` remains the primary runtime store, so both sinks see the same runtime events.

- [x] **Step 5: Emit scoped trial and runtime events**

`OptimizeTrialExecutor.execute()` emits trial started before task execution and trial completed/failed after the final `TrialExecution` is known. Pass a scoped sink to both `run_generic_task` and judge evaluation. Infrastructure errors include only safe codes, elapsed time, and scope.

- [x] **Step 6: Emit candidate, experiment, frontier, holdout, and budget events**

Wire one emitter through `DefaultOptimizeCampaignServices`. Emit candidate admission/rejection around controller iteration results, experiment start/completion around `PairedExperimentRunner`, frontier updates after search, and redacted holdout status. Accumulate candidate count, task-side runs, solver tokens, configured cost, and wall-clock usage and emit `optimization.budget.updated` after proposal and experiment boundaries.

- [x] **Step 7: Verify Task 3 GREEN and commit**

```bash
uv run pytest tests/tasks/test_task_runner.py tests/optimize/test_trial_executor.py tests/integration/test_optimize_end_to_end.py -q
uv run ruff check src/loom/tasks/runner.py src/loom/optimize/trial_executor.py src/loom/optimize/runtime.py
git add src/loom/tasks/runner.py src/loom/optimize/trial_executor.py src/loom/optimize/runtime.py tests/tasks/test_task_runner.py tests/optimize/test_trial_executor.py tests/integration/test_optimize_end_to_end.py
git commit -m "feat: stream scoped optimize trial progress"
```

---

### Task 4: Bounded Operations Dashboard state model

**Files:**
- Create: `src/loom/optimize/tui_state.py`
- Create: `tests/optimize/test_tui_state.py`

**Interfaces:**
- Produces: `OptimizeTuiCollector(max_recent_events=2000)`, `OptimizeDashboardState`, `CandidateRow`, `TrialRow`, `BudgetState`, and an async queue consumed by the app.

- [x] **Step 1: Write failing reducer and bounded-retention tests**

Feed snapshot, stage, candidate, trial, runtime, budget, holdout, and governance events. Assert current stage, pipeline statuses, active trial, candidate row, budget values, and holdout sealed state. Feed more than the configured limit and assert lifecycle rows remain while replaceable recent events are bounded. Feed repeated LLM deltas with one scoped call ID and assert one coalesced display event.

- [x] **Step 2: Run reducer tests and verify RED**

```bash
uv run pytest tests/optimize/test_tui_state.py -q
```

- [x] **Step 3: Implement pure dashboard reduction**

`OptimizeTuiCollector.emit(mapping)` validates sequence order, applies a pure reducer, stores a bounded sanitized event window, and pushes a lightweight update token to an asyncio queue. It never blocks the producer; when the queue is full, replaceable update tokens coalesce.

- [x] **Step 4: Verify Task 4 GREEN and commit**

```bash
uv run pytest tests/optimize/test_tui_state.py tests/optimize/test_events.py -q
uv run ruff check src/loom/optimize/tui_state.py tests/optimize/test_tui_state.py
git add src/loom/optimize/tui_state.py tests/optimize/test_tui_state.py
git commit -m "feat: add bounded optimize tui state"
```

---

### Task 5: Textual Operations Dashboard

**Files:**
- Create: `src/loom/optimize/tui_app.py`
- Create: `tests/optimize/test_tui_app.py`

**Interfaces:**
- Consumes: `OptimizeTuiCollector` and `OptimizeRunControl`.
- Produces: `OptimizeTuiApp(collector, control, *, approval_handler=None)` with `wait_started()` and the approved keyboard bindings.

- [x] **Step 1: Write failing Textual layout tests**

Use `pytest.importorskip("textual")` and `app.run_test()` to assert these stable IDs exist:

```text
#optimize-header
#stage-pipeline
#candidate-table
#active-trial
#event-feed
#budget-panel
#holdout-governance
#optimize-status
```

Feed an event through the collector, pause the pilot, and assert the selected stage/candidate/trial/budget text updates.

- [x] **Step 2: Write failing control and terminal-state tests**

Assert `q` exits the app without setting control flags, `p` sets pause request, `c` requires a confirmation screen before setting cancel, and terminal completion updates the status but does not auto-exit. Approval is disabled unless lifecycle is `awaiting_approval`.

- [x] **Step 3: Run TUI app tests and verify RED**

```bash
uv run pytest tests/optimize/test_tui_app.py -q
```

- [x] **Step 4: Implement the approved Operations Dashboard**

Use Textual `Header`/`Footer`, `Static`, `DataTable`, and `RichLog` widgets in a responsive grid matching Layout A. Poll collector updates without blocking. Render only values in `OptimizeDashboardState`; do not read stores or artifacts from widgets.

Bindings are `q`, `j`, `k`, arrows, `enter`, `space`, `y`, `Y`, `p`, `c`, and `a`. Clipboard failure updates status instead of raising.

- [x] **Step 5: Verify Task 5 GREEN and commit**

```bash
uv run pytest tests/optimize/test_tui_app.py tests/optimize/test_tui_state.py -q
uv run ruff check src/loom/optimize/tui_app.py tests/optimize/test_tui_app.py
git add src/loom/optimize/tui_app.py tests/optimize/test_tui_app.py
git commit -m "feat: add optimize operations dashboard"
```

---

### Task 6: TUI lifecycle, CLI routing, detachment, and approval

**Files:**
- Create: `src/loom/optimize/tui_runner.py`
- Modify: `src/loom/optimize/cli.py`
- Modify: `src/loom/optimize/runtime.py`
- Modify: `tests/optimize/test_cli.py`
- Create: `tests/optimize/test_tui_runner.py`

**Interfaces:**
- Produces: `run_optimize_with_tui(options, *, app_factory=None) -> Result`.
- Extends: `run_optimize(options, *, observer=None, control=None)`, `build_orchestrator(options, *, observer=None, control=None)`, and `build_default_runtime(options, *, observer=None, control=None)`.

- [x] **Step 1: Write the original-symptom regression test**

Use an injected fake app with `wait_started`, `run_async`, and `exit`. Assert the app starts before the fake optimization job, receives `optimization.snapshot.loaded`, receives at least one stage event, and no Rich console stage lines are printed.

- [x] **Step 2: Write lifecycle failure/detachment tests**

Cover:

- dependency/app startup failure returns `TUI_UNAVAILABLE` before a provider/job call;
- app exits mid-run and the job still succeeds with text fallback;
- job completion sends a done update and waits for user detach;
- `q` does not request pause/cancel;
- paused result maps to CLI exit code `3`;
- awaiting approval maps to exit code `2` and the approval callback calls the existing approval service with supplied identity/reason.

- [x] **Step 3: Run CLI/runner tests and verify RED**

```bash
uv run pytest tests/optimize/test_cli.py tests/optimize/test_tui_runner.py -q
```

- [x] **Step 4: Implement TUI-first startup and observer injection**

`main()` routes TUI runs through `run_optimize_with_tui`. The runner constructs collector, control, observer, and app; waits for `wait_started()`; then starts runtime composition. A switching observer falls back to `TextObserver` if the app exits or fails after startup.

Runtime composition uses the injected observer/control and never calls `create_observer(tui=True)`. Non-TUI and JSON paths retain current composition.

- [x] **Step 5: Implement terminal controls and result mapping**

The runner owns approval service calls. Pause/cancel use `OptimizeRunControl`; the orchestrator returns a paused result with report/next-action data instead of a generic failure. CLI maps `paused` to `3`, `awaiting_approval` to `2`, other successful terminal results to `0`, and real failures to `1`.

- [x] **Step 6: Verify Task 6 GREEN and commit**

```bash
uv run pytest tests/optimize/test_cli.py tests/optimize/test_tui_runner.py tests/optimize/test_orchestrator.py -q
uv run ruff check src/loom/optimize/tui_runner.py src/loom/optimize/cli.py src/loom/optimize/runtime.py
git add src/loom/optimize/tui_runner.py src/loom/optimize/cli.py src/loom/optimize/runtime.py tests/optimize/test_cli.py tests/optimize/test_tui_runner.py
git commit -m "feat: run optimize in a real textual tui"
```

---

### Task 7: End-to-end observability, documentation, and full verification

**Files:**
- Modify: `tests/integration/test_optimize_end_to_end.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-19-optimize-operations-tui-design.md`

**Interfaces:**
- Verifies the complete command and documents user-visible behavior.

- [x] **Step 1: Add fake-provider TUI/JSON equivalence E2E**

Run the deterministic optimization twice with separate output roots, one recording TUI envelopes and one recording JSON envelopes. Assert ordered public event types match, candidate/trial progress exists, holdout payloads contain no forbidden fields, and both runs reach the same disposition.

- [x] **Step 2: Add resume E2E**

Pause after a completed seed stage, resume with the same command, and assert snapshot/replayed events appear and proposer/solver/judge call counts do not repeat completed work.

- [x] **Step 3: Update user documentation**

Document:

```bash
uv run loom optimize \
  --trace runs/seed.jsonl \
  --tasks .loom/optimize-demo/tasks.jsonl \
  --config .loom/optimize-demo/config.yaml \
  --output-dir .loom/optimize-demo/runs \
  --tui
```

Explain `q`, `p`, `c`, `a`, exit codes, holdout redaction, and resume behavior. Change the spec status to `Implemented` only after all acceptance tests pass.

- [x] **Step 4: Run focused and full verification**

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest tests/optimize tests/tasks/test_task_runner.py tests/integration/test_optimize_end_to_end.py -q
uv run pytest -q
git diff --check
```

- [x] **Step 5: Commit the final integration**

```bash
git add tests/integration/test_optimize_end_to_end.py README.md docs/superpowers/specs/2026-07-19-optimize-operations-tui-design.md
git commit -m "docs: document optimize operations tui"
```

## Completion Evidence

Recorded on 2026-07-19:

- Focused optimization and lifecycle regression suite: passed.
- Full repository suite: `664 passed, 4 skipped`.
- `uv run ruff format --check src tests`: passed.
- `uv run ruff check src tests`: passed.
- Independent review: all Critical findings resolved; final review performed
  after durable control, holdout snapshot, cancellation, LLM-call accounting,
  retry-deduplication, per-trial-side resume checkpoint fixes, durable usage
  accounting, checkpoint execution-integrity validation, and TUI teardown
  timer safety.
- Reproduction command:

  ```bash
  uv run loom optimize \
    --trace runs/smoke-kimi-20260718-173448-193048.jsonl \
    --tasks .loom/optimize-demo/tasks.jsonl \
    --config .loom/optimize-demo/config.yaml \
    --output-dir .loom/optimize-demo/runs \
    --tui
  ```
