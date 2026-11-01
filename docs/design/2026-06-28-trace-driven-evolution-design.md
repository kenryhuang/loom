# Trace Driven Evolution Design

## Purpose

This document designs the next evolution mechanism for Loom.

The goal is to use persisted traces to evaluate each loop step, identify which
parts of the context system caused weak outcomes, and propose bounded
improvements to prompts, tool calls, tool collections, skills, and context
construction.

The mechanism should make Loom more capable over time without letting the model
rewrite its own operating environment without evidence, replay, and rollback.

The first implementation target is an offline evaluator that reads persisted
JSONL trace records, asks an LLM to score each step, aggregates those scores
into evolution proposals, and stores the proposals for human or later shadow
evaluation. It does not automatically apply mutations.

## Design Principle

Use **LLM scoring with deterministic governance**.

The LLM is allowed to judge, explain, classify, and propose. It is not allowed
to directly publish changes to system prompts, tool schemas, tool catalogs,
skills, or context policies.

```text
trace records
  -> deterministic episode builder
  -> LLM step scorer
  -> deterministic run aggregator
  -> deterministic proposal gates
  -> shadow evaluation
  -> explicit accept or reject
```

This keeps subjective judgment where it is useful and keeps lifecycle control
where it is auditable.

## Existing Foundations

The design builds on current Loom boundaries:

- `JsonlTraceStore` persists both event records and completed `Trace` records.
- Runtime emits `run.started`, `step.started`, `llm.*`, `tool.*`,
  `action.*`, `observation.*`, `step.completed`, and `run.completed` events.
- LLM traces already include model, finish reason, token usage, streaming, tool
  resolution, and tool selection metadata.
- `DefaultEvolutionEvaluator` already provides a simple score comparison model.
- `loom.tools.evaluation` already detects repeated tool patterns.
- `ToolResolver` already applies a hard affordance budget before step-level
  tool selection.

The new evolve mechanism should not replace these pieces. It should add a
trace-to-proposal layer above them.

## Scope

### In Scope

- Score every step in a run from persisted trace events.
- Attribute weak outcomes to specific context system surfaces.
- Aggregate repeated issues across a run or trace window.
- Propose bounded mutations.
- Keep every proposal linked to trace evidence.
- Support shadow evaluation before accepting proposals.
- Preserve rollback and version history.

### Out Of Scope For The First Version

- Automatically applying mutations.
- Generating executable tool code.
- Changing runtime behavior inside an active run.
- Updating long-lived skills without review.
- Letting composed tools compose other composed tools.

## Trace Episode Model

Persisted records are too granular for scoring. The scorer should receive a
step episode, not raw JSONL lines.

```python
@dataclass(frozen=True, slots=True)
class StepEpisode:
    run_id: str
    trace_id: str
    loop_id: str
    step_number: int
    started_event: Mapping[str, Any]
    llm_requests: tuple[Mapping[str, Any], ...]
    llm_completions: tuple[Mapping[str, Any], ...]
    tool_events: tuple[Mapping[str, Any], ...]
    action_events: tuple[Mapping[str, Any], ...]
    observation_events: tuple[Mapping[str, Any], ...]
    completed_trace: Trace | None
    completed_event: Mapping[str, Any] | None
```

The `TraceEpisodeBuilder` groups records by `run_id` and `trace_id`.

It should preserve ordering, normalize payloads to plain JSON, and tolerate
partial traces. Partial episodes can still be scored as failed or incomplete
evidence, but they should be marked as incomplete.

## Step Score

Each step gets a structured score.

```python
@dataclass(frozen=True, slots=True)
class StepScore:
    run_id: str
    trace_id: str
    step_number: int
    overall: float
    dimensions: Mapping[str, float]
    attribution: Mapping[str, tuple[str, ...]]
    proposed_fixes: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    confidence: float
    evaluator_model: str
```

Recommended dimensions:

| Dimension | Meaning |
| --- | --- |
| `task_progress` | Did the step move the loop toward the goal? |
| `evidence_grounding` | Was the decision grounded in observations and tool output? |
| `tool_choice_quality` | Were the selected tools appropriate and minimal? |
| `tool_argument_quality` | Were tool arguments valid, specific, and efficient? |
| `context_relevance` | Did the visible context contain the useful facts and omit noise? |
| `prompt_following` | Did the output follow system and format constraints? |
| `cost_efficiency` | Were prompt tokens, tool calls, and retries proportional to value? |
| `failure_recovery` | Did the loop recover from parser, tool, or LLM failures? |

The key output is attribution. A low score is not useful unless Loom can tell
which surface should change.

Attribution categories:

