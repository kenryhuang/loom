# Optimize Operations TUI Design

**Status:** Implemented and verified
**Date:** 2026-07-19  
**Extends:** `2026-07-19-meta-harness-optimize-command-design.md`

## Summary

`loom optimize --tui` must launch a real Textual application that makes a
long-running governed Meta-Harness campaign observable. The selected interface
is an Operations Dashboard: stage progression, candidates and paired trials,
live events, budgets, holdout state, and governance state remain visible at the
same time.

The TUI is an observer and controller over the existing optimization state
machine. SQLite stores, campaign artifacts, task traces, and governance
evidence remain authoritative. Selecting `--tui` must not change provider
streaming, candidate policy, evaluation, holdout isolation, or promotion
semantics.

Implementation verification on 2026-07-19 covered the full repository: 644
tests passed, 4 optional tests were skipped, and Ruff format/static checks were
clean. The implementation lives in `loom.optimize.events`, `control`,
`tui_state`, `tui_app`, and `tui_runner`, with runtime/orchestrator integration.

## Problem

The current `TuiObserver` in `src/loom/optimize/ui.py` is not a TUI. It wraps a
Rich `Console` and prints one line after each committed stage:

```text
preflight_complete completed
seed_analysis_complete completed
campaign_initialized completed
```

No Textual application is started. A stage emits only after completion, so a
proposer call or a set of paired trials can run for minutes without visible
activity. The implementation therefore does not satisfy the existing
one-command design, which requires live stage, trial, budget, holdout, and
governance views.

## Goals

- Launch a real full-screen Textual application for `loom optimize --tui`.
- Show useful activity before and during long model calls and paired trials.
- Render the selected Operations Dashboard layout.
- Give text, JSON, and TUI observers the same sanitized optimization events.
- Scope nested task, LLM, and tool events to their candidate and trial.
- Build an accurate initial view when resuming an existing optimization.
- Support safe pause, cancellation, detachment, and explicit approval.
- Keep TUI failures fail-open after a successful startup.
- Preserve holdout secrecy and existing governance boundaries.

## Non-goals

- Replacing SQLite, campaign stores, task traces, or artifact stores with UI
  state.
- Showing hidden chain-of-thought or provider-internal reasoning.
- Showing holdout task content, expected answers, judge rubrics, or detailed
  per-task holdout results.
- Letting the TUI directly mutate campaign databases.
- Making `--tui` implicitly enable different model streaming behavior.
- Creating a browser dashboard or remote multi-user control plane.

## Chosen Architecture

The implementation adds a dedicated Optimize TUI instead of overloading the
generic loop-oriented `LoomTuiApp`.

```text
OptimizeOrchestrator
  ├── stage lifecycle events
  ├── campaign and candidate events
  ├── paired-trial and budget events
  └── scoped task runtime events
       ├── run.* / step.*
       ├── llm.*
       └── tool.*
                    │
                    ▼
         OptimizationEventEmitter
          ├── TextObserver
          ├── JsonObserver
          └── OptimizeTuiObserver
                    │
                    ▼
              OptimizeTuiApp
```

The dedicated app may reuse colors, event-detail widgets, copy behavior, and
Textual conventions from `loom.tui`, but its data model is optimization-first.
It must not add campaign-specific branches to the generic agent-loop app.

## Component Boundaries

### Optimization event contracts

`src/loom/optimize/events.py` owns:

- the versioned observer-event envelope;
- event type constants;
- observer-session sequence assignment;
- scope enrichment for nested task events;
- holdout redaction and payload validation;
- immutable optimization snapshot contracts.

Every event contains:

```json
{
  "schema_version": "loom.optimization.event.v1",
  "type": "optimization.trial.started",
  "sequence": 17,
  "at": "2026-07-19T09:46:04.000000Z",
  "optimization_id": "opt_...",
  "campaign_id": "cmp_...",
  "stage": "search_running",
  "status": "running",
  "scope": {
    "phase": "discovery",
    "candidate_id": "cand_...",
    "experiment_id": "exp_...",
    "trial_id": "trial_...",
    "side": "candidate",
    "task_id": "audit-yakdb",
    "repetition": 3
  },
  "payload": {}
}
```

