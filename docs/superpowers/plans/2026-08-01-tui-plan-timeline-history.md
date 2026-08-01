# TUI Plan Timeline History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every canonical plan lifecycle snapshot as an independent TUI timeline row at its real chronological position.

**Architecture:** `TuiEventCollector` already preserves every event, so the change is isolated to `LoomTuiApp._handle_plan_event()`. Plan events will use the immutable append path in `EventFeedWidget`; existing LLM stream and tool execution aggregation remain unchanged.

**Tech Stack:** Python 3.11, Textual 8.x, pytest, pytest-asyncio.

## Global Constraints

- Do not display every token delta as a separate row.
- Do not split `tool.started` and its terminal result into separate rows.
- Successful internal planning-tool rows remain suppressed; rejected and failed planning-tool rows remain visible.
- Do not add event-feed retention limits, pagination, or virtualization.
- Do not change checklist formatting, status icons, or sticky-tail semantics.
- Use strict red-green-refactor TDD.

---

### Task 1: Append Immutable Plan Lifecycle Rows

**Files:**
- Modify: `src/loom/tui/tui_app.py:1421-1436,1508-1525`
- Test: `tests/unit/test_tui_app.py`

**Interfaces:**
- Consumes: `PLAN_PRESENTATION_EVENTS`, `TuiEvent`, and `EventFeedWidget.add_event(event, pinned_expanded=True)`.
- Produces: one immutable `EventItem` per plan lifecycle event; no `plan_id -> event index` state.

- [ ] **Step 1: Write the failing lifecycle-history test**

Replace the existing coalescing expectation with a real app test that feeds complete snapshots:

```python
@pytest.mark.asyncio
async def test_tui_preserves_each_plan_revision_as_a_timeline_node():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        entered = _plan_event("plan.entered", 0)
        submitted = _plan_event("plan.submitted", 1)
        updated = _plan_event("plan.updated", 2)
        completed = _plan_event("plan.completed", 3)
        submitted.data["plan"]["items"][0]["status"] = "pending"
        updated.data["plan"]["items"][0]["status"] = "in_progress"
        completed.data["plan"]["items"][0]["status"] = "completed"

        for event in (entered, submitted, updated, completed):
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)
        plan_events = [event for event in feed.get_events() if event.event_type.startswith("plan.")]

        assert [event.event_type for event in plan_events] == [
            "plan.entered",
            "plan.submitted",
            "plan.updated",
            "plan.completed",
        ]
        assert [event.data["plan"]["revision"] for event in plan_events] == [0, 1, 2, 3]
        assert [event.data["plan"]["items"][0]["status"] for event in plan_events] == [
            "completed",
            "pending",
            "in_progress",
            "completed",
        ]
```

Use independent `_plan_event()` instances so later snapshots cannot mutate earlier fixtures.

- [ ] **Step 2: Run the lifecycle test and verify RED**

Run: `uv run pytest tests/unit/test_tui_app.py::test_tui_preserves_each_plan_revision_as_a_timeline_node -vv`

Expected: FAIL because the current app retains one node whose type and revision are `plan.completed` and `3`.

- [ ] **Step 3: Implement the minimal append-only handler**

Change `_handle_plan_event()` to validate the event type and append it directly:

```python
def _handle_plan_event(self, event: TuiEvent) -> bool:
    if event.event_type not in PLAN_PRESENTATION_EVENTS:
        return False
    self.query_one("#event_feed", EventFeedWidget).add_event(event, pinned_expanded=True)
    return True
```

Remove `self._plan_event_indices` from `LoomTuiApp.__init__`; it has no remaining consumer.

- [ ] **Step 4: Run the lifecycle test and verify GREEN**

Run: `uv run pytest tests/unit/test_tui_app.py::test_tui_preserves_each_plan_revision_as_a_timeline_node -vv`

Expected: PASS with four independent plan nodes.

- [ ] **Step 5: Add a chronological interleaving regression test**

Feed this literal sequence through the real app:

```python
events = (
    TuiEvent(0, "run.started", {"type": "run.started"}),
    _plan_event("plan.entered", 0),
    TuiEvent(2, "llm.requested", {"type": "llm.requested", "llm_call_id": "llm-1"}, llm_call_id="llm-1"),
    _plan_event("plan.submitted", 1),
    TuiEvent(4, "run.completed", {"type": "run.completed", "outcome": "pass"}),
    _plan_event("plan.updated", 2),
    _plan_event("plan.completed", 3),
)
```

Assert the feed's event types are exactly:

```python
assert [event.event_type for event in feed.get_events()] == [
    "run.started",
    "plan.entered",
    "llm.requested",
    "plan.submitted",
    "run.completed",
    "plan.updated",
    "plan.completed",
]
```

- [ ] **Step 6: Run chronology, suppression, and sticky-tail tests**

Run: `uv run pytest tests/unit/test_tui_app.py -k 'plan or planning_tool or manual_scroll' -vv`

Expected: plan chronology tests pass; successful plan-tool events remain hidden; rejected events remain visible; paused scrolling remains stationary.

- [ ] **Step 7: Run the complete TUI module and lint**

Run:

```bash
uv run ruff check src/loom/tui/tui_app.py tests/unit/test_tui_app.py
uv run pytest tests/unit/test_tui_app.py tests/unit/test_tui_collector.py -q
```

Expected: no lint diagnostics and all tests pass.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "fix: preserve plan lifecycle timeline"
```

---

### Task 2: Full Verification and Acceptance Audit

**Files:**
- Verify: `src/loom/tui/tui_app.py`
- Verify: `tests/unit/test_tui_app.py`
- Verify: `docs/superpowers/specs/2026-08-01-tui-plan-timeline-history-design.md`

**Interfaces:**
- Consumes: Task 1's append-only plan presentation.
- Produces: a clean main branch with full repository verification evidence.

- [ ] **Step 1: Run the full repository lint suite**

Run: `uv run ruff check src tests`

Expected: exit 0 with no diagnostics.

- [ ] **Step 2: Run the full repository test suite**

Run: `uv run pytest -q`

Expected: zero failures; only documented environment-dependent skips.

- [ ] **Step 3: Audit repository state and acceptance criteria**

Run:

```bash
git diff --check
git status --short
git log --oneline -6
```

Confirm plan nodes are append-only, no feed capacity limit was introduced, stream/tool aggregation code is unchanged, sticky-tail tests pass, and the worktree contains no uncommitted changes.
