# Native Proposer Protocol Retry Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `LoomNativeProposerAdapter` recover from bounded structured-output protocol errors while preserving every rejected raw response and retaining strict candidate policy validation.

**Architecture:** Keep retry ownership inside the native proposer adapter because it owns the model-output protocol and trusted workspace. Query campaign history once, issue at most `max_protocol_retries + 1` provider calls, feed the exact validation failure back to the model, accumulate usage across attempts, and cache only a fully validated proposal batch. Candidate/controller policy remains fail-closed and unchanged.

**Tech Stack:** Python 3.11, immutable Loom `Result`/`LoomError` contracts, `LlmMessage`, `CandidateWorkspace`, pytest/pytest-asyncio.

## Global Constraints

- The only admitted candidate kind remains the exact string `declarative_patch`.
- Invalid surfaces, workspace paths, operations, hypotheses, and duplicate drafts remain rejected.
- Validation and holdout content must never enter proposer prompts or retry feedback.
- Every rejected provider response is written beneath its allocated candidate workspace.
- Protocol retries are bounded; exhaustion returns non-retryable `PROPOSAL_FAILED` with attempt evidence.
- Successful usage accounts for all protocol attempts, including rejected responses.

---

### Task 1: Add bounded native-proposer protocol recovery

**Files:**
- Modify: `tests/optimize/test_proposer.py`
- Modify: `src/loom/optimize/proposer.py`

**Interfaces:**
- Consumes: `LoomNativeProposerAdapter.propose(request, history, workspace)` and provider `chat()` responses.
- Produces: `LoomNativeProposerAdapter(..., max_protocol_retries: int = 2)` with durable rejected-response artifacts and cumulative `ProposalUsage`.

- [ ] **Step 1: Write the failing regression tests**

Add a sequential fake provider and tests equivalent to:

```python
class SequenceChatProvider:
    def __init__(self, contents):
        self.contents = list(contents)
        self.messages = []
        self.calls = 0

    async def chat(self, messages, **kwargs):
        content = self.contents[self.calls]
        self.calls += 1
        self.messages.append(tuple(messages))
        return ok(LlmResponse(content=content, usage=TokenUsage(10, 32, 42)))


@pytest.mark.asyncio
async def test_native_proposer_retries_invalid_kind_with_validation_feedback(tmp_path):
    invalid = _response({**_draft(), "kind": "declarative"})
    provider = SequenceChatProvider((invalid, _response(_draft())))
    workspace = CandidateWorkspace.allocate(tmp_path, "protocol-retry").unwrap()
    adapter = LoomNativeProposerAdapter(provider, _baseline(), evidence_refs=("trace:seed",), max_protocol_retries=1)

    batch = (await adapter.propose(_request(), RecordingHistory(), workspace)).unwrap()

    assert provider.calls == 2
    assert batch.usage.proposer_tokens == 84
    assert (workspace.root / "proposer-raw-response-attempt-1.txt").read_text() == invalid
    assert "declarative_patch" in "\n".join(message.content or "" for message in provider.messages[1])


@pytest.mark.asyncio
async def test_native_proposer_protocol_retry_exhaustion_preserves_every_raw_response(tmp_path):
    provider = SequenceChatProvider((_response({**_draft(), "kind": "bad-a"}), _response({**_draft(), "kind": "bad-b"})))
    workspace = CandidateWorkspace.allocate(tmp_path, "protocol-exhausted").unwrap()
    adapter = LoomNativeProposerAdapter(provider, _baseline(), evidence_refs=("trace:seed",), max_protocol_retries=1)

    result = await adapter.propose(_request(), RecordingHistory(), workspace)

    assert result.error.code == "PROPOSAL_FAILED"
    assert result.error.retryable is False
    assert result.error.metadata["attempts"] == 2
    assert len(result.error.metadata["raw_response_paths"]) == 2
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/optimize/test_proposer.py -q
```

Expected: the sequential-invalid-kind test fails because the adapter returns immediately and does not accept `max_protocol_retries`.

- [ ] **Step 3: Implement the minimal bounded retry loop**

In `LoomNativeProposerAdapter`:

```python
def __init__(..., max_protocol_retries: int = 2):
    if max_protocol_retries < 0:
        raise ValueError("max_protocol_retries must be non-negative")
    self.max_protocol_retries = max_protocol_retries
```

Refactor `propose()` so one history query builds the base messages and a loop:

```python
for attempt in range(1, self.max_protocol_retries + 2):
    response = await self.provider.chat(tuple(messages), tools=None)
    total_tokens += response.value.usage.total_tokens
    decoded = self._decode_and_materialize(request, workspace, response.value.content or "")
    if decoded.ok:
        return self._cache_batch(decoded.value, total_tokens, elapsed, workspace, request_digest)
    persisted = self._persist_rejected_response(workspace, attempt, raw)
    if not persisted.ok:
        return persisted
    raw_paths.append(str(persisted.value))
    if attempt > self.max_protocol_retries:
        return _proposal_error(
            "Native proposer protocol retries exhausted",
            attempts=attempt,
            last_protocol_error=decoded.error.message,
            raw_response_path=raw_paths[-1],
            raw_response_paths=tuple(raw_paths),
        )
    messages.extend((
        LlmMessage("assistant", raw),
        LlmMessage("user", self._retry_feedback(decoded.error)),
    ))
```

Keep provider transport errors outside this protocol loop. Keep `_materialize_draft()` strict.

- [ ] **Step 4: Make the output contract explicit**

Add a canonical `output_contract` object to the proposer user message with required `drafts`, exact `kind.const = "declarative_patch"`, one `changed_surfaces` item, non-empty operations, and required falsifiable hypothesis arrays. Update the system message to require raw JSON without Markdown.

- [ ] **Step 5: Run focused and integration tests**

Run:

```bash
uv run pytest tests/optimize/test_proposer.py tests/optimize/test_orchestrator.py tests/integration/test_optimize_end_to_end.py -q
uv run ruff check src/loom/optimize/proposer.py tests/optimize/test_proposer.py
uv run ruff format --check src/loom/optimize/proposer.py tests/optimize/test_proposer.py
```

Expected: all tests and checks pass.

- [ ] **Step 6: Resume the recorded demo optimization**

Run the original `loom optimize` command. Expected: preflight, seed analysis, and campaign initialization replay without new model calls; proposer either succeeds within the bounded attempts or returns an evidence-bearing exhaustion error listing every raw-response path.

- [ ] **Step 7: Commit the fix**

```bash
git add src/loom/optimize/proposer.py tests/optimize/test_proposer.py docs/superpowers/plans/2026-07-19-native-proposer-protocol-retry-fix.md
git commit -m "fix: retry native proposer protocol errors"
```
