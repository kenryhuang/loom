# Evaluation Judge Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit streaming support to evaluation judge calls and emit the same live LLM stream events used by the Loom agent loop.

**Architecture:** Extract agent chat/stream response consumption into a public LLM-layer operation parameterized by an event emitter and stable event metadata. Keep semantic request/completion/failure events in each caller, then wire evaluation configuration, CLI parsing, round judges, and step judges to the shared transport when `--stream` is selected.

**Tech Stack:** Python 3.11, asyncio, OpenAI-compatible SSE, pytest, Textual event projection, Loom `Result` and `LlmResponse` contracts.

---

### Task 1: Extract a Reusable LLM Chat-or-Stream Operation

**Files:**
- Modify: `src/loom/llm/api.py:729-877`
- Modify: `src/loom/llm/__init__.py`
- Modify: `tests/llm/test_llm.py`

- [ ] **Step 1: Add a failing test for public streaming response consumption**

Import `request_llm_response` from `loom.llm`. Add a fake streaming provider that emits a reasoning delta, two content deltas, and a completed `LlmResponse`. Call the public operation with `stream=True`, a recording emitter, fixed metadata, and a fixed clock.

```python
result = await request_llm_response(
    provider,
    (LlmMessage("user", "judge"),),
    stream=True,
    emit_event=emit,
    event_metadata={
        "run_id": "run-eval",
        "loop_id": "loop-eval",
        "trace_id": "trace-source",
        "llm_call_id": "judge-1",
        "step_number": 3,
        "model": provider.model,
    },
    now=lambda: NOW,
)

assert result.ok
assert result.value.content == '{"overall":0.8}'
assert [event["type"] for event in events] == [
    "llm.stream.started",
    "llm.reasoning.delta",
    "llm.content.delta",
    "llm.content.delta",
    "llm.stream.completed",
]
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/llm/test_llm.py -k request_llm_response -q`

Expected: collection fails because `request_llm_response` is not exported.

- [ ] **Step 3: Implement the public operation**

Add a public async operation with this contract:

```python
async def request_llm_response(
    provider: Any,
    messages: Sequence[LlmMessage],
    tools: tuple[dict[str, Any], ...] | None = None,
    cancellation: Any = None,
    tool_choice: Any = None,
    *,
    stream: bool = False,
    emit_event: Any = None,
    event_metadata: Mapping[str, Any] | None = None,
    now: Any = now_iso,
) -> Result:
    ...
```

Move the existing non-stream tool-choice fallback and streaming response
assembly behind this operation. A private emitter adapter must accept sync or
async callbacks and normalize `None` to `ok(None)`. Streaming lifecycle and
delta events merge `event_metadata` without changing the current event fields.

- [ ] **Step 4: Rewire the agent loop to the shared operation**

Replace the private `_chat_or_stream` call with `request_llm_response`, passing:

```python
emit_event=lambda event: _emit_runtime_event(runtime, event)
event_metadata={
    "run_id": context.run_id,
    "loop_id": runtime.loop_id,
    "trace_id": trace_id,
    "llm_call_id": llm_call_id,
    "step_number": as_step_number(len(context.state.observations)),
    "model": provider.model,
}
now=runtime.now
```

Export `request_llm_response` from `loom.llm`.

- [ ] **Step 5: Run LLM tests and verify GREEN**

Run: `uv run pytest tests/llm/test_llm.py -q`

Expected: all LLM tests pass, including existing agent streaming and
tool-choice fallback tests.

- [ ] **Step 6: Commit the shared transport extraction**

```bash
git add src/loom/llm/api.py src/loom/llm/__init__.py tests/llm/test_llm.py
git commit -m "refactor: share llm streaming response handling"
```

### Task 2: Add Explicit Evaluation Streaming Configuration

**Files:**
- Modify: `src/loom/evaluation/analyze.py:25-55, 390-420`
- Modify: `tests/evaluation/test_analyze.py:218-235`

- [ ] **Step 1: Add failing CLI parsing tests**

Extend evaluation option tests with:

```python
assert parse_run_options(("--trace-path", str(trace_path))).config.stream is False
assert parse_run_options(("--trace-path", str(trace_path), "--stream")).config.stream is True
assert parse_run_options(("--trace-path", str(trace_path), "--no-stream")).config.stream is False
```

- [ ] **Step 2: Run the CLI tests and verify RED**

Run: `uv run pytest tests/evaluation/test_analyze.py -k 'parse_run_options or cli_help' -q`

Expected: FAIL because `EvaluationConfig` has no `stream` field and argparse
does not recognize the options.

- [ ] **Step 3: Add the configuration field and Boolean CLI pair**

Add `stream: bool = False` to `EvaluationConfig` and:

```python
parser.add_argument(
    "--stream",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Stream LLM judge reasoning and content deltas when supported.",
)
```

Pass `args.stream` through `_config_from_options`. Keep `EvaluationRunOptions`
unchanged because streaming is part of evaluation behavior, not TUI selection.

