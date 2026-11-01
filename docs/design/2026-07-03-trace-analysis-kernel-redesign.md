# Trace Analysis Kernel Redesign

This document designs the next evolution of Loom trace evaluation and
evolution. It supersedes the prototype-oriented parts of
`2026-06-28-trace-driven-evolution-design.md` and refines the platform direction
from `2026-07-02-trace-evaluation-evolution-platform-design.md`.

The design keeps the original Loom Python principles from
`2026-06-05-loom-py-design.md`: explicit typed contracts, immutable trace
artifacts, small public APIs, async control flow, deterministic governance, and
bounded evolution.

## Purpose

Loom should be able to inspect an agent run, explain what happened at the level
of individual LLM rounds and tool calls, score behavior across multiple
dimensions, and propose evidence-backed improvements to prompts, tools, context
policy, loop control, and skills.

The key change is to stop treating `evolution` as a direct trace-to-proposal
script. Instead, introduce a shared trace analysis kernel:

```text
raw trace JSONL
  -> normalized records
  -> episode graph
  -> trajectories and evidence packs
  -> deterministic and judge assessments
  -> normalized findings
  -> evolution signals
  -> proposals
  -> experiments and gates
  -> accepted or rejected mutations
```

Evaluation explains the run. Evolution acts only on evaluated evidence.

## Research Baseline

Current agent evaluation and observability systems converge on the same
architecture:

- Trace first, then evaluate. OpenTelemetry GenAI and OpenInference standardize
  LLM calls, agent spans, tool invocations, retrieval operations, token usage,
  and privacy-aware content capture as trace semantics.
- Evaluate trajectories, not only final outputs. Agent behavior often emerges
  through tool choice, argument formatting, intermediate observations, and
  recovery after errors.
- Use layered evaluators. Deterministic checks catch structural failures;
  LLM-as-judge handles semantic judgments; human review and datasets calibrate
  both.
- Separate online trace scoring from offline experiments. Production traces
  surface failures; curated datasets and replay experiments decide whether a
  prompt, tool, model, or loop change actually improves quality.
- Keep environment outcome distinct from agent claims. A final answer can say
  success while the external environment proves otherwise.

References reviewed:

- OpenInference Specification: https://arize-ai.github.io/openinference/spec/
- OpenTelemetry GenAI Semantic Conventions:
  https://github.com/open-telemetry/semantic-conventions-genai
- OpenAI Agent Evals:
  https://developers.openai.com/api/docs/guides/agent-evals
- OpenAI Agents SDK Tracing:
  https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md
- Anthropic, Demystifying evals for AI agents:
  https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- LangSmith Evaluation and trajectory evaluation:
  https://docs.langchain.com/langsmith/evaluation
  https://docs.langchain.com/langsmith/trajectory-evals
- Langfuse Evaluation Overview:
  https://langfuse.com/docs/evaluation/overview
- Arize Phoenix Evaluation:
  https://arize.com/docs/phoenix/evaluation/llm-evals
- Braintrust Evaluation:
  https://www.braintrust.dev/docs/evaluate

## Current State

Loom already has valuable pieces:

- `JsonlTraceStore` persists runtime events and completed `Trace` records.
- `EventRecordingPolicy` can filter noisy stream delta events.
- `loom.evaluation.records` normalizes JSONL records into `NormalizedEvent`.
- `loom.evaluation.episodes` builds a run, step, LLM round, and tool call graph.
- `loom.evaluation.metrics` and `loom.evaluation.assessments` perform initial
  deterministic scoring.
- `loom.evolution.episodes` builds a separate `StepEpisode`.
- `LlmStepScorer` performs prompt-based LLM scoring.
- `aggregate_step_scores`, `generate_evolution_proposals`, and `gate_proposal`
  create early evolution artifacts.
- `loom.tasks` already provides config-driven model selection, trace paths,
  TUI routing, streaming, and a generic task runner.

The prototype is useful, but the boundaries are unclear:

- `evaluation` and `evolution` each reconstruct trace episodes differently.
- `evolution` scores a runtime step, but a real run may put many LLM rounds and
  tool calls inside one runtime step.
- LLM judge evidence is too broad. Findings are attributed to a whole step
  instead of a specific LLM round, tool call, message, argument, or output.
