# Persistent Service and Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver a local persistent Loom service with independent task sessions, cooperative execution, resumable human input, replayable events and a TUI client.

**Architecture:** A single supervisor owns SQLite state and authenticated HTTP/SSE endpoints. Spawned workers execute the existing provider, runtime and planning primitives through a managed LLM step; all state mutations cross a serialized supervisor boundary. Clients consume snapshots and durable events.

**Tech Stack:** Python >=3.11, standard-library SQLite/HTTP/multiprocessing/asyncio, existing optional Textual/Rich TUI. No new mandatory dependencies.

**Spec:** `docs/superpowers/specs/2026-10-03-persistent-service-sessions-design.md`

## Global Constraints

- Local, single-user service, loopback binding and authenticated commands/events.
- One Task per Session, one executor per Session, bounded global execution concurrency.
- Serialize executions sharing a canonical workspace; do not automatically create or merge worktrees.
- Persist commands and display events before acknowledging or publishing them.
- Preserve existing one-shot task/runtime entry points.
- Keep waiting, pause, stop and completion distinct; never blindly replay uncertain side effects.
- TUI is the initial frontend assumption; transport and state contracts remain frontend independent.
- Scope includes the complete interaction loop; domain SDK and automatic method optimization remain separate.

## Review Focus

- Duplicate commands with different payloads must conflict, including after process restart (Task 1).
- Late worker messages must not overwrite a newer epoch; an orphan tool must keep its workspace blocked (Tasks 1 and 4).
- Interrupted native tool batches must retain valid call/result pairing and completed observations (Task 3).
- A snapshot/subscription race and a slow subscriber must not lose durable events or block execution (Task 5).
- Session switching must reject old connection events and preserve queued/applied user-message status (Task 6).

## File Structure

- `service/contracts.py`: validation, states, errors and public envelopes.
- `service/store.py`, `service/artifacts.py`: transactional commands/state/events and immutable artifact publication.
- `runtime/checkpoints.py`, `runtime/control.py`: allowlisted codecs and normal suspension outcomes.
- `llm/managed_step.py`: resumable provider/tool interaction using existing prompt, parsing and policy helpers.
- `service/agent.py`, `service/workers.py`, `service/controller.py`, `service/scheduler.py`: runtime assembly, IPC, task control and worker scheduling.
- `service/api.py`, `service/cli.py`: authenticated command/query/SSE and daemon lifecycle.
- `client/protocol.py`, `client/projection.py`, `client/tui.py`, `client/cli.py`: remote interactions, replay projection and initial UI.
- Package `__init__.py` files remain explicit export shims.

### Task 1: Durable Session Store and Checkpoint Contracts

**Files:** Create `src/loom/service/{__init__,contracts,artifacts,store}.py`, `src/loom/runtime/{control,checkpoints}.py`; tests in `tests/service/test_store.py`, `tests/runtime/test_checkpoints.py`.

**Interfaces:** `SessionStore(directory)`; `create(command_id, payload) -> dict`; `snapshot(session_id) -> dict`; `submit(session_id, command) -> dict`; `events(session_id, after=0, limit=200) -> list[dict]`; `update(session_id, callback) -> dict`; `save_artifact(session_id, value, kind) -> dict`; `read_artifact(session_id, digest) -> bytes`. `encode(value) -> JSON`; `decode(value) -> typed value`; `StepControl(kind, reason, request_id)`.

- [x] Write RED tests for persisted command deduplication, conflicts, event ordering, snapshot cursors, typed Context round trips, unknown codec types and artifact integrity:
  ```python
  store = SessionStore(tmp_path)
  first = store.create("create-1", {"objective": "Maintain module", "workspace": str(tmp_path)})
  assert SessionStore(tmp_path).create("create-1", first["input"]) == first
  with pytest.raises(ServiceError):
      store.create("create-1", {"objective": "Different", "workspace": str(tmp_path)})
  assert decode(encode(context)) == context
  ```
- [x] Run `pytest -q tests/service/test_store.py tests/runtime/test_checkpoints.py`; expect missing-module RED.
- [x] Implement SQLite transactions under a supervisor lock, immutable JSON artifacts with digests, explicit command validation and allowlisted dataclass codecs. Command transactions append acceptance events with persistent session sequence numbers.
  ```python
  with self.transaction() as connection:
      replay = self.command_replay(connection, command_id, canonical_input)
      if replay is not None:
          return replay
      self.append_event(connection, session_id, "command.accepted", payload)
  ```
- [x] Run the same tests and verify GREEN; commit `feat: add durable session state and checkpoint contracts`.

