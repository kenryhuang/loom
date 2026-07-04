# Evaluation to Evolution Bundle Contract Design

This document defines the structured contract between Loom agent runs,
evaluation, and evolution.

It refines the direction from:

- `docs/superpowers/specs/2026-06-05-loom-py-design.md`
- `docs/superpowers/specs/2026-07-02-trace-evaluation-evolution-platform-design.md`
- `docs/superpowers/specs/2026-07-03-trace-analysis-kernel-redesign.md`

The key boundary is:

```text
loom.agent produces trace evidence.
loom.evaluation consumes traces and produces structured evaluation bundles.
loom.evolution consumes evaluation bundles, resolves trace evidence when needed,
and produces improvement proposals.
```

Evaluation explains what happened. Evolution decides what should change.

## Current State

`loom.evaluation` now writes several useful artifacts:

```text
episodes.jsonl
metrics.jsonl
step-assessments.jsonl
round-judge-assessments.jsonl
judge-assessments.jsonl
findings.jsonl
report.md
```

These artifacts are valuable, but the cross-stage contract is still implicit.
`loom.evolution.analyze` still reads raw trace JSONL directly, rebuilds a
separate `StepEpisode`, and uses `LlmStepScorer` to score the step again. That
duplicates evaluation work and makes evolution depend on a weaker, coarser
trace view.

The new contract should make evaluation the only scoring and diagnosis stage.
Evolution should consume structured evaluation output and only consult raw trace
records to resolve evidence references.

## Goals

- Define a stable, versioned `EvaluationBundle` as the handoff from evaluation
  to evolution.
- Normalize deterministic, round-level judge, and step-level judge findings into
  one `EvaluationFinding` model.
- Let evolution generate signals and proposals from normalized findings instead
  of raw judge prose or raw trace dumps.
- Preserve raw trace access as an evidence resolver, not as a second analysis
  source.
- Keep current JSONL artifacts useful for debugging while adding a single
  machine-readable bundle entrypoint.
- Keep the migration incremental and compatible with the current CLI and tests.

## Non-Goals

- Do not remove current `metrics.jsonl`, `round-judge-assessments.jsonl`,
  `judge-assessments.jsonl`, or `report.md`.
- Do not make evolution mutate prompts, tools, or skills in this slice.
- Do not make evolution re-run LLM judges over raw trace by default.
- Do not require raw trace payloads to be embedded in the evaluation bundle.
- Do not design a proposal application or shadow evaluation system here.

## Operating Model

The full flow should be:

```text
Agent Run
  -> TraceBundle
  -> EvaluationBundle
  -> EvolutionBundle
```

### TraceBundle

`TraceBundle` is the persisted evidence from an agent run. In the current system
this is usually a trace JSONL file under `runs/`.

The trace bundle remains the source of truth for raw events, but downstream
systems should not repeatedly reinterpret it. Once evaluation has created an
evaluation bundle, evolution should treat the trace as an evidence store.

### EvaluationBundle

`EvaluationBundle` is the structured diagnosis layer. It contains:

- source trace metadata
- deterministic metrics
- deterministic step assessments
- round-level judge assessments
- step-level judge assessments
- normalized findings
- compact evidence references
- artifact paths and content digests

This is the main input to evolution.

### EvolutionBundle

`EvolutionBundle` is the improvement planning layer. It contains:

- source evaluation bundle metadata
- generated evolution signals
- proposal candidates
- gate decisions
- report paths

This design focuses on the input contract that lets evolution consume
evaluation results. Proposal application remains a later phase.

## Evaluation Bundle File Layout

Evaluation should continue to write the existing artifacts and add one manifest
entrypoint:

```text
.loom/evaluation/<run-name>/
  evaluation-bundle.json
  metrics.jsonl
  step-assessments.jsonl
  round-judge-assessments.jsonl
  judge-assessments.jsonl
  findings.jsonl
  evidence-index.jsonl
  report.md
```

`evaluation-bundle.json` should be the only file evolution needs to receive on
the command line.

Example:

