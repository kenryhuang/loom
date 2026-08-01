# TUI Semantic Event Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace raw, repetitive TUI lifecycle rows with deterministic semantic progress summaries while preserving the complete JSONL trace and the complete visible semantic timeline.

**Architecture:** Add a focused `event_summary` presentation module that classifies raw/aggregated `TuiEvent` values and derives concise titles and descriptions from structured payloads. Keep lifecycle correlation and feed mutation in `LoomTuiApp`, reusing the existing LLM and tool aggregation states; extend the existing detail formatter for decisions and actionable observations.

**Tech Stack:** Python 3.11+, Textual, Rich, pytest, Ruff

## Global Constraints

- Persisted JSONL events and runtime schemas must not change.
- Live events and replayed events must use the same presentation path.
- No extra LLM calls or external dependencies may be introduced.
- Completed tool rows must summarize structured output before input arguments.
- Failed and actionable guard events must remain visible.
- Plan checklist history, full visible timeline retention, and sticky-tail scrolling must remain unchanged.
- Full inputs, outputs, reasoning, and errors remain available in expandable details.

## File Structure

- Create `src/loom/tui/event_summary.py`: pure, defensive event classification and semantic summary extraction.
- Modify `src/loom/tui/tui_app.py`: consume semantic summaries, filter redundant lifecycle events, preserve metric updates, and render decision/observation details.
- Create `tests/unit/test_tui_event_summary.py`: focused unit coverage for summary extraction from representative payloads.
- Modify `tests/unit/test_tui_app.py`: application-level lifecycle aggregation, filtering, detail, replay-equivalence, timeline-retention, and sticky-tail regression coverage.

---

### Task 1: Pure Semantic Summary Extraction

**Files:**
- Create: `src/loom/tui/event_summary.py`
- Create: `tests/unit/test_tui_event_summary.py`

**Interfaces:**
- Consumes: `loom.tui.tui_collector.TuiEvent`.
- Produces: `EventSummary(title: str, description: str, color_role: str)`.
- Produces: `summarize_event(event: TuiEvent) -> EventSummary | None`.
- Produces: `is_redundant_observation(event: TuiEvent) -> bool`.
- Produces: `llm_response_text(event: TuiEvent) -> str | None`.

- [ ] **Step 1: Write failing decision and observation tests**

Add tests that assert:

```python
def test_summarize_decision_uses_action_description():
    event = TuiEvent(0, "decision.recorded", {
        "decision": {
            "action": {"description": "List project structure", "target": "run_command"},
            "reasoning": "The first plan item requires discovery.",
            "confidence": 0.95,
        }
    })
    assert summarize_event(event) == EventSummary("Decision", "List project structure", "text")


def test_guard_observation_uses_code_and_message():
    event = TuiEvent(0, "observation.recorded", {
        "observation": {
            "source": "planning.guard",
            "value": {"accepted": False, "code": "PLAN_UPDATE_REQUIRED", "message": "Update the checklist"},
        }
    })
    assert summarize_event(event).description == "PLAN_UPDATE_REQUIRED · Update the checklist"
    assert is_redundant_observation(event) is False


@pytest.mark.parametrize("source", ["llm", "read_file", "edit_file", "write_file", "shell_execute", "finish", "submit_plan", "update_plan"])
def test_success_observations_already_represented_elsewhere_are_redundant(source):
    event = TuiEvent(0, "observation.recorded", {"observation": {"source": source, "value": {"accepted": True}}})
    assert is_redundant_observation(event) is True
```

- [ ] **Step 2: Run the new tests and verify collection fails**

Run: `pytest -q tests/unit/test_tui_event_summary.py`

Expected: FAIL because `loom.tui.event_summary` does not exist.

- [ ] **Step 3: Implement the summary model and defensive field extraction**

Create:

