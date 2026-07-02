# Trace Evaluation and Evolution Platform Design

This document designs an industrial-grade trace evaluation and evolution
platform for Loom. It extends the earlier trace-driven evolution design in
`docs/superpowers/specs/2026-06-28-trace-driven-evolution-design.md` and keeps
the original Loom Python principles from
`docs/superpowers/specs/2026-06-05-loom-py-design.md`: explicit typed
contracts, trace-first execution, bounded composition, deterministic governance,
and reversible evolution.

The selected operating mode is:

```text
Auto-apply low-risk changes only.
High-risk changes require human review.
All applied changes must be evidence-backed, reversible, monitored, and
rollbackable.
```

## Current State

Loom already has the first useful pieces:

- `JsonlTraceStore` persists runtime events and completed `Trace` records.
- `loom.evolution.episodes` builds `StepEpisode` objects from JSONL records.
- `LlmStepScorer` scores each step with a fixed scoring prompt.
- `aggregate_step_scores` groups repeated attribution by surface.
- `generate_evolution_proposals` creates bounded proposal objects.
- `gate_proposal` enforces simple confidence, risk, and reversibility gates.
- `write_evolution_artifacts` persists scores, signals, proposals, and report.
- `mutations.py` contains early mutation and shadow evaluation primitives.

This is a good prototype, but it is not yet industrial-grade:

- Evaluation is step-level only.
- Objective metrics and LLM judgment are mixed together.
- The scoring rubric is embedded in a prompt instead of being versioned.
- Proposals are artifacts, not managed lifecycle objects.
- Shadow evaluation is not connected to proposals and rollout.
- There is no active override layer, monitoring loop, or rollback workflow.
- Incomplete episodes currently block analysis instead of becoming evaluable
  failure evidence.

## Goals

- Convert persisted traces into a normalized, queryable episode graph.
- Evaluate runs through deterministic metrics and calibrated LLM judges.
- Attribute failures and improvement opportunities to precise mutable surfaces.
- Generate evidence-backed proposals with explicit risk, TTL, and rollback data.
- Auto-apply only low-risk reversible changes after gates and shadow evaluation.
- Monitor applied changes and rollback automatically on regression.
- Keep every score, finding, gate decision, patch, and rollback tied to trace
  evidence hashes.

## Non-Goals

- Do not let the LLM directly mutate source code.
- Do not auto-apply new tool code, runtime structure changes, security policy
  changes, shell policy changes, or model provider changes.
- Do not rewrite large prompts automatically.
- Do not change runtime behavior inside an active run.
- Do not require all trace episodes to be complete before evaluation can run.
- Do not replace existing `Trace`, `Context`, `MinimalLoopDefinition`, or
  runtime plugin contracts.

## Package Boundary

Split evaluation from evolution:

```text
loom.evaluation
  records.py        # TraceRecord -> NormalizedEvent
  episodes.py       # NormalizedEvent -> EpisodeGraph
  metrics.py        # deterministic metric engine
  rubrics.py        # rubric definitions and versions
  judge.py          # LLM judge execution and parsing
  calibration.py    # golden traces and contradiction checks
  diagnosis.py      # metrics + judge findings -> diagnosis
  artifacts.py      # metrics, scores, findings, reports
  cli.py            # python -m loom.evaluation.analyze

loom.evolution
  proposals.py      # diagnosis -> proposal candidates
  registry.py       # proposal lifecycle registry
  gates.py          # deterministic policy gates
  shadow.py         # replay and shadow evaluation
  apply.py          # active-overrides writer
  monitor.py        # post-apply monitoring
  rollback.py       # rollback and expiry
  artifacts.py      # evolution reports
  cli.py            # python -m loom.evolution.run
```

Existing `loom.evolution.analyze`, `episodes`, `scoring`, and `proposals`
should remain as compatibility shims during migration.

## End-to-End Flow

```text
runs/*.jsonl
  -> TraceIngestor
  -> NormalizedEvent[]
  -> EpisodeGraph
  -> MetricEngine
  -> JudgeEngine
  -> CalibrationChecks
  -> Diagnosis
  -> ProposalGenerator
  -> ProposalRegistry
  -> RiskGate
  -> ShadowEvaluator
  -> AutoApplyEngine
  -> ActiveOverrides
  -> Monitor
  -> Rollback / Expire / Promote
```

## Trace Normalization

Persisted JSONL contains event records and trace records with slightly different
shapes. Normalize them before building episodes.

```python
@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    record_id: str
    event_type: str
    run_id: str | None
    loop_id: str | None
    trace_id: str | None
    step_number: int | None
    llm_call_id: str | None
    tool_call_id: str | None
    tool_id: str | None
    at: str | None
    payload: Mapping[str, Any]
    hash: str | None
```