```json
{
  "schema_version": "loom.evaluation.bundle.v1",
  "bundle_id": "eval_20260704_171234_smoke_glm",
  "created_at": "2026-07-04T17:12:34Z",
  "source_trace": {
    "path": "runs/smoke-glm-20260703-214154-774875.jsonl",
    "kind": "jsonl",
    "sha256": "..."
  },
  "summary": {
    "runs": 1,
    "steps": 1,
    "llm_rounds": 14,
    "tool_calls": 13,
    "metrics": 9,
    "step_assessments": 1,
    "round_judge_assessments": 14,
    "step_judge_assessments": 1,
    "findings": 24
  },
  "artifacts": {
    "metrics": "metrics.jsonl",
    "step_assessments": "step-assessments.jsonl",
    "round_judges": "round-judge-assessments.jsonl",
    "step_judges": "judge-assessments.jsonl",
    "findings": "findings.jsonl",
    "evidence_index": "evidence-index.jsonl",
    "report": "report.md"
  }
}
```

The manifest should store relative paths so the bundle directory can be moved as
a unit.

## Core Schemas

### EvaluationBundle

```python
@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    schema_version: str
    bundle_id: str
    created_at: str
    source_trace: TraceSource
    summary: EvaluationSummary
    artifacts: EvaluationArtifactRefs
```

The in-memory loader may hydrate linked artifacts into:

```python
@dataclass(frozen=True, slots=True)
class LoadedEvaluationBundle:
    manifest: EvaluationBundle
    metrics: tuple[MetricResult, ...]
    step_assessments: tuple[StepAssessment, ...]
    round_judge_assessments: tuple[RoundJudgeAssessment, ...]
    step_judge_assessments: tuple[StepJudgeAssessment, ...]
    findings: tuple[EvaluationFinding, ...]
    evidence_index: tuple[EvidenceRecord, ...]
```

The manifest is the stable disk contract. `LoadedEvaluationBundle` is a runtime
convenience object.

### TraceSource

```python
@dataclass(frozen=True, slots=True)
class TraceSource:
    path: str
    kind: str
    sha256: str | None
```

`path` may be relative to the evaluation bundle directory or absolute. When
possible, evaluation should write a content digest so evolution can detect
stale or mismatched trace evidence.

### EvaluationFinding

All findings should be normalized before evolution consumes them.

```python
@dataclass(frozen=True, slots=True)
class EvaluationFinding:
    id: str
    source: str              # rule, round_judge, step_judge
    severity: str            # info, warning, error
    category: str
    dimension: str
    surface: str
    message: str
    recommendation: str
    confidence: float
    impact_score: float
    frequency_key: str
    step_ref: StepRef | None
    round_ref: RoundRef | None
    evidence_refs: tuple[EvidenceRef, ...]
```

`surface` is the most important field for evolution. It should name the mutable
part of the system most likely responsible for the finding.

Recommended surfaces:

```text
system_prompt
user_prompt
tool_schema
tool_description
tool_collection
tool_call_parser
loop_control
context_policy
skill_context
observability
cost_policy
unknown
```

`frequency_key` should group repeated issues without requiring identical prose.
It should be deterministic and based on normalized fields:

```text
<surface>:<category>:<dimension>
```

Examples:

```text
tool_call_parser:tool_call_missing:tool_selection
context_policy:high_token_usage:efficiency
loop_control:no_forward_progress:round_progress
```

### EvidenceRecord

`EvaluationFinding` should contain compact `EvidenceRef` values. A separate
evidence index can provide slightly richer excerpts without embedding raw trace
payloads in every finding.

```python
@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    ref_id: str
    event_hash: str | None
    event_type: str
    subject_id: str | None
    field_path: str | None
    excerpt: str | None
    source_trace_path: str
```

Evolution can use `event_hash` and `field_path` to resolve the full raw event
from the source trace when a proposal needs deeper evidence.

## Finding Normalization

Evaluation currently has three kinds of source records:

- deterministic `Finding` from `step-assessments.jsonl`
- `JudgeFinding` inside `round-judge-assessments.jsonl`
- `JudgeFinding` inside `judge-assessments.jsonl`