### Task 2: Cooperative Runtime and Managed Tools

**Files:** Modify `core/models.py`, `runtime/engine.py`, `runtime/planning.py`, `tasks/tools.py`; test `tests/runtime/test_service_control.py`, `tests/tasks/test_async_tools.py`.

**Interfaces:** `StepResult.control: StepControl | None`; optional restored `trace_id` argument to `step`; planning `snapshot() -> dict` / `restore(dict) -> Result`; managed subprocess cancellation via the existing options argument.

- [x] Write RED tests proving suspended results use `step.suspended`, preserve trace identity, and do not become INTERNAL; planning snapshots restore route state; long shell execution does not block the event loop and can terminate its process group.
  ```python
  result = await step(handle, context, trace_id="trace_resume")
  assert result.value.control.kind == "paused"
  assert events[-1]["type"] == "step.suspended"
  shell = asyncio.create_task(tool({"command": slow_command}, {}))
  await asyncio.sleep(0.02)
  assert not shell.done()
  ```
- [x] Run `pytest -q tests/runtime/test_service_control.py tests/tasks/test_async_tools.py`; expect behavior RED.
- [x] Return suspension as an ordinary typed result and preserve the legacy path when control is absent. Add versioned planning participant snapshots. Replace blocking shell invocation with asyncio subprocess management, bounded output, timeout and process-group cleanup; move file operations to threads.
  ```python
  if step_result.control and step_result.control.kind in {"paused", "waiting_input", "stopped"}:
      await emit({"type": "step.suspended", "trace": step_result.trace})
      return ok(step_result)
  ```
- [x] Run new tests plus `tests/runtime` and `tests/tasks`; commit `feat: add cooperative step suspension and managed tools`.

### Task 3: Resumable LLM and Human Input

**Files:** Create `llm/managed_step.py`; modify `llm/api.py` only for optional managed execution dispatch; test `tests/llm/test_managed_step.py`.

**Interfaces:** `ManagedStep(provider, execution, planning, limits)` is an async callable returning `Result[StepResult]`. Execution bridge offers `event(event)`, `boundary(checkpoint)`, `operation_start(call)`, `operation_finish(call, result)`, `request_input(call, question)`. Checkpoints carry Context, messages, pending response/index, observations, usage and planning participant.

- [x] Write RED fake-provider tests for native and JSON tool actions, after-tool pause/resume without replay, clarification answer injection, goal changes cancelling unexecuted calls, plan transitions, token/window limits and partial result preservation.
  ```python
  first = await managed(context, runtime)
  assert first.value.control.kind == "paused"
  resumed = await restored(first_checkpoint, runtime)
  assert tool.calls == 1
  assert resumed.value.control.kind == "completed"
  ```
- [x] Run `pytest -q tests/llm/test_managed_step.py`; expect missing-module RED.
- [x] Implement explicit before-LLM, response, tool-batch and finalization phases. Reuse `build_messages`, decision parsing, tool-result recovery and observation policy; persist before each new action. Request-input calls suspend with an unresolved tool result, resolved exactly once by answer or supersede. Generated interrupted results must keep each native tool call paired with a result.
  ```python
  directive = execution.boundary(checkpoint)
  if directive["control"]:
      return suspended_result(directive["control"], checkpoint)
  ```
- [x] Verify GREEN and legacy `tests/llm` / `tests/tasks`; commit `feat: implement resumable LLM task interactions`.

### Task 4: Worker Supervisor and Continuous Task Controller

**Files:** Create `service/{agent,workers,scheduler,controller}.py`; test `tests/service/{fakes,test_controller,test_workers}.py`.

**Interfaces:** `LoomService(directory, config_path=None, max_active_runs=2, provider_factory=None)`; `create`, `submit`, `snapshot`, `events`; `start()`, `close()`. Worker IPC carries per-attempt record IDs and epochs, persisted results are acknowledged before continuation.

- [x] Write RED integration tests with importable fake providers for concurrent sessions, workspace serialization, ordered steering, clarification, pause/resume/stop, restart, stale epoch refusal, failure isolation and uncertain operation recovery.
  ```python
  service.submit(session_id, {"command_id": "pause-1", "type": "pause", "payload": {}})
  await_state(service, session_id, "paused")
  service.close()
  restarted = LoomService(directory, provider_factory=factory)
  assert restarted.snapshot(session_id)["task"]["state"] == "paused"
  ```