The normalizer should:

- Preserve the raw payload.
- Normalize identifiers from both wrapper fields and nested payloads.
- Compute a stable `record_id` when a record does not provide one.
- Validate that event type, hash, and payload are JSON-compatible.
- Classify malformed lines as corrupt records.

Corrupt JSONL blocks evaluation for that input file. Structurally partial or
orphaned events do not block evaluation.

## Episode Graph

The current `StepEpisode` is useful but too coarse. Add a graph with run, step,
LLM round, tool call, and decision episodes.

```text
RunEpisode
  StepEpisode
    LlmRoundEpisode
    ToolCallEpisode
    DecisionEpisode
```

Each episode should contain:

- Stable identity fields.
- Ordered normalized events.
- Evidence event hashes.
- Status: `complete`, `partial`, `failed`, `orphaned`, or `corrupt`.
- Links to parent and child episode ids.

Recommended contracts:

```python
@dataclass(frozen=True, slots=True)
class EpisodeRef:
    kind: str
    id: str

@dataclass(frozen=True, slots=True)
class RunEpisode:
    id: str
    run_id: str
    loop_id: str | None
    steps: tuple[EpisodeRef, ...]
    started_event: NormalizedEvent | None
    completed_event: NormalizedEvent | None
    status: str
    event_hashes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class LlmRoundEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    llm_call_id: str
    requested_event: NormalizedEvent | None
    stream_events: tuple[NormalizedEvent, ...]
    completed_event: NormalizedEvent | None
    failed_event: NormalizedEvent | None
    status: str
    event_hashes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ToolCallEpisode:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    tool_call_id: str | None
    tool_id: str
    started_event: NormalizedEvent | None
    completed_event: NormalizedEvent | None
    failed_event: NormalizedEvent | None
    status: str
    event_hashes: tuple[str, ...]
```

Keep a compatibility adapter that can build the existing `StepEpisode` from the
new episode graph.

## Deterministic Metric Engine

The metric engine answers "what happened" without using an LLM.

```python
@dataclass(frozen=True, slots=True)
class MetricResult:
    id: str
    scope: str
    subject_id: str
    name: str
    value: JsonValue
    unit: str | None
    severity: str
    evidence_event_hashes: tuple[str, ...]
```

First metric set:

- `trace.completeness`
- `episode.partial_count`
- `episode.orphaned_count`
- `llm.request_count`
- `llm.completion_count`
- `llm.failed_count`
- `llm.stream_delta_count`
- `llm.tool_call_count`
- `llm.parse_failure_count`
- `tool.call_count`
- `tool.success_rate`
- `tool.failure_count`
- `tool.argument_validation_failure_count`
- `tool.result_bytes`
- `tool.result_truncated`
- `context.prompt_tokens`
- `context.message_count`
- `context.tool_schema_tokens`
- `cost.total_tokens`
- `cost.tokens_per_success`
- `run.finish_called`
- `run.final_report_present`
- `run.duration_ms`

Metric results become first-class evidence for diagnosis and proposal gates.

## LLM Judge Engine

The judge answers "what does this mean" and "what should change". It receives a
bounded evidence pack, not raw trace JSONL.

Evidence pack:

```text
episode summary
objective and success criteria
selected normalized events
deterministic metrics
relevant prompt and tool schema excerpts
final output or decision output
```

Output:

```python
@dataclass(frozen=True, slots=True)
class JudgeScore:
    id: str
    scope: str
    subject_id: str
    rubric_id: str
    rubric_version: str
    evaluator_model: str
    overall: float
    dimensions: Mapping[str, float]
    findings: tuple[JudgeFinding, ...]
    confidence: float
    evidence_event_hashes: tuple[str, ...]
    token_usage: TokenUsage

@dataclass(frozen=True, slots=True)
class JudgeFinding:
    id: str
    surface: str
    problem: str
    recommendation: str
    severity: float
    confidence: float
    evidence_event_hashes: tuple[str, ...]
```

Recommended dimensions:

- `task_progress`
- `evidence_grounding`
- `tool_choice_quality`
- `tool_argument_quality`
- `context_relevance`
- `prompt_following`
- `cost_efficiency`
- `failure_recovery`

## Rubric Versioning

Rubrics are versioned data, not only prompt text.

```python
@dataclass(frozen=True, slots=True)
class Rubric:
    id: str
    version: str
    scope: str
    dimensions: tuple[str, ...]
    instructions: str
    output_schema: Mapping[str, Any]
```