`sequence` is monotonic within one observer session. Durable stage and trial
identifiers provide cross-session identity. On resume, the emitter first emits
one `optimization.snapshot.loaded` event and then assigns later live events
from the same session sequence, avoiding a false representation that completed
work ran again.

### Observer model

`src/loom/optimize/ui.py` retains one `OptimizationObserver.emit(event)`
boundary:

- `TextObserver` prints concise lifecycle progress.
- `JsonObserver` writes the complete versioned event envelope.
- `OptimizeTuiObserver` sends the same envelope to a bounded TUI collector.

The Rich line-printing class named `TuiObserver` is removed. A TUI observer is
not responsible for starting or stopping Textual; lifecycle ownership belongs
to the TUI runner.

### TUI runner and controls

`src/loom/optimize/tui_runner.py` owns:

- dependency and terminal preflight;
- Textual app lifecycle;
- optimization snapshot loading;
- observer injection into the runtime;
- typed control requests from the app;
- detachment and text fallback;
- returning the original optimization `Result`.

The runner starts the app before composing or running work that can make model
calls. It monitors the optimization job and app independently. User detachment
or a render failure closes only the UI side; the optimization job continues.

The app sends typed requests through a controller queue. It never writes a
store directly. The runner invokes the same pause, cancellation, and approval
services used by non-interactive commands.

### Operations Dashboard app

`src/loom/optimize/tui_app.py` owns presentation state and widgets only. Its
input is an `OptimizationSnapshot` followed by event envelopes. It has no
provider, campaign-controller, evaluator, or governance dependencies.

### Nested task events

`loom.tasks.run_generic_task` gains an optional additional trace sink. The
normal task runner remains unchanged when it is absent. Optimize passes a
trial-scoped sink that enriches `run.*`, `step.*`, `llm.*`, and `tool.*` events
with candidate, experiment, task, repetition, phase, and side fields before
they enter the optimization observer.

This creates one TUI for the campaign. Individual paired trials must never
start their own Textual apps.

## Event Types

The observer stream includes these public groups:

### Optimization lifecycle

- `optimization.snapshot.loaded`
- `optimization.stage.started`
- `optimization.stage.replayed`
- `optimization.stage.completed`
- `optimization.stage.failed`
- `optimization.pause.requested`
- `optimization.paused`
- `optimization.cancel.requested`
- `optimization.detached`

### Search and candidates

- `optimization.proposal.started`
- `optimization.proposal.rejected`
- `optimization.candidate.admitted`
- `optimization.candidate.rejected`
- `optimization.frontier.updated`
- `optimization.search.sealed`

### Experiments and budgets

- `optimization.experiment.started`
- `optimization.trial.started`
- `optimization.trial.completed`
- `optimization.trial.failed`
- `optimization.experiment.completed`
- `optimization.budget.updated`

### Holdout and governance

- `optimization.holdout.status`
- `optimization.governance.started`
- `optimization.governance.awaiting_approval`
- `optimization.governance.completed`

### Nested runtime

Normal Loom runtime event types remain unchanged inside the envelope payload.
Their optimization scope is added by the trial sink rather than by modifying
the runtime engine.

Stage events use `started`, `replayed`, `completed`, and `failed` consistently.
The orchestrator emits `started` only after acquiring its durable stage lease,
`replayed` after validating stored output, `completed` after committing output,
and `failed` after cancelling the lease. Consequently, UI progress never gets
ahead of durable state.

## Holdout Redaction

All event payloads pass through the event-contract redactor before reaching any
observer. During holdout, observers may receive only:

- sealed/opened state;
- finalist and evaluated candidate identifiers;
- aggregate counts and aggregate metric summaries;
- evidence references and digests;
- risk, gates, and terminal disposition.

They may not receive:

- task prompts or workspace contents;
- expected outputs or verifier data;
- per-task scores or judge rationales;
- detailed failure text that reconstructs a hidden task;
- holdout dataset paths.

