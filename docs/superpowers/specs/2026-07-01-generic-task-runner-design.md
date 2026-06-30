# Generic Task Runner Design

This document designs a generic task runner for Loom. It is written as an
extension of the original Loom Python design in
`docs/superpowers/specs/2026-06-05-loom-py-design.md`.

The goal is to move beyond `real_project_smoke.py` as a hard-coded case while
preserving Loom's original design shape: small typed contracts, explicit
context layers, minimal loop definitions, trace-first execution, bounded
composition, and evolution through auditable proposals.

## Source Principles

The original Loom Python port set these priorities:

- Keep compatibility with Loom TypeScript semantics where traces and
  composition matter.
- Use Python idioms: dataclasses, explicit factory functions, async control
  flow.
- Keep the public API small and well-typed.
- Favor explicitness over magic.
- Prefer immutable dataclasses for trace artifacts and context layers.
- Keep packages modular and compact.

The generic task runner should follow those principles more strictly than a
demo does. It should not become a hidden agent framework with implicit global
state, dynamic prompt mutation, or unbounded loop/tool generation.

## Problem

`src/loom/examples/real_project_smoke.py` is valuable because it exercises a
real project with real tools, trace persistence, TUI streaming, and LLM
judgment. But it is a case-specific module. The next step is a reusable runner
that can execute arbitrary user tasks such as:

- audit a project
- modify code
- run a command-driven investigation
- inspect data files
- research and write a report
- perform a bounded shell workflow

The runner should compile a task request into Loom's existing primitives:

```text
TaskRequest
  -> TaskProfile
  -> PromptPack
  -> ToolResolution
  -> LoopBlueprint
  -> Context
  -> MinimalLoopDefinition
  -> runtime.run/run_with_plugins
```

The runner should reuse existing TUI, trace, tool resolver, LLM step function,
and evolution mechanisms. It should not fork the runtime.

## Non-Goals

- Do not make an unconstrained autonomous agent.
- Do not let the LLM generate the root system prompt directly.
- Do not let the LLM build arbitrary nested loop graphs at runtime.
- Do not replace `Context`, `MinimalLoopDefinition`, `Trace`, or the runtime
  plugin model.
- Do not make `real_project_smoke.py` the new abstraction boundary.
- Do not introduce a separate memory system disconnected from `StateLayer`,
  `KnowledgeLayer`, trace records, and artifacts.

## Recommended Approach

Use a policy-governed dynamic runner.

The stable execution kernel remains deterministic and explicit. Dynamic parts
are allowed only inside bounded surfaces:

- classify task profile
- generate a task brief
- select a loop blueprint from a finite set
- select tools through the catalog and resolver
- create a small number of ephemeral task-local helpers
- compact context into structured packs

This gives the LLM enough flexibility to adapt to tasks without giving it
control over the execution substrate.

## Package Layout

Add a new package:

```text
src/loom/tasks/
  __init__.py
  request.py
  profiles.py
  prompts.py
  blueprints.py
  context_manager.py
  runner.py
  cli.py
```

The package should expose a small public API:

```text
TaskRequest
TaskRunOptions
TaskRunResult
TaskProfile
LoopBlueprint
PromptCompiler
ContextManager
run_generic_task
```

The implementation should keep APIs plain dataclasses and functions. Avoid
global registries in the first version; inject provider, tool catalog, trace
store, and runtime options explicitly.

## Task Request Contract

`TaskRequest` is the user-facing input.

```python
@dataclass(frozen=True, slots=True)
class TaskRequest:
    objective: str
    workspace: Path | None = None
    profile: str = "auto"
    constraints: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    risk_level: str = "auto"
    metadata: Mapping[str, Any] | None = None
```

`TaskRunOptions` controls execution, not task meaning.

```python
@dataclass(frozen=True, slots=True)
class TaskRunOptions:
    tui: bool = False
    stream: bool = False
    trace_path: Path | None = None
    max_steps: int | None = None
    timeout_ms: int | None = None
    tool_budget: AffordanceBudget = AffordanceBudget()
```