```python
@dataclass(frozen=True)
class EventSummary:
    title: str
    description: str = ""
    color_role: str = "text"


def summarize_event(event: TuiEvent) -> EventSummary | None:
    if event.event_type == "decision.recorded":
        return _decision_summary(event.data)
    if event.event_type == "observation.recorded":
        return _observation_summary(event.data)
    if event.event_type.startswith("action."):
        return _action_summary(event.event_type, event.data)
    if event.event_type.startswith("tool."):
        return _tool_summary(event.event_type, event.data, event.error)
    if event.event_type == "llm.completed":
        text = llm_response_text(event)
        return EventSummary("Answer", first_meaningful_line(text), "text") if text else None
    return None
```

Implement mapping-safe helpers that accept mappings, dataclass-like observation values, malformed scalars, and JSON strings without raising.

- [ ] **Step 4: Write failing typed tool-summary tests**

Cover exact collapsed summaries for:

```python
shell_output = {
    "value": {
        "source": "shell_execute",
        "value": {
            "exit_code": 0,
            "stdout": "43 passed, 8 skipped in 3.41s\n",
            "stderr": "",
            "duration_ms": 3410,
            "timed_out": False,
        },
    }
}
assert summarize_event(tool_event("shell_execute", output=shell_output)).description == (
    "Passed · exit 0 · 43 passed, 8 skipped · 3.41s"
)
```

Also assert summaries for `read_file` (`path`, `bytes_read`, `truncated`), `edit_file` (`path`, `replacements`, `first_changed_line`, `bytes_written`), `write_file` (`path`, `bytes_written`), `finish` (`completed`), non-zero shell exit, timeout, runtime `tool.failed`, rejected plan tool, and a generic `{"summary": "found docs"}` result.

- [ ] **Step 5: Run typed tool tests and verify they fail**

Run: `pytest -q tests/unit/test_tui_event_summary.py -k 'tool or shell or read or edit or write or finish'`

Expected: FAIL because typed output summaries are incomplete.

- [ ] **Step 6: Implement lifecycle-aware tool summaries**

Unwrap output shapes in this order:

```python
output -> Observation.value or mapping["value"] -> nested observation mapping["value"]
```

Use input only for an in-progress target. For completion, use typed output fields. Add helpers for human-readable byte and duration values, normalized first/last meaningful lines, pytest count detection, error code/message extraction, and safe 96-character truncation.

- [ ] **Step 7: Write and implement LLM response classification tests**

Assert that `llm_response_text()` returns `None` for empty responses, native `tool_calls`, exact JSON action envelopes, and prose followed by an action JSON envelope. Assert that it returns normalized text for an ordinary final response and extracts `action.input.content` or `action.input.report` only when the action kind is a final/custom response rather than a tool call.

- [ ] **Step 8: Run the pure summary suite**

Run: `pytest -q tests/unit/test_tui_event_summary.py`

Expected: PASS.

- [ ] **Step 9: Commit the pure presentation helper**

```bash
git add src/loom/tui/event_summary.py tests/unit/test_tui_event_summary.py
git commit -m "feat: derive semantic tui event summaries"
```

---

### Task 2: Integrate Curated Timeline Filtering

**Files:**
- Modify: `src/loom/tui/tui_app.py`
- Modify: `tests/unit/test_tui_app.py`

**Interfaces:**
- Consumes: `summarize_event`, `is_redundant_observation`, and `llm_response_text` from Task 1.
- Produces: `LoomTuiApp._should_present_event(event: TuiEvent) -> bool`.
- Produces: one visible row per tool execution and one visible row per meaningful LLM stream/answer.

- [ ] **Step 1: Write failing application filtering tests**

Create an app test that sends a realistic sequence:

```python
run.started -> llm.requested -> llm.stream.started -> reasoning delta ->
llm.completed(tool-only) -> decision.recorded -> action.started ->
tool.started -> tool.completed -> observation.recorded(tool) ->
action.completed -> action.recorded -> step.completed
```

Assert the visible event types are exactly:

```python
["run.started", "llm.stream.completed", "decision.recorded", "tool.completed", "step.completed"]
```