The bundle writer should normalize these into `EvaluationFinding` records.

### Deterministic Finding Mapping

Example:

```text
category=high_token_usage
dimension=efficiency
affected_surface=context_policy
```

Maps to:

```text
source=rule
surface=context_policy
impact_score=derived from severity and metric value
frequency_key=context_policy:high_token_usage:efficiency
```

### Round Judge Finding Mapping

Round-level judge findings are usually local to one LLM round. They should keep
`round_ref` so evolution can distinguish repeated round behavior from a whole
step failure.

Examples:

```text
tool_call_count is 0 but the response describes an action
```

Maps to:

```text
source=round_judge
surface=tool_call_parser
category=tool_call_missing
dimension=tool_selection
```

If the judge cannot name a surface, normalization should infer one from the
dimension:

```text
tool_selection -> tool_collection
tool_arguments -> tool_schema
tool_result_handling -> loop_control
evidence_grounding -> observability
efficiency -> cost_policy
instruction_following -> system_prompt
context_quality -> context_policy
```

### Step Judge Finding Mapping

Step-level judge findings summarize a full runtime step. They should keep
`step_ref` and may aggregate evidence from lower-level round findings.

Step findings are best used for broad surfaces:

```text
system_prompt
context_policy
loop_control
observability
```

They should not overwrite round-level evidence. If a step judge and multiple
round judges describe the same issue, evolution should aggregate them through
`frequency_key`.

## Evolution Consumption

Evolution should add a new input path:

```bash
uv run python -m loom.evolution.analyze \
  --evaluation-bundle .loom/evaluation/smoke-glm/evaluation-bundle.json \
  --out-dir .loom/evolution/smoke-glm
```

`--trace-path` should remain as a legacy path during migration. If both are
provided, `--evaluation-bundle` should win.

The new pipeline should be:

```text
evaluation-bundle.json
  -> load_evaluation_bundle()
  -> normalize or read EvaluationFinding[]
  -> aggregate_findings()
  -> EvolutionSignal[]
  -> generate_evolution_proposals()
  -> EvolutionBundle artifacts
```

The old pipeline remains available but should be labeled legacy:

```text
raw trace
  -> StepEpisode
  -> LlmStepScorer
  -> StepScore
  -> EvolutionSignal
```

### EvolutionSignal from EvaluationFinding

```python
@dataclass(frozen=True, slots=True)
class EvolutionSignal:
    id: str
    kind: str
    surface: str
    severity: float
    frequency: int
    confidence: float
    finding_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    explanation: str
```

Signal severity should combine:

- finding impact score
- finding severity
- failed or warning dimensions
- frequency across runs, steps, and rounds
- confidence from judge or rule source

Single high-impact findings should still be allowed to produce a signal. The
current `min_signal_frequency` gate can remain configurable, but evolution
should not require repeated failures for severe single-run structural issues
such as no tool calls being executed.

### Proposal Generation

The current `EvolutionProposal` shape is a good starting point. Proposal
generation should consume `EvolutionSignal`, not raw `StepScore`.

Examples:

```text
surface=tool_call_parser
kind=tool_call_missing
proposal.kind=loop_control_policy
patch.operation=add_tool_call_validation_feedback
```

```text
surface=context_policy
kind=high_token_usage
proposal.kind=context_policy
patch.operation=tighten_round_evidence_pack
```

```text
surface=tool_schema
kind=bad_tool_arguments
proposal.kind=tool_schema_clarification
patch.operation=clarify_schema
```

## Evolution Bundle

Evolution should also write a manifest:

```text
.loom/evolution/<run-name>/
  evolution-bundle.json
  signals.jsonl
  proposals.jsonl
  gate-decisions.jsonl
  report.md
```

Example:

```json
{
  "schema_version": "loom.evolution.bundle.v1",
  "bundle_id": "evo_20260704_172000_smoke_glm",
  "created_at": "2026-07-04T17:20:00Z",
  "source_evaluation_bundle": "../evaluation/smoke-glm/evaluation-bundle.json",
  "summary": {
    "signals": 3,
    "proposals": 2,
    "accepted_by_gate": 2,
    "rejected_by_gate": 0
  },
  "artifacts": {
    "signals": "signals.jsonl",
    "proposals": "proposals.jsonl",
    "gate_decisions": "gate-decisions.jsonl",
    "report": "report.md"
  }
}
```