The same sanitized envelope goes to JSON, text, and TUI. Redaction is not a
view-layer concern and cannot be bypassed by changing output mode.

## Operations Dashboard Layout

The selected layout contains five always-visible regions.

### Header and pipeline

```text
Preflight -> Seed -> Search 1/4 -> Validation -> Holdout -> Governance
```

The header shows optimization ID, lifecycle, total elapsed time, current stage,
iteration, stage elapsed time, and lease state. Each pipeline node renders one
of `pending`, `running`, `completed`, `replayed`, `failed`, or `paused`.

### Candidate and paired-trial panel

Rows show:

- candidate ID and changed surface;
- baseline/candidate completed pairs and total pairs;
- current aggregate score and regression;
- frontier membership;
- validation/finalist state;
- rejection code or short reason.

The active-trial subsection shows phase, task ID, repetition, side, solver,
judge, current runtime event, and elapsed time.

### Live event panel

The event panel shows stage, proposal, candidate, trial, LLM, tool, holdout,
and governance events in observer sequence order. Selecting an event reveals a
sanitized structured detail view.

### Budget panel

The panel shows used and configured maximum values for:

- candidates;
- LLM calls;
- tokens when provider usage is available;
- monetary cost when prices are configured;
- wall-clock time.

Normal usage is neutral, approaching a limit is yellow, and a stop or exceeded
limit is red. Missing price data is displayed as unavailable rather than zero.

### Holdout and governance status

The bottom status area shows holdout sealed/opened state, evidence-reference
count, risk class, gates, approval state, and governance disposition. It never
renders hidden holdout content.

## Keyboard and Control Semantics

- `j`/`k` and arrow keys move through events or rows.
- `Enter` or `Space` expands the selected sanitized detail.
- `y` copies selected detail; `Y` copies the sanitized event transcript.
- `q` detaches the TUI. It does not pause, cancel, or terminate optimization.
- `p` requests a graceful pause.
- `c` opens a confirmation modal for cancellation of the active work.
- `a` is enabled only while awaiting approval and opens identity/reason input.

A graceful pause stops scheduling new trials or stages. The current atomic
operation is allowed to finish and persist, then lifecycle becomes `paused`.

A confirmed cancellation requests cancellation of the active stage/trial,
records its lease as cancelled, and leaves the optimization paused and
resumable. User cancellation must not be counted as candidate verifier or task
failure.

Approval requires an approver identity and optional reason and invokes the
existing approval service. The UI cannot synthesize an approval actor or skip
staleness, risk, evidence, or authorization checks.

After a terminal result the dashboard remains visible until `q`, with the
report path, disposition, and next action in the status area.

## Snapshot and Resume

At startup, the runner builds a sanitized snapshot from:

- `optimization.sqlite` aggregate and operations;
- campaign projection and frontier;
- experiment checkpoints and published summaries;
- configured budget limits;
- governance decision and approval request when present.

The snapshot does not reconstruct authority from TUI history. It is a read-only
projection of authoritative stores and artifacts.

Completed stages appear as completed or replayed immediately. The resumed
orchestrator still validates its stored operation input digest and output before
emitting `optimization.stage.replayed`. The TUI therefore cannot cause or mask
an invalid replay.

## Pause and Cancellation Checkpoints

Pause state currently exists in the optimization store but the orchestrator
does not consult it while running. The implementation adds checkpoints:

- before acquiring each stage lease;
- before each proposal batch;
- before scheduling each paired trial;
- after each completed atomic trial;
- before opening holdout;
- before governance action.

When pause is requested, no new model call or trial begins after the next
checkpoint. In-flight calls follow the configured timeout and cancellation
contract. Cancellation explicitly cancels the active operation lease before
the command returns a paused disposition.

## Failure Semantics

- Missing Textual dependencies or failure to start the app returns
  `TUI_UNAVAILABLE` before any new model call.
- After successful startup, app rendering and clipboard failures are
  observer failures and do not change optimization results.