- `system_prompt`
- `user_prompt`
- `tool_schema`
- `tool_description`
- `tool_collection`
- `tool_selection`
- `tool_call_policy`
- `skill_context`
- `history_context`
- `knowledge_context`
- `runtime_policy`

The scorer should return only JSON. Invalid JSON becomes a failed score record
and does not produce proposals.

## LLM Scoring Prompt

The scoring prompt should be stable and narrow.

The LLM receives:

- goal and identity summary
- step number and outcome
- system prompt and user prompt excerpts
- visible tool list and selected tool list
- LLM response or stream aggregate
- tool call inputs and outputs
- final action, observations, decision, and trace metadata
- token usage and timing

The LLM should not receive the entire run by default. Step scoring should stay
local. Run-level aggregation is deterministic.

The scoring prompt should ask:

1. What happened in this step?
2. Did it help the goal?
3. Which context surfaces helped or hurt?
4. What small bounded change would most improve future runs?
5. How confident is this judgment?

## Run Aggregation

The `RunEvolutionAggregator` combines step scores into signals.

It should not average everything into one number. It should find repeated,
trace-backed patterns:

- Same attribution category appears across multiple steps or runs.
- Same tool is selected but not called.
- Same tool is called with invalid or vague arguments.
- Tool selection fallback is frequent.
- Token cost increases without outcome improvement.
- LLM repeatedly violates output format.
- Context history is included but not used.
- Important observation appears in trace but not in final reasoning.
- Skill-like procedural instructions are repeated in prompts.

Aggregation should produce `EvolutionSignal` records:

```python
@dataclass(frozen=True, slots=True)
class EvolutionSignal:
    kind: str
    surface: str
    severity: float
    frequency: int
    trace_ids: tuple[str, ...]
    explanation: str
    confidence: float
```

Signals are evidence, not mutations.

## Proposal Types

The planner converts signals into candidate proposals.

```python
@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    id: str
    surface: str
    kind: str
    title: str
    rationale: str
    created_from_trace_ids: tuple[str, ...]
    expected_impact: Mapping[str, Any]
    risk: str
    reversible: bool
    ttl_runs: int | None
    patch: Mapping[str, Any]
```

Supported first-class proposal surfaces:

### System Prompt Proposal

Allowed changes:

- add one concise rule
- clarify output format
- remove redundant text
- move a high-priority instruction earlier
- add a failure-handling instruction

Disallowed changes:

- full prompt rewrite
- changing loop identity semantics without review
- increasing prompt size without token budget justification

### Tool Schema Proposal

Allowed changes:

- add a required property
- narrow a property description
- add enum values
- add validation hints
- clarify output schema
- improve error message semantics

Disallowed changes:

- changing executable tool behavior without a matching implementation change
- hiding required inputs from the schema
- making schemas more permissive to avoid validation failures

### Tool Collection Proposal

Allowed changes:

- prune low-value composed tools
- promote a candidate composed tool with evidence
- merge a repeated pattern into an existing atomic tool mode
- adjust resolver priority or TTL

Rules:

- Atomic tools are permanent.
- Composed tools cannot depend on composed tools.
- Ephemeral tools expire at run end.
- Promotion requires at least two evidence categories.
- Catalog size should converge, not expand.

### Skill Proposal

A skill proposal is appropriate when traces show repeated procedural knowledge
being injected into prompts or rediscovered by the model.

Allowed changes:

- propose a new skill document
- propose updating skill trigger language
- propose moving repeated instructions out of the prompt and into a skill

Disallowed changes:

- automatically installing or activating a skill
- loading all skills into every context
- treating skills as ordinary tools

### Context Policy Proposal

Allowed changes:

- reduce or increase history window
- summarize repeated observations
- promote facts into knowledge
- demote stale facts
- adjust token budget allocation among tools, history, and knowledge

Disallowed changes:

- keeping more context without an explicit budget tradeoff
- deleting evidence needed for auditability

## Governance Gates

Every proposal must pass deterministic gates before it can move to shadow
evaluation.

### Evidence Gate

Required:

- at least one trace id
- at least one concrete event or trace payload reference
- scorer confidence above configured threshold
- repeated signal for durable changes

Single-step issues can create local suggestions, but not durable mutations.

### Budget Gate

Required:

- prompt and tool schema token impact estimate
- no violation of `AffordanceBudget`
- no durable increase in visible tools unless another tool is merged or pruned

### Risk Gate

Allowed first version risks:

- `low`: prompt wording, schema description, resolver priority
- `medium`: skill proposal, composed tool promotion proposal

Blocked first version risks:

- executable code generation
- runtime policy mutation
- automatic publication

### Reversibility Gate

Every accepted proposal must have:

- base version
- candidate version
- rollback metadata
- trace evidence

If it cannot be rolled back, it cannot be automatically accepted.