Every `JudgeScore` records `rubric_id` and `rubric_version`. Historical scores
from different rubric versions should not be blindly averaged.

## Calibration and Contradictions

Minimum calibration:

- Golden traces with expected score ranges and expected findings.
- Judge parser tests for schema variants and invalid responses.
- Finding evidence checks: findings without event hashes are downgraded or
  rejected.
- Deterministic contradiction checks.

Examples:

- Metric says no tool failed, judge claims a tool failed.
- Metric says `finish_called=false`, judge says the run finished correctly.
- Metric says prompt token count increased 50 percent, proposal claims no cost
  impact.

Contradictions block auto-apply and may still appear in reports.

## Diagnosis

Diagnosis combines metrics and judge findings into stable signals.

```python
@dataclass(frozen=True, slots=True)
class DiagnosisSignal:
    id: str
    scope: str
    surface: str
    kind: str
    severity: float
    frequency: int
    confidence: float
    metric_refs: tuple[str, ...]
    judge_finding_refs: tuple[str, ...]
    trace_ids: tuple[str, ...]
    evidence_event_hashes: tuple[str, ...]
    explanation: str
```

The aggregator should detect:

- Repeated low scores on the same surface.
- Repeated tool argument validation failures.
- Tool selection fallback frequency.
- Token growth without quality gain.
- Tool outputs not used in later LLM rounds.
- Final answers not grounded in observed evidence.
- Frequent parser failures.
- Repeated context policy misses.

Signals are evidence, not mutations.

## Proposal Registry

Proposals are managed lifecycle objects.

```python
@dataclass(frozen=True, slots=True)
class ManagedProposal:
    id: str
    status: str
    surface: str
    kind: str
    patch: Mapping[str, Any]
    risk: str
    reversible: bool
    ttl_runs: int
    created_at: str
    created_from_trace_ids: tuple[str, ...]
    evidence_event_hashes: tuple[str, ...]
    metric_refs: tuple[str, ...]
    judge_finding_refs: tuple[str, ...]
    gate_decisions: tuple[str, ...]
    shadow_result_id: str | None
    applied_version: str | None
```

Lifecycle:

```text
proposed
  -> gated
  -> shadowed
  -> accepted
  -> applied
  -> monitoring
  -> expired

rejected
needs_review
rolled_back
```

## Patch Model

Patches must be structured and rollbackable.

```python
@dataclass(frozen=True, slots=True)
class EvolutionPatch:
    operation: str
    target: str
    value: JsonValue
    rollback: Mapping[str, Any]
    budget_impact: Mapping[str, Any]
```

Example:

```json
{
  "operation": "append_prompt_rule",
  "target": "profile.project_audit.rules",
  "value": "Before finalizing, cite at least two observed files or command outputs.",
  "rollback": {
    "operation": "remove_prompt_rule",
    "target": "profile.project_audit.rules",
    "value_hash": "..."
  },
  "budget_impact": {
    "estimated_tokens": 18
  }
}
```

## Risk Gate

Auto-apply requires all of:

- `risk == "low"`
- `reversible == true`
- `ttl_runs > 0`
- patch surface is in the auto-apply allowlist
- evidence frequency meets threshold
- judge confidence meets threshold
- no deterministic contradiction
- shadow evaluation passes
- patch size is within budget
- estimated token/cost impact is within budget

Initial auto-apply allowlist:

```text
prompt.kernel.add_rule
prompt.profile.add_rule
tool.description.clarify
tool.schema.description_clarify
tool_resolver.priority_hint
context.compaction_hint
```

Auto-apply blocklist:

```text
runtime.loop_structure
tool.code.create
tool.code.modify
tool.delete
shell_policy.relax
security_policy.modify
large_prompt_rewrite
model_provider_change
```

Blocked proposals may still enter `needs_review`.

## Shadow Evaluation

Before auto-apply, evaluate baseline and candidate versions.

```text
baseline version + replay contexts
candidate version + replay contexts
compare metrics and judge scores
```

Pass criteria:

- candidate score improves by at least `min_delta`
- no critical metric regression
- cost increase is within budget
- failure rate does not worsen
- judge does not introduce new high-risk finding

Not all traces are safely replayable. First version should use replay-safe
contexts, synthetic eval fixtures, and side-effect-free tool runs. Real shell
side effects are not replayed by default.

## Active Overrides

Auto-apply should not modify Python source files. It writes versioned override
state.

```text
.loom/evolution/
  proposals.jsonl
  gate-decisions.jsonl
  shadow-results.jsonl
  applied-patches.jsonl
  active-overrides.json
  monitor-results.jsonl
  report.md
```

