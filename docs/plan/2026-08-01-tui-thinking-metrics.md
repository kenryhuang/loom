# TUI Thinking Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore TUI LLM-stream rows to the compact `Thought for <elapsed> <count> tokens >` format.

**Architecture:** Keep `_LlmStreamState` as the sole stream aggregator and change only the presentation branch in `_event_conversation_parts`. The formatter will read aggregate metrics already carried by `TuiEvent`, while existing detail rendering and all other semantic event summaries remain untouched.

**Tech Stack:** Python 3.12, Textual/Rich, pytest, Ruff

## Global Constraints

- Preserve one aggregated timeline row per `llm_call_id`.
- Do not show reasoning, content, reasoning-context, or delta excerpts in the row summary.
- Use `elapsed_ms` with the existing `duration_ms` fallback and duration formatter.
- Use `token_count` with `delta_count` fallback; do not substitute provider billing usage.
- Preserve all non-thinking event summaries and the bottom status bar.

---

### Task 1: Restore Metric-Only Thinking Rows

**Files:**
- Modify: `src/loom/tui/tui_app.py:307-375`
- Test: `tests/unit/test_tui_app.py:456-598`
- Test: `tests/unit/test_tui_app.py:1647-1691`

**Interfaces:**
- Consumes: `_event_conversation_parts(event: TuiEvent) -> tuple[str, str, str]`, `_format_seconds(value: Any) -> str`, and aggregate fields `elapsed_ms`, `token_count`, `delta_count`.
- Produces: `_format_event_line(event)` output such as `Thought for 3s 2 tokens >`; no new public interface.

- [ ] **Step 1: Write failing formatter and integration assertions**

Add exact row assertions covering plural metrics, singular grammar, and text suppression:

```python
def test_thinking_event_line_uses_elapsed_time_and_token_count_only():
    event = TuiEvent(
        timestamp=0,
        event_type="llm.stream.completed",
        data={
            "type": "llm.stream.completed",
            "elapsed_ms": 3410,
            "token_count": 2,
            "reasoning": "Inspect the project before editing.",
        },
        duration_ms=3410,
    )

    assert str(_format_event_line(event)) == "Thought for 3s 2 tokens >"


def test_thinking_event_line_uses_singular_token():
    event = TuiEvent(
        timestamp=0,
        event_type="llm.stream.started",
        data={"type": "llm.stream.started", "elapsed_ms": 1000, "delta_count": 1, "content": "hidden"},
    )

    assert str(_format_event_line(event)) == "Thought for 1s 1 token >"
```

Update `test_tui_curates_redundant_lifecycle_events_into_progress_rows` to expect its aggregated stream row as `Thought for 0s 1 token >`. Extend `test_tui_app_aggregates_llm_stream_tokens_into_one_sse_row` to assert `Thought for 0s 2 tokens >` so the aggregation path, not only the formatter, is covered.

- [ ] **Step 2: Run focused tests and verify the old behavior fails**

Run:

```bash
uv run pytest -q \
  tests/unit/test_tui_app.py::test_thinking_event_line_uses_elapsed_time_and_token_count_only \
  tests/unit/test_tui_app.py::test_thinking_event_line_uses_singular_token \
  tests/unit/test_tui_app.py::test_tui_curates_redundant_lifecycle_events_into_progress_rows \
  tests/unit/test_tui_app.py::test_tui_app_aggregates_llm_stream_tokens_into_one_sse_row
```

Expected: failures show the current `Thinking <reasoning/content>` output instead of metric-only output.

- [ ] **Step 3: Implement the minimal presentation change**

Replace the stream branch with metric formatting:

```python
    if event.event_type in {"llm.stream.started", "llm.stream.completed", "llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        elapsed = _format_seconds(data.get("elapsed_ms") or event.duration_ms)
        token_count = int(data.get("token_count") or data.get("delta_count") or 0)
        suffix = "tokens" if token_count != 1 else "token"
        return f"Thought for {elapsed}", f"{token_count} {suffix} >", COLORS["text_dim"]
```

Delete `_stream_progress_preview`, because no caller should remain.

- [ ] **Step 4: Run focused and surrounding TUI tests**

Run:

```bash
uv run pytest -q tests/unit/test_tui_app.py tests/unit/test_tui_event_summary.py tests/unit/test_tui_collector.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run complete verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git diff --check
```

Expected: all tests and checks pass. If the repository-wide format check has an unrelated pre-existing failure, report it and retain the focused format result.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/loom/tui/tui_app.py tests/unit/test_tui_app.py
git commit -m "fix: restore tui thinking metrics"
```