- [ ] **Step 4: Run CLI tests and verify GREEN**

Run: `uv run pytest tests/evaluation/test_analyze.py -k 'parse_run_options or cli_help' -q`

Expected: selected tests pass and help displays `--stream | --no-stream`.

- [ ] **Step 5: Commit CLI configuration**

```bash
git add src/loom/evaluation/analyze.py tests/evaluation/test_analyze.py
git commit -m "feat: add evaluation judge stream option"
```

### Task 3: Stream Round and Step Judge Calls

**Files:**
- Modify: `src/loom/evaluation/judge.py:225-380`
- Modify: `src/loom/evaluation/analyze.py:120-245`
- Modify: `tests/evaluation/test_judge.py`
- Modify: `tests/evaluation/test_analyze.py`

- [ ] **Step 1: Add a failing streaming judge integration test**

Create a fake provider exposing both `chat` and `stream_chat`. Its two streams
produce valid round and step judge JSON responses plus reasoning/content deltas.
Run:

```python
result = await analyze_trace(
    EvaluationConfig(trace_path=trace_path, out_dir=out_dir, judge=True, stream=True),
    judge_provider=provider,
    event_sink=sink,
)
```

Assert:

```python
assert result.ok
assert provider.chat_calls == 0
assert provider.stream_calls == 2
assert [event["type"] for event in sink.events].count("llm.stream.started") == 2
assert [event["type"] for event in sink.events].count("llm.reasoning.delta") == 2
assert [event["type"] for event in sink.events].count("llm.content.delta") >= 2
assert [event["type"] for event in sink.events].count("llm.stream.completed") == 2
assert len(result.value.round_judge_assessments) == 1
assert len(result.value.judge_assessments) == 1
```

- [ ] **Step 2: Add a failing fallback test for chat-only judge providers**

Run the same evaluation with `stream=True` and the existing `FakeJudgeProvider`
that only defines `chat`. Assert it succeeds and produces both judge artifacts.

- [ ] **Step 3: Run the judge tests and verify RED**

Run: `uv run pytest tests/evaluation/test_analyze.py tests/evaluation/test_judge.py -k stream -q`

Expected: streaming integration fails because judges still call `provider.chat`.

- [ ] **Step 4: Wire judges to `request_llm_response`**

Give `LlmRoundJudge` and `LlmStepJudge` a `stream: bool = False` constructor
argument. Replace direct provider calls with:

```python
response = await request_llm_response(
    self.provider,
    messages,
    tools=None,
    stream=self.stream,
    emit_event=lambda event: _emit_event(event_sink, event),
    event_metadata=event_base,
    now=now_iso,
)
```

Keep existing `llm.requested`, `llm.completed`, `llm.failed`, and assessment
parsing around the shared response call.

- [ ] **Step 5: Pass evaluation configuration into both judges**

Change `_judge_steps` to accept `stream: bool`, construct both judges with that
flag, and call it from `analyze_trace` with `stream=config.stream`.

- [ ] **Step 6: Run evaluation tests and verify GREEN**

Run: `uv run pytest tests/evaluation/test_analyze.py tests/evaluation/test_judge.py -q`

Expected: all evaluation and judge tests pass in streaming and non-streaming
modes.

- [ ] **Step 7: Commit judge streaming integration**

```bash
git add src/loom/evaluation/analyze.py src/loom/evaluation/judge.py tests/evaluation/test_analyze.py tests/evaluation/test_judge.py
git commit -m "feat: stream evaluation judge responses"
```

### Task 4: Regression and Live Verification

**Files:**
- Verify: `src/loom/llm/api.py`
- Verify: `src/loom/evaluation/analyze.py`
- Verify: `src/loom/evaluation/judge.py`
- Verify: `tests/llm/test_llm.py`
- Verify: `tests/evaluation/test_analyze.py`
- Verify: `tests/evaluation/test_judge.py`

- [ ] **Step 1: Run focused regression suites**

Run: `uv run pytest tests/llm/test_llm.py tests/evaluation -q`

Expected: all focused tests pass.

- [ ] **Step 2: Run lint checks**

Run: `uv run ruff check src/loom/llm src/loom/evaluation tests/llm tests/evaluation`

Expected: no lint errors.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`

Expected: all non-live tests pass; explicitly gated live tests remain skipped.

- [ ] **Step 4: Run one bounded live streaming judge probe**

Create a one-step fixture trace from the existing evaluation test fixture or a
small completed Loom trace. Run evaluation with `--judge --stream --model
kimi_judge` and no TUI so emitted output can complete unattended.

Expected: the command exits successfully and generated artifacts contain one
round judge assessment and one step judge assessment. Do not run the 45-call
YakDB judge corpus as the smoke test.

- [ ] **Step 5: Inspect final scope**

Run: `git status --short && git diff --check && git log --oneline -6`

Expected: only intended files changed, no whitespace errors, and all
implementation commits are present.