- [x] Run `pytest -q tests/service/test_controller.py tests/service/test_workers.py`; expect missing-module RED.
- [x] Implement spawned worker IPC, executor epochs, durable tool ledger, per-workspace ownership and FIFO ready scheduling. Configure providers only from server-owned config. Finalized runs return Task to idle; suspended runs retain identity. Recovery uses deterministic ledger reconciliation or an explicit recovery question.
  ```python
  if operation["status"] == "started" and operation["effect_kind"] == "side_effecting":
      state["task"]["state"] = "recovering"
  ```
- [x] Verify all integration tests GREEN; commit `feat: supervise persistent multi-session execution`.

### Task 5: Authenticated API, Durable SSE and Client Protocol

**Files:** Create `service/{api,cli}.py`, `client/{__init__,protocol,projection}.py`; modify `loom/cli.py`; test `tests/service/test_api.py`, `tests/client/test_protocol.py`.

**Interfaces:** `ServiceHTTPServer(address, service, token)`; `SessionClient(url, token)` with create/command/snapshot/history/events/artifact methods. SSE yields full persisted event envelopes. Client projection applies each seq once and merges text by identity/offset.

- [x] Write RED loopback tests for auth, invalid payload sizes, command conflicts, JSON errors, snapshot/subscription races, Last-Event-ID, two readers, slow disconnect, event dedup and artifact authorization.
  ```python
  snapshot = client.snapshot(session_id)
  client.command(session_id, message)
  event = next(client.events(session_id, snapshot["event_cursor"]))
  assert event["seq"] > snapshot["event_cursor"]
  ```
- [x] Run `pytest -q tests/service/test_api.py tests/client/test_protocol.py`; expect missing-module RED.
- [x] Implement standard-library threaded HTTP with constant-time token checks, bounded bodies and SSE read-after-cursor loops. API threads never invoke models. Persist installation credentials privately; client resolves credentials from explicit env/path, not query strings. Add `loom serve` and `loom session` command dispatch.
  ```python
  for event in store.events(session_id, after=cursor):
      write_sse(id=event["seq"], event=event["type"], data=event)
  ```
- [x] Verify GREEN; commit `feat: expose session commands and replayable event streams`.

### Task 6: TUI Service Client and User Documentation

**Files:** Create `client/{tui,cli}.py`; test `tests/client/test_tui.py`; update `README.md`, `tests/test_package_structure.py` and plan/spec status with verified limitations.

**Interfaces:** `SessionTuiApp(client, session_id=None)`; optional Textual imports only when running the TUI. `loom session` supports list/create/message/control and interactive connection.

- [x] Write RED headless UI tests for session switching, message submission, pending question answering, controls and stale-stream rejection; smoke CLI commands against the real HTTP server.
  ```python
  async with app.run_test() as pilot:
      await app.select_session(session_id)
      await app.submit_text("Prioritize the regression")
      assert client.commands[-1]["type"] == "submit_message"
  ```
- [x] Run `pytest -q tests/client/test_tui.py`; expect missing-module RED.
- [x] Compose session list, existing execution feed, task/plan state, message input and controls. Load snapshot plus paged history, then follow events; ignore old session-generation callbacks after switching. Render question and recovery states explicitly. Document serve/connect/auth/config/restart and the distinction between finish, pause and stop.
  ```python
  generation = self.subscription_generation
  if generation == self.subscription_generation:
      self.apply_session_event(event)
  ```
- [x] Run the full suite, Ruff checks, CLI smoke and review; commit `feat: add persistent session TUI and usage documentation`.

## Final Verification and Review

- [x] Run all existing and added tests from this worktree with the shared virtualenv.
- [x] Run Ruff lint, format checks and `git diff --check` on changed files.
- [x] Run an actual service/client/fake-provider process smoke including disconnect and reconnect.
- [ ] Request the fresh whole-branch reviewer required by executing-plans; resolve material findings with failing tests first.
- [ ] Preserve the feature branch and worktree for user review; do not merge or push without authorization.

## Execution Decision

The user explicitly requested a branch and implementation. Execute inline in this session, with progress recorded in the plan ledger and one independent final review. The written-plan handoff does not introduce a second permission request for already authorized implementation.

## Verification Evidence

- Full suite: 923 passed, 4 skipped (live-model tests remain opt-in).
- Ruff lint: entire src/tests pass; format check: all 43 changed Python files pass. The repository has 27 existing formatting differences outside this change.
- Process smoke: an actual daemon with spawned workers survives frontend disconnect and service restart while awaiting input, then resumes the same Run.
- Additional regressions: cancellation during model requests, active-time accounting, committed final response recovery, durable planning transition recovery, tool-process registration before effects, and delayed TUI scroll callbacks.