Use the actual lifecycle event names emitted by `_LlmStreamState`; the assertion may use `llm.stream.started` if no completion event was provided. Also assert a guard observation remains visible and has no repeated event-type text.

- [ ] **Step 2: Run the filtering tests and verify failure**

Run: `pytest -q tests/unit/test_tui_app.py -k 'semantic or redundant or curated'`

Expected: FAIL because request/action/observation rows are still added.

- [ ] **Step 3: Route collapsed row formatting through semantic summaries**

In `_event_conversation_parts`, preserve the existing dedicated plan/run/step/evolution branches, then consult `summarize_event(event)`. Map `color_role` through the existing `COLORS` dictionary. Remove the generic `event_type` description fallback; unknown events receive a title-only humanized label or are filtered before insertion.

- [ ] **Step 4: Add context-aware event inclusion to `LoomTuiApp`**

Track decision trace IDs:

```python
self._decision_trace_ids: set[str] = set()
```

Rules:

- Add meaningful decisions and remember their `trace_id`.
- Always suppress `action.recorded`.
- Suppress `action.started` and successful `action.completed` when their trace already has a visible decision; retain failed action completions.
- Suppress `is_redundant_observation(event)` observations; retain actionable guard/error observations.
- Suppress unknown events whose semantic formatter returns no useful information.
- Run metric/status updates independently of visibility so hidden transport events still update tokens and status.

- [ ] **Step 5: Collapse LLM presentation to thinking plus answer**

Change `_handle_llm_event` so:

- `llm.requested` allocates round metadata but does not add a row.
- Stream deltas continue to update one `Thinking` row.
- `llm.completed` always updates usage, adds an `Answer` row only when `llm_response_text(event)` is non-empty, and does not add tool-call-only responses.
- `llm.failed` always adds a visible pinned row.

Ensure a completed stream retains its original timeline position and detail data.

- [ ] **Step 6: Run the curated timeline tests**

Run: `pytest -q tests/unit/test_tui_app.py -k 'semantic or redundant or curated or llm_round or aggregates_tool'`

Expected: PASS.

- [ ] **Step 7: Update existing expectations affected by intentional filtering**

Adjust tests that currently require visible `llm.requested` and raw `llm.completed` rows. Preserve assertions for token accounting, tool aggregation, expansion, plan chronology, selection, and scroll behavior. Do not weaken unrelated assertions.

- [ ] **Step 8: Run the full TUI unit suite**

Run: `pytest -q tests/unit/test_tui_app.py tests/unit/test_tui_collector.py tests/unit/test_tui_launcher.py`

Expected: PASS.

- [ ] **Step 9: Commit curated timeline integration**

```bash
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "feat: curate tui progress timeline"
```

---

### Task 3: Expand Semantic Detail Rendering

**Files:**
- Modify: `src/loom/tui/tui_app.py`
- Modify: `tests/unit/test_tui_app.py`

**Interfaces:**
- Consumes: existing `_format_event_detail`, `_append_jsonish`, `_append_wrapped`, and safe-markup helpers.
- Produces: dedicated detail sections for decisions, fallback actions, and actionable observations.

- [ ] **Step 1: Write failing detail tests**

Assert decision detail includes:

```text
action: List project structure
target: run_command
reasoning: The first plan item requires discovery.
confidence: 0.95
alternatives:
```

Assert a planning guard detail includes source, code, message, and the compact relevant value without dumping trace metadata. Assert tool details still retain complete input/output, including diff/stdout/stderr fields.

- [ ] **Step 2: Run detail tests and verify failure**

Run: `pytest -q tests/unit/test_tui_app.py -k 'decision_detail or observation_detail'`

Expected: FAIL because generic JSON detail is used.

- [ ] **Step 3: Add dedicated decision/action/observation branches**