Important distinction:

- `TaskRequest` describes what should be done.
- `TaskRunOptions` describes how Loom should execute and observe it.

This preserves the original explicitness principle.

## Task Profiles

Profiles are predefined task families. A profile supplies prompt fragments,
default tools, risk rules, context policy, and preferred loop blueprint.

First-version profiles:

```text
general
project_audit
coding
shell_workflow
research
data_inspection
```

`profile="auto"` uses a classifier to select one. The classifier may use an LLM
or deterministic heuristics, but its output is a structured decision:

```text
profile
risk_level
recommended_blueprint
required_capabilities
reasoning
confidence
```

This decision should be persisted into trace metadata so evolution can later
evaluate whether profile selection was useful.

## Prompt Design

The prompt should be compiled, not freely generated.

The compiled prompt has five layers:

```text
Stable Kernel Prompt
+ Profile Prompt
+ Generated Task Brief
+ Context Pack
+ Tool Contract
```

### Stable Kernel Prompt

This is owned by Loom and should not be generated by the LLM. It defines:

- obey the current task objective and constraints
- use tools for external facts and side effects
- never invent command results or file contents
- preserve traceability in final answers
- choose bounded next actions
- finish only when success criteria are met or a clear blocker is found
- produce structured decisions compatible with `create_llm_step_function`

The kernel should be small and stable. Evolution may propose changes to it, but
changes must go through proposal gating and review.

### Profile Prompt

The profile prompt is predefined. Example for `project_audit`:

```text
Inspect relevant project files and run targeted commands.
Prefer evidence over broad enumeration.
Report purpose, health, risks, and concrete improvement directions.
Do not modify source files unless the task explicitly asks for edits.
```

Profiles should remain compact. If a profile grows large, split it into smaller
rules or skills.

### Generated Task Brief

The LLM may generate a task brief from `TaskRequest`, but the brief is data, not
authority. It can include:

```text
interpreted_objective
success_criteria
known_constraints
initial_questions
likely_artifacts
```

The brief must not override the kernel prompt. It should be stored in
`Context.metadata` and trace metadata.

### Context Pack

The context manager builds a structured context projection before each LLM
round. It is not raw chat history.

```text
objective
current_plan
completed_work
open_work
verified_facts
recent_tool_results
artifact_refs
active_constraints
selected_tools
```

The LLM sees the pack; the full trace remains the source of truth.

### Tool Contract

The tool contract is produced from `ToolRef` values after catalog resolution
and step-level tool selection. It should not be hand-written in each task
module.

## Loop Structure

Loop structure should be dynamically selected from finite blueprints, not
generated freely.

First-version blueprints:

### direct

One LLM loop step, usually enough for simple tool-backed answers.

Use when:

- objective is small
- risk is low
- expected output is simple

### plan_execute_verify

The default for coding and project tasks.

```text
plan -> act/tool loop -> verify -> finish
```

This can still be implemented as a `MinimalLoopDefinition` using
`create_llm_step_function`; the blueprint controls prompt/profile/done policy,
not a separate runtime.

### explore_execute_verify

The default replacement for `real_project_smoke`.

```text
inspect workspace -> gather evidence -> execute requested checks -> verify -> report
```

This is the best first implementation target because it covers project audit,
smoke testing, and command-driven investigation.

### fork_reduce

For independent subtasks such as auditing multiple directories or comparing
several files.

Use existing `composition.fork` only when the split is explicit and bounded.
The first version can define the blueprint without implementing automatic
forking.

### supervisor_worker

For higher-risk or longer tasks. A supervisor loop assigns bounded child goals
and merges outputs using existing `project`, `emit_child_output`, and
`merge_child_output`.

This should not be in the first implementation unless needed.

## Loop Assembly Rules

`LoopAssembler` converts a selected blueprint into Loom runtime objects.

It should produce:

```text
MinimalLoopDefinition
Context
RuntimeRegistry
TraceStore | None
Run plugins
```

