# Streaming Workflow Route Tool Choice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Auto workflow routing compatible with streaming thinking/reasoning models while preserving strict route selection and actionable HTTP errors.

**Architecture:** Auto routing will request provider-supported automatic tool selection and retain Loom's existing bounded `require_tool_call` enforcement. The OpenAI-compatible SSE transport will convert urllib HTTP failures into an internal exception carrying status and response body so `_tool_choice_retryable` can make the same decision in streaming and non-streaming paths.

**Tech Stack:** Python 3.11, asyncio, urllib, pytest, Loom `Result`/`LoomError` contracts.

---

### Task 1: Make Auto Routing Use Compatible Tool Choice

**Files:**
- Modify: `tests/runtime/test_planning.py:199-221`
- Modify: `src/loom/runtime/planning.py:470-485`

- [ ] **Step 1: Change the routing policy test to require automatic provider selection**

```python
policy = planning.step_policy(_context(*normal))
assert policy.tool_choice == "auto"
assert policy.require_tool_call is True
assert policy.missing_tool_call_retries == 1
assert policy.preserve_all_tools is True
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/runtime/test_planning.py::test_auto_routing_exposes_exact_choice_and_continue_crosses_boundary -q`

Expected: FAIL because the current policy returns `"required"`.

- [ ] **Step 3: Implement the minimal routing-policy change**

In `PlanningRuntime.step_policy`, change only the provider hint:

```python
return LlmStepPolicy(
    tool_choice="auto",
    preserve_all_tools=True,
    require_tool_call=True,
    missing_tool_call_retries=1,
    invalid_tool_call_retries=1,
    retry_prompt="Call exactly one of enter_plan or continue_react with a concise reason.",
    failure_code="WORKFLOW_ROUTE_FAILED",
)
```

- [ ] **Step 4: Run focused planning tests and verify GREEN**

Run: `uv run pytest tests/runtime/test_planning.py tests/runtime/test_workflow_routing.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the routing-policy change**

```bash
git add src/loom/runtime/planning.py tests/runtime/test_planning.py
git commit -m "fix: use compatible tool choice for workflow routing"
```

### Task 2: Preserve Streaming HTTP Errors and Compatibility Retry

**Files:**
- Modify: `tests/llm/test_llm.py`
- Modify: `src/loom/llm/api.py:735-797, 923-972, 1898-1935`

- [ ] **Step 1: Add a failing streaming compatibility test**

Add a test whose streaming provider raises an HTTP-style error with the DashScope diagnostic on its first `tool_choice="required"` call, then returns a valid tool-call stream when retried without `tool_choice`. Assert the recorded provider calls are `("required", None)` and the Loom step succeeds.

```python
assert result.ok
assert provider.tool_choices == ["required", None]
```

- [ ] **Step 2: Add a failing urllib streaming error-detail test**

Patch `urllib.request.urlopen` to raise `urllib.error.HTTPError` whose body is:

```json
{"error":{"code":"InvalidParameter","message":"The tool_choice parameter does not support being set to required in thinking mode"}}
```

Consume `provider.stream_chat(...)` and assert the raised message contains both `400` and `tool_choice` rather than only `HTTP Error 400: Bad Request`.

- [ ] **Step 3: Run both new tests and verify RED**

Run: `uv run pytest tests/llm/test_llm.py -k 'streaming_http_error or streaming_required_tool_choice' -q`

Expected: the error-detail assertion fails and the compatibility call is not retried.

- [ ] **Step 4: Add a structured internal streaming HTTP exception**

Introduce a private exception used only at the transport boundary:

```python
class _OpenAIHTTPError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        message = _openai_error_message(status, body)
        super().__init__(message)
```

Use one private `_openai_error_message(status, payload)` helper for both chat and streaming responses. It should prefer `payload["error"]["message"]`, include the HTTP status, and fall back to readable response text without exposing request headers.

- [ ] **Step 5: Convert urllib streaming failures at their source**

In `_urlopen_stream_chunks`, catch `urllib.error.HTTPError` before the generic exception, safely read/decode/parse its body, and enqueue `_OpenAIHTTPError(exc.code, payload)`. Keep the generic exception branch for non-HTTP failures.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/llm/test_llm.py -k 'tool_choice or stream' -q`

Expected: all selected tests pass, including non-stream fallback coverage.

- [ ] **Step 7: Commit the streaming transport fix**

```bash
git add src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "fix: preserve streaming provider errors for fallback"
```

### Task 3: Regression and Live Verification

**Files:**
- Verify: `tests/integration/test_live_llm_smoke.py`
- Verify: `/tmp/loom-workflow-route-stream.jsonl`

- [ ] **Step 1: Run focused regression suites**

Run: `uv run pytest tests/runtime/test_planning.py tests/runtime/test_workflow_routing.py tests/llm/test_llm.py tests/tasks -q`

Expected: all tests pass.

- [ ] **Step 2: Run lint checks**

Run: `uv run ruff check src/loom/runtime/planning.py src/loom/llm/api.py tests/runtime/test_planning.py tests/llm/test_llm.py`

Expected: no lint errors.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`

Expected: all non-live tests pass; live tests remain skipped unless explicitly enabled.

- [ ] **Step 4: Run a live streaming Auto routing probe**

Run a one-step, no-TUI task with `--stream`, `--plan-mode auto`, model `main`, and a complex read-only objective, writing its trace to `/tmp/loom-workflow-route-stream.jsonl`.

Expected: no HTTP 400; trace contains `workflow.route.selected` and `llm.completed`. The command may end with the deliberate `max_steps exceeded` after proving routing succeeded.

- [ ] **Step 5: Inspect scope and commit any final test-only adjustment**

Run: `git status --short && git diff --check && git log --oneline -5`

Expected: only intended files changed, no whitespace errors, and implementation commits are present.