- If the app exits unexpectedly, the runner emits a warning to stderr, swaps
  to `TextObserver`, and continues the job.
- Event-schema or redaction failure drops the unsafe event, records an observer
  warning, and never sends the unvalidated payload to a view.
- A full TUI queue coalesces replaceable streaming updates and retains bounded
  lifecycle history; it must not block campaign execution.
- JSON output remains complete and is not subject to TUI retention limits.
- Failure to persist pause, cancellation, or approval leaves the displayed
  control state unchanged and shows the returned Loom error.

## Bounded Retention

The Optimize TUI collector retains lifecycle events and a bounded recent event
window. It coalesces LLM content/reasoning deltas by call ID and trial scope.
Completed LLM and tool events replace their transient entries. Candidate,
experiment, stage, holdout, and governance lifecycle events are retained for
the session.

The event transcript copied with `Y` includes only retained sanitized events.
Authoritative complete traces remain in their existing trace stores.

## Compatibility

- `loom optimize` without `--tui` keeps concise text progress.
- `loom optimize --json` keeps machine-readable stdout and never starts
  Textual.
- `--tui` and `--json` remain mutually exclusive.
- Provider streaming settings are unchanged by TUI selection.
- Existing `loom task --tui`, evaluation TUI, evolution TUI, and generic
  `LoomTuiApp` behavior remain unchanged.
- The optional task trace-sink parameter defaults to `None`, preserving current
  callers.

The optimization event schema stays `loom.optimization.event.v1`. The missing
`type`, `sequence`, lifecycle variants, and sanitized scope are completed as
part of the previously underspecified v1 observer contract. New consumers must
branch on `type`; existing consumers that inspect `stage` and `status` continue
to work.

## Testing Strategy

### Event and observer tests

- Text, JSON, and TUI observers receive identical event types and sequence.
- Concurrent trial events preserve emitter sequence and correct scope.
- Holdout payloads containing forbidden fields fail redaction before any
  observer sees them.
- TUI queue retention and delta coalescing remain bounded.

### Textual tests

Use Textual `run_test()` to verify:

- pipeline state and elapsed-time rendering;
- candidate/trial row updates;
- live event selection and sanitized details;
- budget warning states;
- holdout and governance status;
- `q`, `p`, `c`, `a`, `y`, and `Y` behavior;
- terminal result remains until detach.

### Orchestrator and trial tests

- Stage events appear in `started/completed`, `started/failed`, and `replayed`
  order.
- A replay does not execute its service again.
- Trial-scoped nested runtime events contain phase, candidate, task,
  repetition, and side.
- Pause prevents the next stage/trial from starting.
- Cancellation cancels the active lease without producing candidate failure
  evidence.
- Resume does not repeat completed model calls.

### CLI and lifecycle regression tests

- `loom optimize --tui` starts an injected fake app before the optimization
  job and feeds it at least the initial snapshot.
- The regression test fails against the old Rich-only observer.
- TUI startup failure happens before provider invocation.
- Mid-run app failure falls back to text while the job succeeds.
- `q` detaches without cancelling the job.
- TUI and JSON runs over deterministic fake providers produce the same ordered
  optimization event types.

### End-to-end test

A deterministic fake-provider campaign runs from preflight through governance
with multiple paired trials. The test verifies stage progression, candidate
matrix data, budgets, holdout redaction, governance disposition, and final
report linkage without making external model calls.

## Acceptance Criteria

- Running the documented command with `--tui` visibly enters a Textual
  Operations Dashboard instead of printing Rich stage lines.
- The current stage or active trial is visible throughout model and verifier
  waits.
- A resumed demo immediately shows completed stages and continues with live
  events without repeating work.
- Candidate/trial progress and configured budget consumption are visible.
- Holdout task contents cannot be recovered from TUI or JSON events.
- Closing or crashing the TUI does not change campaign outcome.
- Pause, cancellation, and approval use durable existing control paths.
- All existing task, evaluation, evolution, campaign, governance, and TUI tests
  continue to pass.