- Proposal generation consumes LLM attribution directly instead of normalized
  findings.
- `evolution.analyze` uses `.env` provider creation rather than the same
  `config.yaml` model system used by `loom.tasks`.
- The report exposes long event hash lists instead of concise evidence
  references.

## Design Principles

### One Fact Layer

All analysis must use one normalized event and episode graph model. `evaluation`
and `evolution` should not maintain separate trace parsers.

### Episode Granularity Before Scoring

Scoring must target the smallest meaningful unit:

- `ToolCallEpisode` for tool argument/result quality.
- `LlmRoundEpisode` for model response, tool choice, and prompt following.
- `RuntimeStepEpisode` for loop state transitions.
- `RunEpisode` for final outcome and task completion.

Runtime steps are not assumed to be agent decision steps.

### Findings Before Proposals

Evaluation emits `AssessmentFinding` records. Evolution consumes those findings.
No proposal should depend on raw judge prose alone.

### Evidence Slices, Not Raw Dumps

LLM judges receive bounded evidence packs with only the relevant messages,
tool schemas, tool arguments, tool outputs, deterministic metrics, and outcome
facts. The judge should never receive an entire JSONL trace by default.

### Deterministic Governance

LLMs may judge, classify, and suggest. Deterministic code owns schema
validation, evidence references, proposal lifecycle, risk gates, experiment
comparison, and rollback.

### Config and TUI Consistency

Evaluation and evolution are Loom tasks from an operational perspective. They
should reuse the same config file, named models, trace path handling, and TUI
event stream conventions as `loom.tasks`.

## Package Boundary

Introduce a shared package:

```text
loom.trace_analysis
  records.py       # JSONL -> NormalizedRecord / NormalizedEvent
  graph.py         # Normalized events -> EpisodeGraph
  trajectory.py    # EpisodeGraph -> AgentTrajectory views
  evidence.py      # compact evidence packs and evidence refs
  schemas.py       # shared dataclasses and enums
  selectors.py     # query helpers for run/step/round/tool slices
```

Refactor `evaluation` to depend on `trace_analysis`:

```text
loom.evaluation
  evaluators.py       # Evaluator protocol and registry
  deterministic.py    # structural and policy checks
  trajectory.py       # trajectory and tool-use evaluators
  judge.py            # LLM-as-judge execution
  assessments.py      # normalized Assessment and Finding records
  reports.py          # scorecards and markdown reports
  analyze.py          # CLI orchestration
```

Refactor `evolution` to depend on `evaluation`:

```text
loom.evolution
  signals.py       # findings -> repeated patterns
  proposals.py     # patterns -> candidate mutations
  experiments.py   # baseline vs candidate replay/shadow runs
  gates.py         # confidence, risk, reversibility, regression gates
  registry.py      # proposal lifecycle
  artifacts.py     # reports and JSONL artifacts
  run.py           # CLI orchestration
```

Keep compatibility shims for the current modules during migration:

- `loom.evolution.analyze`
- `loom.evolution.episodes`
- `loom.evolution.scoring`
- `loom.evaluation.records`
- `loom.evaluation.episodes`

## Trace Analysis Model

### Normalized Event

The current `NormalizedEvent` shape is close to the target. Move it into
`loom.trace_analysis.schemas` and add stable field-path evidence support.

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
    hash: str | None = None
    line_number: int | None = None
```

### Evidence Reference

Findings should not expose giant hash lists. Use compact references.

```python
@dataclass(frozen=True, slots=True)
class EvidenceRef:
    event_hash: str | None
    event_type: str
    subject_id: str | None
    field_path: str | None = None
    excerpt: str | None = None
```

Examples:

- `llm.requested.messages[0].content`
- `llm.completed.response.tool_calls[1].function.arguments`
- `tool.completed.output.stderr`
- `trace.completed.decisions[0].action`

Reports show the compact ref and excerpt. JSONL artifacts retain the hash.

### Episode Graph

Use one graph for all downstream analysis:

```python
@dataclass(frozen=True, slots=True)
class EpisodeGraph:
    events: tuple[NormalizedEvent, ...]
    runs: tuple[RunEpisode, ...]
    runtime_steps: tuple[RuntimeStepEpisode, ...]
    llm_rounds: tuple[LlmRoundEpisode, ...]
    tool_calls: tuple[ToolCallEpisode, ...]
    decisions: tuple[DecisionEpisode, ...]
    observations: tuple[ObservationEpisode, ...]
    orphaned_events: tuple[NormalizedEvent, ...]