`active-overrides.json` is read during task/context compilation. It may inject:

- prompt rules
- profile rules
- tool description clarifications
- tool schema description clarifications
- resolver priority hints
- context compaction hints

Rollback removes or disables the override entry.

## Monitoring, TTL, and Rollback

Applied patches enter `monitoring`.

Monitor matching future runs until TTL expires. Compare against the baseline
window and shadow expectations.

Rollback triggers:

- quality score regression
- parse/tool failure increase
- cost over budget
- contradiction introduced
- TTL expires with no measurable benefit
- patch target disappears or becomes incompatible

If monitoring shows durable improvement, a patch may be promoted to a durable
configuration entry after human review.

## Artifacts

Evaluation outputs:

```text
.loom/evaluation/
  normalized-events.jsonl
  episodes.jsonl
  metrics.jsonl
  judge-scores.jsonl
  findings.jsonl
  contradictions.jsonl
  diagnosis-signals.jsonl
  report.md
```

Evolution outputs:

```text
.loom/evolution/
  proposals.jsonl
  gate-decisions.jsonl
  shadow-results.jsonl
  applied-patches.jsonl
  active-overrides.json
  monitor-results.jsonl
  report.md
```

Reports are for humans. JSONL artifacts are the source of truth.

## CLI

Evaluation:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/loom-task-xxx.jsonl \
  --out-dir .loom/evaluation
```

Evolution:

```bash
uv run python -m loom.evolution.run \
  --evaluation-dir .loom/evaluation \
  --registry-dir .loom/evolution \
  --auto-apply-low-risk
```

Combined convenience:

```bash
uv run python -m loom.evolution.run \
  --trace-path runs/loom-task-xxx.jsonl \
  --auto-apply-low-risk \
  --tui
```

## Error Handling

- Corrupt JSONL: fail evaluation for that input file.
- Partial episode: score as partial evidence and emit failure findings.
- Orphan event: record as orphaned evidence.
- Judge provider failure: persist failed score and skip proposal generation for
  affected episode.
- Judge parse failure: persist failed score and skip proposal generation for
  affected episode.
- Contradiction: block auto-apply.
- Gate reject: persist proposal as `rejected`.
- Shadow failure: persist proposal as `needs_review`.
- Apply failure: leave `active-overrides.json` unchanged.
- Monitor regression: rollback automatically.

## Testing Strategy

Unit tests:

- event normalization
- episode graph builder
- metric calculators
- judge response parser
- rubric validation
- gate policies
- patch rollback payloads

Contract tests:

- judge output schema variants
- malformed judge JSON
- rubric version compatibility
- active override reader contract

Golden trace tests:

- fixed trace input
- expected metrics
- expected episode graph status
- expected judge findings with fake provider
- expected proposal and gate decision

End-to-end tests:

- trace -> evaluation artifacts
- evaluation -> proposal registry
- low-risk proposal -> shadow -> active override
- monitoring regression -> rollback
- high-risk proposal -> needs_review

## Migration Plan

Phase 1: Evaluation foundation

- Add `loom.evaluation.records`.
- Add `NormalizedEvent`.
- Add `EpisodeGraph` while preserving current `StepEpisode`.
- Add deterministic metrics and artifacts.

Phase 2: Judge upgrade

- Add versioned rubrics.
- Add `JudgeScore` and `JudgeFinding`.
- Add calibration and contradiction checks.
- Keep old `LlmStepScorer` as compatibility wrapper.

Phase 3: Diagnosis and proposal registry

- Add `DiagnosisSignal`.
- Add `ManagedProposal`.
- Add proposal lifecycle registry.
- Preserve old proposal artifact format as an export.

Phase 4: Gates and shadow evaluation

- Add deterministic gates.
- Connect proposals to shadow evaluation.
- Introduce replay-safe fixture contexts.

Phase 5: Active overrides and auto-apply

- Add `active-overrides.json`.
- Add low-risk auto-apply allowlist.
- Wire overrides into task/context compilation.

Phase 6: Monitor and rollback

- Monitor future matching traces.
- Roll back on regression or TTL expiry.
- Produce durable promotion recommendations for human review.

## Industrial Readiness Criteria

The platform is industrial-grade when:

- Every score and finding cites trace evidence hashes.
- Every proposal cites metrics and judge findings.
- Every auto-applied patch is reversible and TTL-bound.
- Every patch has a shadow result before application.
- Active changes are isolated in override state, not source edits.
- Rollback is automatic on measurable regression.
- Rubric versions are persisted and test-covered.
- Golden traces prevent judge prompt drift.
- High-risk surfaces cannot auto-apply.