Add focused append helpers in `tui_app.py` with these exact signatures: `_append_decision_detail(lines: list[str], data: dict[str, Any]) -> None`, `_append_action_detail(lines: list[str], event_type: str, data: dict[str, Any]) -> None`, and `_append_observation_detail(lines: list[str], data: dict[str, Any]) -> None`.

Render semantic fields first, preserve complete structured input/value afterward, and continue excluding envelope metadata (`type`, IDs, timestamps, step number).

- [ ] **Step 4: Run detail and transcript tests**

Run: `pytest -q tests/unit/test_tui_app.py -k 'detail or transcript or copy'`

Expected: PASS.

- [ ] **Step 5: Commit semantic detail rendering**

```bash
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "feat: show semantic tui event details"
```

---

### Task 4: Replay and Regression Verification

**Files:**
- Modify: `tests/unit/test_tui_app.py`
- Modify: `src/loom/tui/README.md`

**Interfaces:**
- Consumes: the existing `TuiEventCollector.replay_jsonl`/event conversion path and semantic presentation logic.
- Produces: documented curated-timeline behavior and replay regression coverage.

- [ ] **Step 1: Write a representative replay regression test**

Build a compact JSONL fixture in pytest's `tmp_path` using payload shapes from `runs/yakdb-reliability-plan.jsonl`: submit/update plan, structured decision, duplicated action lifecycle, shell tool completion with pytest output, tool observation, planning guard rejection, and final answer. Replay it through the same collector/app path used by the CLI.

Assert:

- No collapsed line contains the same dotted event name twice.
- No successful tool row displays serialized input after completion.
- The shell row contains exit/test/duration progress.
- The plan lifecycle and guard rows remain.
- The final answer remains.

- [ ] **Step 2: Run replay test and verify expected failure or gap**

Run: `pytest -q tests/unit/test_tui_app.py -k 'replay_semantic_timeline'`

Expected before final wiring: FAIL if replay bypasses any live presentation path; otherwise PASS confirms shared behavior.

- [ ] **Step 3: Fix replay wiring only if the test exposes a separate path**

Route replayed `TuiEvent` instances through `LoomTuiApp._handle_event` exactly like queue-delivered events. Do not add replay-only summary logic.

- [ ] **Step 4: Document the curated event stream**

Update `src/loom/tui/README.md` to state that the event stream is a semantic view, raw events remain in JSONL, tool rows update in place with outcomes, duplicate lifecycle observations are hidden, and expandable details retain raw inputs/results.

- [ ] **Step 5: Run focused and full verification**

Run:

```bash
pytest -q tests/unit/test_tui_event_summary.py tests/unit/test_tui_app.py tests/unit/test_tui_collector.py tests/unit/test_tui_launcher.py
ruff check src/loom/tui tests/unit/test_tui_event_summary.py tests/unit/test_tui_app.py
pytest -q
git diff --check
```

Expected: all tests pass, Ruff reports no issues, and `git diff --check` emits no output.

- [ ] **Step 6: Inspect representative rendered transcript**

Use a small read-only script that feeds representative yakDB events through the presentation helpers and prints `_format_event_line_plain` output. Confirm manually that it contains meaningful progress and omits raw duplicated lifecycle labels and completed-tool arguments.

- [ ] **Step 7: Commit replay coverage and documentation**

```bash
git add tests/unit/test_tui_app.py src/loom/tui/README.md
git commit -m "test: verify semantic tui timeline replay"
```

---

## Completion Criteria

- The screenshot pattern `decision.recorded decision.recorded` and equivalent `action.*` repetition cannot be produced by the visible timeline.
- A completed `Bash`, `Read`, `Edit`, or `Write` row shows outcome data rather than repeating raw invocation arguments.
- Successful duplicate observations/actions are hidden, while guard errors and failures remain visible.
- Intermediate tool-call LLM responses do not become answer rows; a genuine final response does.
- Plan history, full semantic history, manual-scroll pause, sticky-tail resume, copy, and expanded details continue to work.
- Focused tests, full suite, Ruff, and diff checks pass.