## Shadow Evaluation

Shadow evaluation compares current and candidate behavior against historical
contexts or curated benchmark cases.

```text
baseline loop/context policy
  -> replay contexts
  -> collect traces and scores

candidate loop/context policy
  -> replay same contexts
  -> collect traces and scores

compare
  -> accept if improvement exceeds threshold and no guardrail regresses
```

Comparison should consider:

- outcome rate
- step score delta
- dimension-specific improvements
- token usage
- tool call count
- parser failures
- tool failures
- output format adherence
- regression on previously passing cases

LLM scoring can be part of shadow evaluation, but the final accept/reject
decision is deterministic.

## Storage

Evolution artifacts should be stored separately from raw trace records.

Recommended JSONL streams:

```text
.loom/evolution/step-scores.jsonl
.loom/evolution/signals.jsonl
.loom/evolution/proposals.jsonl
.loom/evolution/shadow-evaluations.jsonl
```

Trace records remain immutable evidence. Evolution records point back to trace
ids and event hashes.

## CLI Shape

The first CLI should be offline.

```bash
uv run python -m loom.evolution.analyze \
  --trace-path .loom/traces/yakdb-smoke.jsonl \
  --out-dir .loom/evolution \
  --llm
```

Useful flags:

- `--surface prompt|tools|skills|context|all`
- `--min-confidence 0.7`
- `--max-proposals 3`
- `--dry-run`
- `--json`

The command should print a compact report and write structured artifacts.

## TUI Integration

The evolve analyzer can later reuse the TUI event model.

Suggested event types:

- `evolution.episode_built`
- `evolution.step_score_requested`
- `evolution.step_score_completed`
- `evolution.signal_detected`
- `evolution.proposal_created`
- `evolution.proposal_rejected`
- `evolution.shadow_started`
- `evolution.shadow_completed`

These should be separate from loop runtime events. Evolution observes loop
traces; it is not itself part of the original run.

## Failure Handling

If the scoring LLM fails:

- persist a failed score record
- continue with other episodes
- do not create proposals from failed scores

If score JSON is invalid:

- persist raw output and parse error
- mark score as unusable

If trace records are incomplete:

- score with `incomplete_evidence=true`
- block durable proposals from that episode unless corroborated elsewhere

If proposals conflict:

- prefer lower-risk proposal
- prefer proposal with lower token cost
- prefer merge or prune over addition
- limit each evolve round to one mutation surface

## Versioning And Auditability

Every durable proposal should include:

- base artifact id and version
- candidate artifact id and version
- created timestamp
- scorer model
- trace ids
- event hashes
- before and after token estimates
- rollback metadata

Trace replay must be able to explain why a mutation existed.

## Implementation Phases

### Phase 1: Offline Scoring

- Build `StepEpisode` records from JSONL trace events.
- Add an LLM scorer that returns `StepScore` JSON.
- Persist score records.
- Produce a human-readable report.
- No mutation proposals are applied.

### Phase 2: Signal Aggregation

- Aggregate repeated score attributions.
- Produce `EvolutionSignal` records.
- Add deterministic gates for evidence, budget, risk, and reversibility.

### Phase 3: Proposal Generation

- Generate prompt, tool schema, tool collection, skill, and context proposals.
- Persist proposals with trace evidence.
- Limit output to a small ranked set.

### Phase 4: Shadow Evaluation

- Replay baseline and candidate configurations.
- Compare score, cost, and outcome deltas.
- Mark proposals accepted or rejected.

### Phase 5: Controlled Application

- Add explicit apply command.
- Version changed artifacts.
- Support rollback.
- Keep automatic application disabled by default.

## Testing Strategy

Unit tests:

- episode builder groups raw records correctly
- incomplete episodes are marked
- score parser accepts valid JSON and rejects invalid JSON
- aggregator detects repeated attribution patterns
- gates reject unsupported or over-budget proposals

Integration tests:

- run `real_project_smoke` with persisted trace
- analyze the trace with a fake scoring provider
- verify step scores, signals, and proposals are written
- verify no proposal is applied automatically

Regression tests:

- malformed JSONL line handling
- missing `step.completed`
- missing `llm.completed`
- tool failure event attribution
- proposal limit enforcement

## Non Goals

- This system is not a reinforcement learning loop.
- This system does not fine-tune models.
- This system does not mutate code by itself.
- This system does not guarantee every run improves.
- This system does not remove the need for benchmark cases.

## Summary

Trace-driven evolution should make Loom better by turning run evidence into
small, reversible, scored proposals.

The model can judge and suggest. The system governs, budgets, validates,
evaluates, and versions. That split is what keeps evolution useful without
turning context construction into uncontrolled growth.