```

Status values:

```text
complete
partial
failed
orphaned
corrupt
```

Partial and orphaned episodes are evaluable evidence. Malformed JSONL is a
load error for that file.

### Agent Trajectory

Create a trajectory view from the graph:

```python
@dataclass(frozen=True, slots=True)
class AgentTrajectory:
    run_id: str
    objective: str | None
    rounds: tuple[TrajectoryRound, ...]
    final_output: Mapping[str, Any] | str | None
    outcome: Mapping[str, Any]
```

Each `TrajectoryRound` should summarize:

- input messages and selected tool schemas
- model response
- requested tool calls
- executed tool calls and results
- observations added to context
- parse fallback or schema violations
- token usage and duration

Trajectory views enable deterministic checks such as strict, unordered, subset,
or superset tool sequence matching.

## Evaluation Model

### Assessment

All evaluators emit the same normalized shape:

```python
@dataclass(frozen=True, slots=True)
class Assessment:
    id: str
    evaluator_id: str
    evaluator_version: str
    scope: str              # run, runtime_step, llm_round, tool_call
    subject_id: str
    status: str             # pass, warn, fail
    aggregate_score: float
    dimensions: Mapping[str, DimensionScore]
    findings: tuple[AssessmentFinding, ...]
    evidence: tuple[EvidenceRef, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

### Finding

```python
@dataclass(frozen=True, slots=True)
class AssessmentFinding:
    id: str
    severity: str           # info, warning, error
    category: str
    surface: str
    subject_id: str
    message: str
    recommendation: str | None
    confidence: float
    evidence: tuple[EvidenceRef, ...]
```

The `surface` vocabulary is shared with evolution:

```text
system_prompt
profile_prompt
task_brief
tool_schema
tool_description
tool_collection
tool_runtime
context_policy
history_context
knowledge_context
loop_control
model_selection
observability
permission_policy
skill_context
```

### Dimensions

Use these first-version dimensions:

- `task_progress`
- `instruction_following`
- `tool_selection`
- `tool_arguments`
- `tool_result_handling`
- `evidence_grounding`
- `context_quality`
- `efficiency`
- `recovery`
- `observability`

Dimensions are reusable across scopes. A `tool_call` assessment may leave
non-applicable dimensions absent rather than assigning fake perfect scores.

## Evaluator Layers

### Deterministic Evaluators

Deterministic evaluators answer "what happened" without model judgment:

- trace completeness
- event schema validity
- missing LLM completion
- failed or partial tool call
- tool result not followed by LLM use
- parse fallback
- missing finish call
- final report missing
- high token use
- repeated identical tool calls
- tool output truncation
- missing event hashes
- orphaned events

These should be fast, reproducible, and safe to run in CI.

### Trajectory Evaluators

Trajectory evaluators inspect ordered behavior:

- required tool presence
- forbidden tool use
- strict tool sequence
- unordered tool set
- subset or superset matching
- tool argument shape matching
- no-op loop detection
- repeated ineffective action detection

They can run with or without reference trajectories. For task profiles such as
`project_audit`, reference-free heuristics are enough in the first version.

### Outcome Evaluators

Outcome evaluators compare agent claims to external evidence:

- final answer satisfies success criteria
- command exit code supports claimed success
- files were or were not modified as required
- final report cites observed files or command output
- blocker claims are supported by error evidence

This layer prevents a fluent final response from masking a failed environment
outcome.

### LLM Judge Evaluators

LLM judges answer semantic questions:

- Was the chosen tool appropriate?
- Did the agent interpret tool output correctly?
- Is the final report grounded in evidence?
- What minimal surface should change?

Judge input is an `EvidencePack`, not raw JSONL. Judge output must be structured
JSON, preferably through tool/function calling when the provider supports it.

The judge must output findings with:

- surface
- problem
- recommendation
- severity
- confidence
- evidence refs

If the judge cannot cite evidence, the finding is rejected.

## Evidence Packs

Evidence packs make judge calls bounded and auditable.

```python
@dataclass(frozen=True, slots=True)
class EvidencePack:
    subject: EpisodeRef
    objective: str | None
    success_criteria: tuple[str, ...]
    summary: Mapping[str, Any]
    metrics: tuple[MetricResult, ...]
    excerpts: tuple[EvidenceExcerpt, ...]
    token_budget: int
```

Evidence pack construction rules:

- Include only events linked to the subject and its immediate parent/children.
- Keep prompt excerpts short and field-addressable.
- Include tool schema excerpts only for tools visible in that round.
- Include tool output excerpts with truncation metadata.
- Include deterministic findings before judge findings.
- Redact secrets and environment variables before judge calls.

## Evolution Model

Evolution consumes normalized findings, not raw step scores.

```text
AssessmentFinding[]
  -> EvolutionSignal[]
  -> EvolutionProposal[]
  -> ProposalGateResult[]
  -> ExperimentResult[]
  -> AcceptedMutation | RejectedProposal
```

### Signals

Signals aggregate repeated or high-severity findings:

```python
@dataclass(frozen=True, slots=True)
class EvolutionSignal:
    id: str
    kind: str
    surface: str
    category: str
    severity: float
    frequency: int
    confidence: float
    subject_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    explanation: str
```

Aggregation examples:

- Same tool argument failure appears across multiple rounds.
- Same instruction-following failure appears across runs.
- Same context-policy issue causes high token use without better outcomes.
- A required tool is repeatedly unavailable or skipped.
- The final report repeatedly lacks evidence despite available observations.

### Proposals

Proposal kinds:

- `prompt_rule`
- `prompt_compaction`
- `tool_schema_clarification`
- `tool_runtime_policy`
- `tool_collection_policy`
- `context_policy`
- `loop_control_policy`
- `skill_proposal`
- `model_routing_policy`
- `observability_policy`

Proposal contract:

```python
@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    id: str
    kind: str
    surface: str
    title: str
    rationale: str
    patch: Mapping[str, Any]
    expected_impact: Mapping[str, Any]
    risk: str
    reversible: bool
    ttl_runs: int | None
    created_from_signal_ids: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    confidence: float
```

Low-risk examples:

- Clarify a tool description.
- Add one concise prompt rule.
- Reduce redundant prompt text.
- Adjust tool resolver priority.
- Add a context compaction rule.

High-risk examples require human review:

- Add executable tool code.
- Change permission policy.
- Change shell execution policy.
- Change model provider.
- Rewrite a profile prompt.
- Change loop termination logic.

### Experiments and Gates

Every proposal must pass deterministic gates before application:

- confidence threshold
- evidence count threshold
- risk threshold
- reversibility requirement
- token budget impact
- no security or permission escalation
- no conflict with active overrides

Then it enters shadow evaluation:

```text
baseline runner + dataset examples
candidate runner + same dataset examples
same evaluators
compare deltas
accept only if improvement is positive and regressions are absent
```

For early versions, shadow evaluation can be simulated by replaying trace
evidence through evaluators where full environment replay is unavailable. Real
task replay should be added for `loom.tasks` profiles that have deterministic
workspace fixtures.

## Config and CLI

Evaluation and evolution should use the same config file model as
`loom.tasks`.

Add optional config sections:

```yaml
evaluation:
  model: glm
  out_dir_template: .loom/evaluation/{trace_stem}
  evaluators:
    - deterministic
    - trajectory
    - outcome
    - judge
  judge_token_budget: 12000

evolution:
  model: glm
  out_dir_template: .loom/evolution/{trace_stem}
  min_confidence: 0.75
  min_signal_frequency: 2
  max_proposals: 5
  auto_apply: false
```

CLI shape:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/smoke-glm-20260703-214154-774875.jsonl \
  --config config.yaml \
  --model glm \
  --tui

uv run python -m loom.evolution.run \
  --assessment-path .loom/evaluation/smoke-glm/assessments.jsonl \
  --config config.yaml \
  --model glm \
  --tui
```

Compatibility command:

```bash
uv run python -m loom.evolution.analyze ...
```

This remains as a wrapper during migration.

## Artifacts

Evaluation writes:

```text
episode-graph.json
metrics.jsonl
assessments.jsonl
findings.jsonl
judge-scores.jsonl
report.md
```

Evolution writes:

```text
signals.jsonl
proposals.jsonl
gate-results.jsonl
experiments.jsonl
accepted-mutations.jsonl
report.md
```

Reports should lead with concise tables:

- failing subjects
- dimensions by scope
- top findings
- proposed surfaces
- evidence refs
- next action

Long raw hashes stay in JSONL artifacts, not markdown bodies.

## TUI Integration

The same TUI event stream should cover task runs, evaluation runs, and evolution
runs.

New event types:

- `trace_analysis.ingest.started`
- `trace_analysis.ingest.completed`
- `trace_analysis.graph.built`
- `evaluation.evaluator.started`
- `evaluation.evaluator.completed`
- `evaluation.finding.recorded`
- `evaluation.assessment.completed`
- `evolution.signal.generated`
- `evolution.proposal.generated`
- `evolution.gate.completed`
- `evolution.experiment.completed`

LLM judge calls reuse existing `llm.requested`, `llm.stream.*`, and
`llm.completed` events. Stream deltas should be aggregated in TUI and excluded
from persisted trace files by default unless explicitly enabled.

## Migration Plan

### Phase 1: Shared Kernel

- Add `loom.trace_analysis`.
- Move or wrap `NormalizedEvent` and `EpisodeGraph`.
- Build `RunEpisode`, `RuntimeStepEpisode`, `LlmRoundEpisode`,
  `ToolCallEpisode`, `DecisionEpisode`, and `ObservationEpisode`.
- Add evidence refs and evidence pack construction.
- Keep current `evaluation` and `evolution` imports working.

### Phase 2: Evaluation Rewrite

- Introduce `Evaluator` protocol.
- Port existing deterministic metrics and assessments into evaluator outputs.
- Add trajectory and outcome evaluators.
- Convert LLM scorer into a judge evaluator that consumes evidence packs.
- Write `assessments.jsonl` and `findings.jsonl`.
- Update reports to be scope-aware and evidence-ref based.

### Phase 3: Evolution Rewrite

- Make evolution consume `findings.jsonl`.
- Rebuild signals from normalized findings.
- Generate proposals with concise evidence refs.
- Add gates as first-class artifacts.
- Keep `loom.evolution.analyze` as compatibility wrapper.

### Phase 4: Experiments

- Add dataset examples derived from failed traces.
- Add baseline vs candidate experiment records.
- Add regression gates.
- Support shadow evaluation before proposal acceptance.

### Phase 5: Controlled Mutation

- Add a registry for proposed, accepted, expired, and rolled-back mutations.
- Support manual acceptance first.
- Add optional auto-apply only for low-risk reversible changes.
- Monitor accepted mutations and rollback on regression.

## First Implementation Slice

The first slice should not try to auto-evolve Loom. It should deliver reliable
analysis:

```text
input: one existing runs/*.jsonl
output:
  episode graph with LLM rounds and tool calls
  deterministic assessments
  judge assessments by LLM round and tool call
  normalized findings with evidence refs
  concise markdown report
```

This is the smallest slice that fixes the current ambiguity. Once findings are
precise and normalized, evolution becomes a cleaner downstream problem.

## Success Criteria

- A trace with one runtime step and many LLM rounds produces many evaluable
  `LlmRoundEpisode` and `ToolCallEpisode` subjects.
- Deterministic evaluation works without an LLM provider.
- LLM judge calls receive bounded evidence packs, not raw JSONL.
- Every finding has a scope, subject id, surface, confidence, and evidence ref.
- Evolution proposals cite signal ids and concise evidence refs.
- The analyzer can run with `config.yaml` model aliases.
- TUI shows evaluation and evolution events using the same timeline mechanics as
  task runs.
- Reports are useful to a human reviewer without opening raw JSONL.

## Deliberate First-Version Limits

- No automatic mutation application.
- No generated tool code.
- No permission policy changes.
- No full environment replay unless a task profile provides fixtures.
- No requirement that every production trace has reference output.
- No dependency on external observability services.

External systems such as OpenTelemetry, OpenInference, LangSmith, Langfuse,
Phoenix, and Braintrust should inform semantics. Loom's local JSONL artifacts
remain the first-class source of truth.