## Error Handling

Evaluation bundle loading should fail fast for:

- missing manifest
- unsupported schema version
- malformed JSON
- missing required artifact files
- source trace digest mismatch when digest checking is enabled

Evolution should degrade, not fail, for:

- missing raw trace when normalized findings already include enough evidence
- missing optional evidence excerpts
- unknown finding surface, which should map to `unknown`

Evolution should fail for:

- no findings and no metrics available
- all finding records malformed
- proposal artifact write failures

## TUI Events

Evaluation should emit one artifact event with the bundle path:

```text
evaluation.artifacts.written
  artifacts.evaluation_bundle_path
```

Evolution should emit:

```text
evolution.bundle.loaded
evolution.signals.generated
evolution.proposals.generated
evolution.artifacts.written
```

The TUI should show the bundle path in the event detail panel so the user can
open the exact machine-readable handoff.

## Migration Plan

### Phase 1: Evaluation Bundle Manifest

- Add `EvaluationBundle`, `TraceSource`, `EvaluationSummary`, and
  `EvaluationArtifactRefs`.
- Write `evaluation-bundle.json` from `loom.evaluation.artifacts`.
- Add `evaluation_bundle_path` to `EvaluationArtifacts`.
- Add tests that load the bundle and verify all referenced files exist.

### Phase 2: Normalized Findings

- Add `EvaluationFinding`.
- Convert deterministic findings and judge findings into normalized findings.
- Replace the current mixed `findings.jsonl` contents with
  `EvaluationFinding` records.
- Keep source ids so old records remain traceable.

### Phase 3: Evolution Bundle Input

- Add `--evaluation-bundle` to `loom.evolution.analyze`.
- Add `load_evaluation_bundle()`.
- Add `signals_from_evaluation_findings()`.
- Keep `--trace-path` as legacy fallback.

### Phase 4: Proposal Refinement

- Update proposal generation to use finding-derived signals.
- Add proposal kinds for:
  - `tool_call_parser`
  - `loop_control`
  - `context_policy`
  - `tool_schema`
  - `observability`
- Write `evolution-bundle.json`.

### Phase 5: Legacy Scorer Retirement

- Mark `LlmStepScorer` as legacy.
- Stop using `LlmStepScorer` by default when an evaluation bundle is present.
- Keep it only for old raw trace analysis or ad hoc debugging.

## Testing Strategy

- Unit test `EvaluationBundle` manifest writing and loading.
- Unit test normalized finding mapping for deterministic, round judge, and step
  judge findings.
- Unit test signal aggregation from repeated `frequency_key` values.
- Integration test:

```text
raw trace -> evaluation-bundle.json -> evolution-bundle.json
```

- Regression test that `evolution.analyze --evaluation-bundle ...` does not call
  `LlmStepScorer`.
- TUI test that artifact events include bundle manifest paths.

## Initial Design Decisions

These decisions are intentionally fixed for the first implementation slice:

- Use JSON and JSONL files, not SQLite or Parquet.
- Store artifact paths relative to bundle directory.
- Keep raw trace external and referenced by path and digest.
- Use one normalized `EvaluationFinding` model for every finding source.
- Treat `--trace-path` evolution analysis as legacy once
  `--evaluation-bundle` exists.

## Acceptance Criteria

- Running evaluation writes `evaluation-bundle.json`.
- The bundle manifest references all required artifacts with relative paths.
- The bundle loader validates schema version and required artifact existence.
- `findings.jsonl` contains normalized `EvaluationFinding` records.
- Running evolution with `--evaluation-bundle` produces signals and proposals
  without re-scoring the raw trace through `LlmStepScorer`.
- Raw trace access is used only to resolve evidence references when needed.
- Reports still remain human-readable, but no downstream stage depends on
  markdown content.