Rules:

- Blueprint selection happens before the run starts.
- The selected blueprint is fixed for the run.
- LLM may request a blueprint change, but that becomes a proposal, not an
  immediate mutation.
- Composition depth is capped at 2.
- `compose(composed(...))` style nesting is rejected.
- Every assembled run records profile, blueprint, tool resolution, context
  policy, and prompt template ids in trace metadata.

This keeps dynamic assembly compatible with evolution and debugging.

## Tool Model

The task runner should use the existing tool governance design:

- atomic tools are permanent
- composed tools are bounded and evaluated
- ephemeral tools are task-local and short-lived
- `AffordanceBudget` limits tool schema tokens and count
- `ToolResolver` resolves the catalog before step-level tool selection
- `ToolSelectionConfig` selects the current step subset

First-version built-in atomic tools:

```text
read_file
write_file
shell_execute
finish
```

Optional next tools:

```text
list_dir
search_files
read_many_files
apply_patch
```

`real_project_smoke.py` should eventually become either:

- an example that calls `run_generic_task`, or
- a thin profile-specific wrapper around `TaskRequest(profile="project_audit")`.

## Context Management

Context management is the most important part of making the runner general.

The system should distinguish:

```text
Trace
  append-only source of truth

StateLayer
  observations, decisions, pending work, scratch

KnowledgeLayer
  stable facts, heuristics, memories

ContextPack
  token-bounded projection for one LLM round

Artifacts
  large outputs and reports stored outside the prompt
```

### Compaction

Compaction should be structured, not a free-form summary.

When token pressure rises, convert:

```text
old observations -> EvidenceSummary
tool outputs -> VerifiedFacts
decisions -> TaskLedger
large stdout/files -> ArtifactRef
failed attempts -> FailureSummary
```

The compacted result should preserve:

- source trace ids
- event hashes where available
- command/file/tool provenance
- confidence or verification status

The original trace and artifacts remain unchanged.

### Refactory

Use "refactory" as a context restructuring operation, not code refactoring.

Refactory means reorganizing context when the current shape is no longer useful:

- split a broad objective into subgoals
- promote repeated observations into knowledge facts
- demote stale scratch data
- replace verbose history with a task ledger
- attach large outputs as artifact refs

Refactory should be triggered by policy:

```text
token estimate over threshold
tool output too large
too many observations
plan changed materially
verification failed and recovery needs a clean view
```

The result is a new `Context` with a new id, not mutation in place.

## Done Policy

The generic runner should avoid fixed call-count limits as the primary stop
condition. Stop conditions should combine:

- LLM calls `finish`
- required success criteria are satisfied
- verifier passes
- task is blocked with evidence
- runtime timeout or explicit budget is hit
- safety policy blocks further action

`max_steps` should remain available as a guardrail, but the task prompt should
not force a fixed number of tool calls. This matches the earlier decision that
the LLM may call multiple tools in one round.

## Trace and TUI

The generic runner should use the existing TUI plugin and trace stores.

Required trace metadata:

```text
task_request
profile
blueprint
prompt_template_ids
task_brief
tool_resolution
context_policy
workspace
risk_level
```

The TUI should not need a new interface. It should display the same event
stream:

```text
run.started
step.started
llm.requested
llm stream events
tool.started/completed
llm.completed
step.completed
run.completed
```

The task runner should add only task-specific metadata, not task-specific UI
logic.

## Evolution Integration

Evolution should operate on the generic runner through trace evidence.

Surfaces evolution may propose changes for:

```text
profile prompt
kernel prompt
task classifier rules
blueprint selection policy
tool resolver priority
context compaction thresholds
finish/verifier policy
skills or reusable task brief templates
```

Evolution must not directly alter live execution. It produces proposals that go
through gating and shadow evaluation.

The runner should make evolution easier by recording:

- which profile was selected
- which blueprint was selected
- which tools were pruned or exposed
- when compaction/refactory happened
- why done policy stopped

## Error Handling

The runner should return `Result` values and `LoomError` values consistently
with existing packages.

Important error categories:

```text
VALIDATION_FAILED
  invalid request, missing workspace, unsupported profile

TOOL_FAILED
  tool execution failed

LLM_FAILED
  provider call failed

LLM_PARSE_ERROR
  model output cannot be parsed

BUDGET_EXCEEDED / TOKEN_BUDGET_EXCEEDED
  runtime or prompt budget exceeded

LOOP_FAILED
  blueprint assembly or child loop failed

INTERNAL
  unexpected implementation error
```

Errors should preserve trace id and task metadata where possible.

## First Implementation Slice

The first implementation should be small:

```text
TaskRequest
TaskRunOptions
TaskProfile
PromptCompiler
ContextManager skeleton
run_generic_task()
CLI: python -m loom.tasks.run
```

Supported behavior:

- `profile=auto` with deterministic classifier for obvious cases
- `profile=project_audit`
- blueprint `explore_execute_verify`
- tools: `read_file`, `write_file`, `shell_execute`, `finish`
- trace persistence
- TUI support
- streaming when provider supports it

Example:

```bash
uv run python -m loom.tasks.run \
  "Audit this project and suggest improvements" \
  --workspace /Users/huanggui/workspace/yakDB \
  --profile auto \
  --tui \
  --trace-path .loom/debug/task.jsonl
```

Expected first-version output:

- final markdown report
- full trace JSONL
- TUI timeline
- raw task metadata in trace

## Migration Path for real_project_smoke.py

Do not delete `real_project_smoke.py` immediately.

Phase 1:

- keep it as a compatibility example
- add generic task runner separately
- test both paths

Phase 2:

- make `real_project_smoke.py --llm` call `run_generic_task`
- preserve deterministic smoke mode if still useful for regression tests

Phase 3:

- turn `real_project_smoke.py` into a small example wrapper or deprecate it
- keep its tests as generic runner integration tests

## Testing Strategy

Unit tests:

- parse `TaskRequest` and CLI args
- classify task profile
- compile prompt packs
- assemble context with correct layers
- resolve tools under `AffordanceBudget`
- compact/refactory context with provenance preserved

Integration tests:

- fake provider executes project audit with tool calls
- tool call list with multiple tool calls is handled
- assistant and tool messages are appended to LLM context
- trace includes task metadata
- TUI runner receives normal loop events
- `real_project_smoke` compatibility path still works

Live smoke:

- optional provider-backed run against a real workspace
- not part of default CI

## Open Decisions

1. Whether the first version should include `apply_patch` as an atomic tool.
   Recommendation: not by default for audit tasks; include for `coding`.

2. Whether `TaskClassifier` should use LLM in the first implementation.
   Recommendation: deterministic first, LLM classifier later.

3. Whether `ContextManager` should estimate tokens exactly or roughly.
   Recommendation: rough estimate first, exact provider-aware accounting later.

4. Whether `finish` should be a required tool for every task.
   Recommendation: yes for first version, because it gives a clear terminal
   event and structured final report.

## Acceptance Criteria

- A generic CLI can run a project audit task without using
  `real_project_smoke.py` internals.
- The runner emits normal Loom runtime/TUI events.
- The runner persists full traces.
- The runner uses `Context`, `MinimalLoopDefinition`, `ToolRef`,
  `ToolResolver`, and `create_llm_step_function` rather than custom runtime
  concepts.
- Prompt compilation separates kernel, profile, task brief, context pack, and
  tool contract.
- Loop blueprint selection is bounded and traceable.
- Context compaction/refactory produces a new context and preserves provenance.
- Tests cover profile selection, prompt compilation, tool resolution, trace
  metadata, and a fake-provider end-to-end task.

## Summary

The generic task runner should make Loom feel like a general task execution
system without losing Loom's original compactness and explicitness. The right
shape is not a fully dynamic agent that invents its own runtime. It is a small
compiler from task requests into Loom's existing context, loop, tool, trace,
TUI, and evolution primitives.

