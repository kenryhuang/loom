# Governed Meta-Harness Integration for Loom

## Status

Approved design. This document defines the target architecture and phased
delivery model for integrating the Meta-Harness method into Loom.

The central decision is:

> Loom will give a coding-agent proposer broad freedom to inspect prior
> experience and generate candidate harnesses, while deterministic Loom
> components retain exclusive control over evidence, evaluation, permissions,
> promotion, monitoring, and rollback.

The design supports both declarative candidates and executable Python
components. Executable candidates may run only inside an isolated sandbox and
always require human approval before promotion. Low-risk declarative candidates
may be promoted automatically after deterministic gates and must be monitored
and automatically rolled back on regression.

## Research Basis

This design is based on:

- Yoonho Lee et al., "Meta-Harness: End-to-End Optimization of Model
  Harnesses," arXiv:2603.28052v1, 2026:
  <https://arxiv.org/abs/2603.28052>
- Official reference implementation:
  <https://github.com/stanford-iris-lab/meta-harness>
- Optimized TerminalBench-2 artifact:
  <https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact>
- Lilian Weng, "Harness Engineering for Self-Improvement":
  <https://lilianweng.github.io/posts/2026-07-04-harness/>

The official repositories were reviewed at the following immutable revisions:

| Repository | Commit |
| --- | --- |
| `stanford-iris-lab/meta-harness` | `44b9942127847f7421db70d8c7e48407f09a3c70` |
| `stanford-iris-lab/meta-harness-tbench2-artifact` | `57fefdb2ff84af3fd81b69d67814acbe69bd0743` |

The repositories were cloned under `.loom/research/` for local analysis. That
directory is ignored by git; the implementation is research input rather than
a Loom runtime dependency.

## Context

Loom already has the foundations of an evidence-driven self-improvement
system:

- The runtime emits persisted trace events.
- `loom.trace_analysis` normalizes records into a single episode graph with
  run, runtime-step, LLM-round, tool-call, decision, and observation views.
- `loom.evaluation` combines deterministic metrics and LLM judges and writes a
  versioned `EvaluationBundle`.
- `loom.evolution` consumes normalized findings, produces signals and bounded
  proposals, and writes an `EvolutionBundle`.
- Early mutation registries and shadow-evaluation primitives exist.

The intended fact and decision boundary is already:

```text
Agent Run
  -> TraceBundle
  -> EvaluationBundle
  -> EvolutionBundle
```

Evaluation explains what happened. Evolution decides what should change.

What Loom does not yet have is a governed multi-candidate experiment loop. The
current evolution path converts grouped findings into a small number of
proposal artifacts, but it does not:

- let a coding agent inspect raw prior candidates and traces selectively;
- execute and compare many candidate harnesses;
- preserve candidate lineage and failed experiments;
- maintain a multi-objective Pareto frontier;
- separate discovery, selection, and final holdout evaluation;
- connect shadow evaluation to proposal and promotion lifecycles;
- distinguish candidate-declared risk from deterministically computed risk;
- promote, monitor, expire, and roll back active harness versions.

Meta-Harness supplies the missing search pattern. Loom supplies the stronger
contracts and governance needed to use that pattern safely.

## Problem Statement

Harness improvements have long-range effects. A change to a prompt, retrieval
policy, context builder, tool description, completion policy, or loop strategy
may affect behavior many LLM rounds later. A scalar score or short summary often
cannot explain that causal chain.

The Meta-Harness paper addresses this by exposing all prior candidate code,
scores, and execution traces through a filesystem. A coding agent chooses what
to inspect, forms hypotheses, writes new candidate programs, and repeats an
evaluate-and-log loop.

Directly embedding the reference implementation would create two problems:

1. It uses script-level files such as `pending_eval.json`,
   `evolution_summary.jsonl`, and `frontier_val.json` as shared mutable state,
   while Loom favors explicit typed contracts and immutable artifacts.
2. Its proposer and generated code operate with broader filesystem and process
   access than Loom should permit in a production evolution system.

Loom therefore needs a native campaign layer that preserves Meta-Harness's
selective access to full diagnostic history while enforcing Loom's existing
principles: trace-first execution, explicit typed contracts, deterministic
governance, bounded evolution, reversibility, and evidence-backed decisions.

## Goals

- Treat harness search as a first-class, resumable Loom campaign.
- Give the proposer selective read-only access to complete prior diagnostic
  experience rather than only representative summaries.
- Support declarative patches and executable Python candidate components.
- Keep candidate generation separate from evaluation and promotion.
- Evaluate baseline and candidates under paired, reproducible conditions.
- Maintain a multi-objective Pareto frontier without prematurely collapsing
  quality and cost into one scalar.
- Protect validation and holdout sets from proposer access.
- Make every candidate, experiment, gate, approval, promotion, monitoring
  result, and rollback traceable to immutable evidence.
- Auto-promote only low-risk declarative candidates that pass all gates.
- Require human approval for every executable candidate.
- Preserve all failed, invalid, dominated, and rolled-back candidates as
  future diagnostic experience.
- Reuse Loom's runtime, task configuration, tracing, evaluation, evolution,
  error, and registry conventions.
- Keep proposer vendors interchangeable through a protocol.

## Non-Goals

- Updating model weights or training pipelines.
- Allowing the proposer to switch to a stronger solver model to improve a
  campaign score.
- Letting a candidate edit Loom's active runtime during a run.
- Letting the proposer modify evaluators, benchmarks, permission rules,
  provider settings, hidden data, or governance code.
- Replacing external task verifiers with LLM judgments.
- Concatenating all historical traces into one prompt.
- Making the official Meta-Harness repository a production dependency.
- Automatically publishing generated tool implementations in the initial
  phases.
- Building a distributed, multi-organization scheduler in the first delivery
  plan.
- Treating benchmark improvement as sufficient evidence of security,
  maintainability, or generalization.

## Design Principles

### One Fact Layer

`TraceBundle` and `EpisodeGraph` remain the sole representation of execution
facts. Campaigns must not introduce a second trace parser or reinterpret raw
events independently of `loom.trace_analysis` and `loom.evaluation`.

### Open Diagnosis, Bounded Writes

The proposer may query all history that campaign policy marks visible. It may
write only to a newly allocated candidate workspace and a candidate draft
manifest. It cannot mutate active configuration, historical artifacts,
evaluation output, or lifecycle state.

### External Evaluation

The proposer never owns the formal evaluation process. A controller outside the
candidate sandbox validates and evaluates candidates and writes all official
results.

### Deterministic Governance

LLMs may diagnose, hypothesize, and propose. Deterministic Loom code owns:

- schema and interface validation;
- task-set membership and contamination checks;
- evidence linkage;
- risk classification;
- budget accounting;
- Pareto computation;
- gate outcomes;
- approvals;
- active-version changes;
- monitoring and rollback.

### Immutable Conditions

A campaign freezes the baseline, solver model, tool set, permissions,
environment, task-set digests, evaluator versions, budgets, and objectives.
Changing any frozen condition creates a derived campaign rather than mutating
the active one.

### Failed Attempts Are Experience

Invalid candidates, crashes, regressions, timeouts, and failed hypotheses are
stored permanently. Removing failures would recreate the lossy feedback channel
that Meta-Harness is intended to avoid.

### Reversibility Before Automation

No candidate may be auto-promoted without a concrete rollback artifact and an
active monitoring policy. Executable candidates are never auto-promoted.

## Findings from the Official Implementation

### Reusable Method

The reference implementation validates several useful design choices:

- The proposer sees a small stable instruction containing paths to the run
  history, frontier, reports, and required candidate output.
- The proposer chooses which source files, candidate history, scores, and logs
  to read.
- Candidate proposals include names and natural-language hypotheses.
- Cheap import or interface validation happens before expensive evaluation.
- A separate benchmark process evaluates candidates.
- Text-classification search maintains an accuracy/context-length Pareto
  frontier.
- Validation-only evolution is separated from a one-time final test
  finalization.
- Proposer interactions are logged in full.
- The experimental Harbor implementation exposes a scarce,
  budget-controlled `evaluate_harness` operation.

Relevant source references:

- [Text classification evolution loop](https://github.com/stanford-iris-lab/meta-harness/blob/44b9942127847f7421db70d8c7e48407f09a3c70/reference_examples/text_classification/meta_harness.py)
- [Text classification benchmark and Pareto frontier](https://github.com/stanford-iris-lab/meta-harness/blob/44b9942127847f7421db70d8c7e48407f09a3c70/reference_examples/text_classification/benchmark.py)
- [TerminalBench-2 evolution loop](https://github.com/stanford-iris-lab/meta-harness/blob/44b9942127847f7421db70d8c7e48407f09a3c70/reference_examples/terminal_bench_2/meta_harness.py)
- [Bounded Harbor meta-harness agent](https://github.com/stanford-iris-lab/meta-harness/blob/44b9942127847f7421db70d8c7e48407f09a3c70/experimental/harbor_meta_harness/agents/meta_harness.py)
- [Harbor candidate controller](https://github.com/stanford-iris-lab/meta-harness/blob/44b9942127847f7421db70d8c7e48407f09a3c70/experimental/harbor_meta_harness/controller.py)
- [Optimized TerminalBench-2 environment-bootstrap harness](https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact/blob/57fefdb2ff84af3fd81b69d67814acbe69bd0743/agent.py)

### Gaps Loom Must Not Copy

The official repository describes itself as cleaned-up reference code that has
only been checked to run. It is not a production governance design.

Specific gaps include:

- Shared state is maintained by mutable JSON/JSONL files and generated Python
  files rather than versioned domain contracts.
- Candidate files are written inside the experiment repository.
- The main examples grant the proposer Bash, Write, Edit, Agent, Read, Glob,
  and Grep access in the experiment tree.
- Validation is mainly import, inheritance, method-shape, smoke, and forbidden
  string checking.
- Risk and reversibility are not first-class computed properties.
- Candidate promotion and online rollback are outside the core loop.
- TerminalBench search and final reporting use the same small public benchmark,
  which is suitable for discovery but not sufficient for automated production
  promotion.
- The search orchestrators are domain-specific scripts rather than reusable
  protocols.

Loom will borrow the search behavior, not the script architecture or trust
model.

## Selected Architecture

The selected approach is a governed Evolution Campaign layer.

Alternatives rejected:

1. Extending `loom.evolution` with a direct code generator would be fast, but
   would leave candidate source, experiments, frontiers, and promotion as loose
   artifacts and overload evolution's current responsibility.
2. Running Meta-Harness as an external orchestrator with Loom as a black box
   would isolate the reference code, but would duplicate tracing, evaluation,
   lifecycle, and rollback and create two competing fact systems.

The selected layers are:

```text
loom.runtime
  -> executes tasks and emits immutable trace evidence

loom.trace_analysis
  -> constructs the single normalized event and episode graph

loom.evaluation
  -> interprets execution and emits EvaluationBundle

loom.evolution
  -> converts evaluated findings into signals and seed proposals

loom.campaigns
  -> searches, validates, evaluates, compares, and archives candidates

loom.governance
  -> classifies risk, gates, approves, promotes, monitors, and rolls back
```

The governing rule is:

```text
proposer proposes
controller validates and evaluates
governance decides and activates
```

No layer may bypass the one below it by claiming an outcome without the
corresponding artifact and digest.

## End-to-End Flow

```text
Frozen baseline
  -> create CampaignSpec
  -> import offline Trace/Evaluation/Evolution bundles
  -> run or import baseline experiments
  -> proposer queries visible history
  -> proposer writes CandidateDraft
  -> controller builds CandidateBundle
  -> manifest/static/interface/capability validation
  -> isolated search-set experiments
  -> candidate TraceBundles
  -> candidate EvaluationBundles
  -> paired baseline deltas
  -> FrontierSnapshot
  -> repeat within budget
  -> sealed validation evaluation for finalists
  -> one-time sealed holdout finalization
  -> PromotionDecision
     -> low-risk declarative auto-promotion
     -> executable candidate awaiting human approval
  -> active monitoring
  -> retain, expire, or rollback
```

## Package Boundaries

### `loom.campaigns`

```text
loom.campaigns
  schemas.py       # public immutable campaign contracts
  store.py         # transactional events, projections, leases, and budgets
  reducer.py       # deterministic campaign/candidate state projection
  operations.py    # idempotent operation and reconciliation contracts
  proposer.py      # ProposerAdapter protocol and request/result contracts
  history.py       # read-only history and evidence queries
  workspace.py     # candidate workspace and sandbox specification
  validation.py    # manifest, patch, AST, interface, capability validation
  experiments.py   # paired baseline/candidate experiment orchestration
  frontier.py      # Pareto dominance and snapshots
  controller.py    # campaign state machine and budget enforcement
  artifacts.py     # content-addressed manifests, exports, and reports
  cli.py           # campaign, candidate, and experiment commands
```

`loom.campaigns` owns exploration. It cannot change active runtime versions.

### `loom.governance`

```text
loom.governance
  policy.py        # editable surfaces and promotion/monitoring policies
  risk.py          # deterministic risk classification
  gates.py         # evidence, security, regression, and budget gates
  registry.py      # active artifact and component versions
  promotion.py     # approval and atomic activation
  monitor.py       # post-promotion comparison
  rollback.py      # rollback and expiry
  artifacts.py     # decisions, audit records, and reports
  cli.py           # review, approve, reject, and rollback commands
```

`loom.governance` is the only layer authorized to update active overrides or
the executable component registry.

## Core Contracts

All public contracts are immutable `dataclass(frozen=True, slots=True)` values.
Cross-stage files include a schema version, stable ID, creation timestamp,
relative artifact references, and content digests.

### CampaignSpec

`CampaignSpec` freezes the experiment definition:

```python
@dataclass(frozen=True, slots=True)
class CampaignSpec:
    schema_version: str
    campaign_id: str
    created_at: str
    derived_from: str | None
    objective: str
    baseline: ArtifactRef
    solver: SolverSpec
    proposer: ProposerSpec
    evaluator: EvaluatorSpec
    environment: EnvironmentSpec
    tools_digest: str
    permissions_digest: str
    editable_surfaces: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]
    discovery_set: TaskSetRef
    validation_set: TaskSetRef
    holdout_set: TaskSetRef
    objectives: tuple[ObjectiveSpec, ...]
    budget: CampaignBudget
    candidate_policy: CandidatePolicy
    promotion_policy: PromotionPolicy
    monitoring_policy: MonitoringPolicy
```

Changing any frozen field creates a new campaign with a `derived_from` link.
The derived campaign must use a holdout digest different from every ancestor
whose validation or holdout aggregate has been disclosed. A campaign is not a
mutable configuration namespace: the canonical serialized spec and its digest
never change after creation.

### CandidateBundle

`CandidateBundle` is the durable record of one proposed harness:

```python
@dataclass(frozen=True, slots=True)
class CandidateBundle:
    schema_version: str
    candidate_id: str
    campaign_id: str
    created_at: str
    kind: str
    parent_ids: tuple[str, ...]
    inspiration_ids: tuple[str, ...]
    proposer_session: ArtifactRef
    hypothesis: CandidateHypothesis
    evidence_refs: tuple[EvidenceRef, ...]
    changed_surfaces: tuple[str, ...]
    artifact: ArtifactRef
    patch: ArtifactRef | None
    capability_manifest: CapabilityManifest
```

Candidate kinds are:

- `declarative_patch`
- `executable_component`

The proposer supplies a draft risk statement for explanation only. The
authoritative `RiskAssessment` is computed by governance.

`CandidateBundle` is an immutable proposal artifact. Status, validation,
experiments, frontier membership, risk, and governance decisions are not
mutated into it. They are projected from campaign events into a separate
`CandidateState`:

```python
@dataclass(frozen=True, slots=True)
class CandidateState:
    candidate_id: str
    aggregate_version: int
    lifecycle: CandidateLifecycle
    validation_refs: tuple[ArtifactRef, ...]
    experiment_refs: tuple[ArtifactRef, ...]
    risk_ref: ArtifactRef | None
    latest_governance_ref: ArtifactRef | None
```

The tuple and mapping values in all public contracts are recursively frozen at
construction; `frozen=True` alone is not treated as deep immutability.

### CandidateHypothesis

Every candidate must make a falsifiable claim:

```python
@dataclass(frozen=True, slots=True)
class CandidateHypothesis:
    problem: str
    mechanism: str
    expected_improvements: tuple[MetricPrediction, ...]
    expected_regressions: tuple[MetricPrediction, ...]
    preserved_behaviors: tuple[str, ...]
```

"Try a different prompt" is invalid because it does not state a mechanism or
prediction.

### ObjectiveSpec

Objectives and hard constraints are executable contracts:

```python
@dataclass(frozen=True, slots=True)
class ObjectiveSpec:
    id: str
    direction: ObjectiveDirection  # maximize | minimize
    aggregation: str
    hard: bool
    absolute_limit: float | None
    max_baseline_regression: float | None
    min_valid_pairs: int
    confidence_level: float
    missing_policy: str  # hard constraints require fail_closed
```

A hard objective must define at least one of `absolute_limit` or
`max_baseline_regression`, requires `min_valid_pairs >= 5`, and uses the frozen
paired interval method. Let candidate interval be `[Lc, Uc]` and paired
candidate-minus-baseline delta interval be `[Ld, Ud]`:

- maximize passes an absolute limit only when `Lc >= absolute_limit` and a
  regression limit only when `Ld >= -max_baseline_regression`;
- minimize passes an absolute limit only when `Uc <= absolute_limit` and a
  regression limit only when `Ud <= max_baseline_regression`;
- when both limits exist, both must pass;
- missing/non-finite values, too few pairs, or an unavailable interval are
  `insufficient_evidence` and fail a hard gate.

`max_baseline_regression` is non-negative and measured in the objective's
declared units after its frozen aggregation/transform. Promotion's required
improvement is a separate `PromotionPolicy` field and cannot weaken these
constraints. These rules determine discovery eligibility, validation pass,
holdout pass, and frontier exclusion identically.

### ExperimentBundle

An `ExperimentBundle` compares a candidate and baseline under paired
conditions:

```python
@dataclass(frozen=True, slots=True)
class ExperimentBundle:
    schema_version: str
    experiment_id: str
    campaign_id: str
    candidate_id: str
    created_at: str
    baseline: ArtifactRef
    phase: str
    task_set: TaskSetRef
    environment_digest: str
    solver_digest: str
    tools_digest: str
    trial_plan: TrialPlan
    baseline_evaluations: tuple[ArtifactRef, ...]
    candidate_evaluations: tuple[ArtifactRef, ...]
    baseline_runs: tuple[RunArtifactRef, ...]
    candidate_runs: tuple[RunArtifactRef, ...]
    metrics: tuple[PairedMetricResult, ...]
    failures: tuple[ExperimentFailure, ...]
    status: str
```

Phases are `discovery`, `validation`, `holdout`, and `monitoring`.
`baseline_evaluations` and `candidate_evaluations` refer explicitly to normal
Loom `EvaluationBundle` artifacts. An experiment is invalid if either side
cannot be resolved to evaluation evidence with matching task, environment,
solver, tool, permission, evaluator, and trial-plan digests.

### CampaignBudget

Budget units are explicit rather than a generic “formal evaluation” count:

```python
@dataclass(frozen=True, slots=True)
class PhaseBudget:
    phase: ExperimentPhase
    max_candidate_experiments: int
    max_task_side_runs: int

@dataclass(frozen=True, slots=True)
class CampaignBudget:
    iterations: int
    candidates_per_iteration: int
    phases: tuple[PhaseBudget, ...]
    infrastructure_retry_task_side_runs: int
    proposer_tokens: int
    solver_tokens: int
    maximum_cost: Decimal
    wall_time_seconds: int
```

One candidate experiment means one candidate evaluated against its baseline
over one frozen task set and trial plan. One task-side run means one solver
execution for one side of one `(task, seed, repetition)` entry; an uncached
paired entry consumes two. Scheduling reserves one candidate-experiment slot
and the worst-case task-side runs before execution. Exact-digest baseline cache
hits consume no task-side run, but the candidate experiment slot still counts.

Discovery, validation, and holdout reservations are separate and cannot borrow
from one another. Infrastructure retries consume the dedicated retry-run budget
and still count toward token, cost, and wall-clock ceilings. Candidate failures
are results and are never retried. Unused or released reservations do not count
as consumption; a launched provider call does. Monitoring has its own
`MonitoringPolicy` budget and cannot draw from sealed campaign phases.

### FrontierSnapshot

The frontier is append-only:

```python
@dataclass(frozen=True, slots=True)
class FrontierSnapshot:
    schema_version: str
    snapshot_id: str
    campaign_id: str
    created_at: str
    iteration: int
    candidate_ids: tuple[str, ...]
    objective_values: Mapping[str, Mapping[str, float]]
    dominance_edges: tuple[DominanceEdge, ...]
    excluded: tuple[FrontierExclusion, ...]
    budget_usage: BudgetUsage
```

The latest snapshot is a pointer; earlier snapshots are never overwritten.
`frontier` and `dominated` are classifications within a particular snapshot,
not candidate lifecycle states. A later snapshot may classify the same
candidate differently after new evidence is added.

### PromotionDecision

Each gate has an explicit result:

```python
@dataclass(frozen=True, slots=True)
class PromotionDecision:
    schema_version: str
    decision_id: str
    campaign_id: str
    candidate_id: str
    created_at: str
    baseline_digest: str
    policy_digest: str
    risk_rule_set: ArtifactRef
    gates: tuple[GateDecision, ...]
    computed_risk: RiskLevel
    matched_risk_rules: tuple[str, ...]
    human_approval: ApprovalRecord | None
    decision: PromotionDisposition
    active_artifact: ArtifactRef | None
    rollback_artifact: ArtifactRef | None
```

Possible promotion decisions are `rejected`, `awaiting_approval`, and
`promoted`. Expiry and rollback are later `GovernanceDecision` artifacts that
reference the promotion and the active-version observation that triggered the
change. This prevents a historical promotion decision from being rewritten.

## Candidate Lifecycle

```text
proposed
  -> invalid
  -> validated
     -> evaluated
        -> validation_rejected
        -> validation_passed
           -> holdout_rejected
           -> holdout_passed
              -> awaiting_approval
              -> promoted
                 -> expired
                 -> rolled_back
```

Rules:

- The proposer can create only `proposed` drafts.
- The validator owns `invalid` and `validated` transitions.
- The experiment controller owns `evaluated`; frontier classification belongs
  to immutable `FrontierSnapshot` artifacts.
- Finalization owns validation and holdout states.
- Governance owns all promotion, expiry, and rollback states.
- State transitions use expected-version checks and idempotent operation IDs.
- Invalid transitions return a structured, non-retryable `LoomError`.
- `CandidateState` is always rebuilt by a deterministic event reducer. No JSON
  manifest is independently edited to advance lifecycle state.

## Artifact Layout

```text
.loom/campaigns/<campaign-id>/
  campaign.json
  campaign.sqlite
  exports/
    events.jsonl
    budget.json
  baselines/
    <baseline-id>/
      artifact.json
      source-or-patch/
  imported-experience/
    manifest.json
  candidates/
    <candidate-id>/
      candidate.json
      hypothesis.md
      evidence.jsonl
      patch.json
      src/
      capability.json
      validations/
      experiments/
      proposer-session/
  frontiers/
    frontier-0001.json
    frontier-0002.json
  decisions/
  reports/
```

The campaign directory is movable as one unit. Manifest paths are relative.
Large raw traces may remain in the existing run store, but references must
record path, kind, digest, and expected schema.

The authoritative state is a transactional `CampaignStore`; the reference
implementation uses SQLite in WAL mode. `events.jsonl`, `budget.json`, latest
frontier pointers, candidate state, and reports are reproducible exports, not
independent sources of truth. Immutable payloads are published to a
content-addressed artifact store before a store transaction commits their
references. Publication uses a temporary file, checksum verification, fsync,
and atomic rename. The store records a monotonic event sequence, aggregate
version, operation lease, budget reservation, and artifact digest in one
transaction.

Only authenticated controller/governance service identities may publish or
advance references. Proposer and candidate sandboxes have no artifact-store or
database credentials. Local deployments enforce separate OS ownership and
read-only mounts; exported evidence can additionally be signed for transfer,
but signatures do not replace digest and ACL checks at every read.

## Proposer Model

### Protocol

```python
class ProposerAdapter(Protocol):
    async def propose(
        self,
        request: ProposalRequest,
        history: CampaignHistory,
        workspace: CandidateWorkspace,
    ) -> Result[tuple[CandidateDraft, ...]]: ...
```

The first adapters may launch Codex or Claude Code as subprocesses. A future
`LoomNativeProposerAdapter` may use Loom's own coding-agent loop. No vendor name
appears in the campaign domain model.

The adapter is responsible for:

- starting and cancelling the proposer;
- configuring allowed tools;
- streaming progress events;
- recording tool calls and output;
- persisting the complete proposer session;
- returning a draft manifest and candidate artifact.

It is not responsible for validation, evaluation, risk, or lifecycle state.
The returned tuple contains between one and
`CampaignSpec.budget.candidates_per_iteration` drafts. Returning an
empty tuple, too many drafts, or two drafts with the same canonical digest is a
structured proposal failure.

The proposer itself is untrusted and runs in a separate OS sandbox. It receives
no host credentials, network access, inherited environment, or direct mount of
the campaign directory. Its only historical access is the typed, read-only
query service below; its only write capability is a newly allocated candidate
workspace. A general shell, when enabled for source authoring, is rooted in
that workspace and cannot invoke campaign, evaluator, governance, or host
commands.

Model access crosses a controller-owned inference broker over a narrow local
RPC channel. The broker holds provider credentials and is the only process with
provider egress. It accepts a versioned inference request schema, binds every
request to campaign/model/token/cost limits, rejects arbitrary URLs and headers,
redacts recorded payloads, and returns only model responses and metering. The
proposer sandbox therefore needs neither network access nor credentials. A
local-model adapter may replace the broker without changing `ProposerAdapter`.

### Minimal Starting Context

Each proposer iteration receives only:

- the CampaignSpec summary;
- current iteration and remaining budget;
- editable and forbidden surfaces;
- candidate output contract;
- current frontier summary;
- history-query CLI instructions.

The complete history stays outside the context window and is queried
selectively.

`CandidatePolicy` fixes candidate kinds, editable and forbidden surfaces,
source/file/complexity limits, import and capability allowlists, duplicate
threshold, and executable eligibility. Proposal count belongs to
`CampaignBudget`. A separately digested
`HistoryVisibilityPolicy` defines which discovery fields, trace slices, prior
candidate artifacts, aggregate metrics, and imported experience are visible.
Validation and holdout membership, task text, per-task values, failure labels,
and aggregates are never proposer-visible. Policy evaluation happens in the
query service, not in proposer prompts.

### Read-Only History Interface

The proposer receives a read-only `CampaignHistory` view and a small CLI:

```text
loom campaign status
loom campaign frontier
loom candidate list
loom candidate show <id>
loom candidate diff <left> <right>
loom experiment compare <left> <right>
loom finding search --surface <surface>
loom evidence show <ref>
loom trace slice --candidate <id> --task <task-id>
loom failures cluster
```

Commands support stable JSON output. Every history query is recorded so Loom
can later determine which evidence informed a proposal.

All imported experience and discovery trace fields pass through schema-aware
secret removal and prompt-injection labeling before entering the visible
history index. Query results use a typed data envelope that marks historical
text as untrusted evidence. The proposer system policy instructs it not to
follow embedded instructions, but the real boundary is that history content
cannot grant tools, change mounts, call the evaluator, or write lifecycle
state.

### Candidate Production

The proposer writes only:

- candidate source or structured patch;
- hypothesis;
- evidence references;
- expected improvements and regressions;
- behaviors that should remain unchanged;
- parent and inspiration candidate IDs;
- draft capability declaration.

The proposer may make a local edit, combine prior ideas, or rewrite an allowed
candidate component. There is no hard-coded tournament parent-selection rule.
The controller records lineage regardless of strategy.

## Campaign Search Loop

```text
for each iteration within budget:
  allocate a new candidate workspace
  expose frozen CampaignSpec and read-only visible history
  ask proposer for up to budget.candidates_per_iteration CandidateDraft values
  materialize immutable CandidateBundle and candidate.created event
  validate manifest, patch, source, interface, and capabilities
  reject invalid candidates without expensive benchmark cost
  run paired discovery experiments for valid candidates
  build candidate TraceBundles and EvaluationBundles
  compute paired metrics and uncertainty
  update append-only FrontierSnapshot
  append CampaignEvents
  stop or continue according to policy
```

Every experiment reserves the phase-specific candidate slot and task-side run
units defined by `CampaignBudget`. Static or interface validation does not
consume solver-run budget.

Default stop conditions are:

- iteration, candidate, evaluation, token, cost, or wall-clock budget exhausted;
- frontier unchanged for the configured patience window;
- all candidates rejected for several consecutive iterations;
- repeated evaluator or infrastructure failure;
- human pause, abort, or finalization.

## Dataset Isolation

Loom uses three task-set roles.

### Discovery Set

- Used every search iteration.
- Full traces and findings are visible to the proposer.
- Supports causal diagnosis and frontier updates.

### Validation Set

- Run only by the controller for deterministically selected discovery-frontier
  entrants.
- Task content and per-task traces are hidden from the proposer.
- Results determine finalist eligibility.
- `seal-search` first reaches a durable barrier: it stops proposal admission,
  cancels or closes in-flight discovery work, persists the discovery-frontier
  digest, validation-entrant batch, and fixed validation trial plan, and only
  then permits the first validation lease.
- Candidate creation and discovery search can never reopen after this barrier.
- Neither detailed failures, pass/fail state, aggregates, timing, nor the fact
  that a specific candidate reached validation is returned to the proposer.

### Holdout Set

- Sealed until one-time campaign finalization.
- Hidden from the proposer and candidate workspace.
- Used only for the promotion decision.
- After validation completes, the frozen finalist policy computes a holdout
  batch from validation-eligible candidates without operator substitution.
- Finalization persists that finalist-set digest, fixed holdout trial/seed plan,
  and aggregation policy, then unseals holdout exactly once.

Continuing search after observing holdout results requires a new derived
campaign and a newly sealed holdout set.

Task sets store stable fingerprints. Exact and near-duplicate detection prevents
the same task, template, project snapshot, or trivially transformed instance
from crossing set boundaries.

The default contamination detector records normalized-content SHA-256,
project-snapshot digest, template lineage, and token-shingle MinHash. It rejects
exact matches and pairs whose estimated Jaccard similarity is at least `0.80`.
Campaign policy may make the threshold stricter, but not weaker, and the
detector version, threshold, matched fingerprints, and provenance are part of
the task-set validation artifact.

Validation and holdout run as controller-only jobs. For each task and trial,
the controller creates a fresh ephemeral sandbox containing only the approved
runtime, read-only candidate artifact, and that trial's single task workspace.
The full hidden dataset, other hidden tasks, task registry, membership list,
reference answers, verifier internals, and prior trial scratch are never
mounted. Writable scratch is destroyed after the trial. Only redacted run
artifacts and the fixed aggregate required for governance are published; no
hidden artifact becomes visible to the proposer or a later candidate.

## Paired Experiment Design

Baseline and candidate run with identical:

- task revision;
- solver model and generation parameters;
- tool implementations and descriptions, except the declared candidate
  surface;
- permission set;
- environment image;
- seed set and trial count;
- token, step, time, and resource limits;
- evaluator and rubric versions.

Each metric records baseline value, candidate value, paired delta, trial
variance, and confidence interval where applicable.

The controller freezes a `TrialPlan` before either side starts. Its entries are
ordered `(task_fingerprint, seed, repetition)` tuples. Baseline and candidate
execution order is deterministically counterbalanced from the experiment ID.
The default is three paired repetitions per task. Baseline results may be
reused only when every frozen input digest, trial-plan entry, runner version,
and isolation profile is identical; the initial implementation reruns the
baseline in the same experiment block to avoid temporal confounding.

Aggregation is normative:

- a candidate behavioral failure receives the task-policy outcome and remains
  in the paired sample;
- an infrastructure failure invalidates that pair and makes the experiment
  `partial` unless the same trial ID is retried under its preallocated retry
  allowance;
- missing or non-finite objective values fail closed for hard constraints and
  cannot establish dominance for soft objectives;
- with at least five valid pairs, the default uncertainty method is a two-sided
  paired percentile bootstrap with 10,000 resamples and a seed derived from the
  experiment ID; smaller samples are reported descriptively and cannot claim
  uncertainty-based dominance;
- bootstrap method, confidence level, metric transforms, retry count, and
  missing-value policy are stored in `EvaluatorSpec` and cannot change during
  a campaign.

Infrastructure failure is distinct from candidate failure:

- provider outage or sandbox startup failure makes an experiment partial and
  ineligible for the frontier;
- a candidate-triggered context overflow, tool failure, or task timeout is a
  valid behavioral result;
- whether task timeout receives zero reward is defined by task policy, not by
  a generic infrastructure handler.

Each trial has a stable idempotency key. A retry replaces only the matching
infrastructure-failed attempt and never adds a favorable extra sample. Once the
fixed trial plan is exhausted, the controller cannot add trials based on an
interim score. This forbids optional stopping.

## Evaluation Priority

Evidence has the following precedence:

1. External verifier or environment outcome.
2. Deterministic trajectory and policy metrics.
3. Calibrated LLM judge.
4. Token, latency, cost, and resource metrics.

An LLM judge cannot overturn an external verifier. Judge model, prompt, rubric,
and calibration suite are versioned and frozen in CampaignSpec.

## Pareto Frontier

Default objectives are:

- task success or verifier reward, maximize;
- critical regression count, minimize;
- total tokens, minimize;
- wall-clock latency, minimize;
- LLM rounds and tool retries, minimize;
- failure and timeout rate, minimize;
- candidate complexity, minimize.

CampaignSpec may add domain-specific objectives and identifies which are hard
constraints.

Candidates are dominated only when the configured uncertainty policy supports
the comparison. Overlapping confidence intervals keep candidates tied unless a
hard constraint resolves the tie. Frontier reports retain overall and
stratified task metrics so averages cannot hide a severe regression in one task
class.

The default comparison first rejects hard-constraint violations, then uses
conservative interval dominance: candidate A dominates B only when A is no
worse on every objective and strictly better on at least one objective at the
configured confidence level. Stable tie ordering is canonical candidate ID.

Default `candidate_complexity` is a versioned deterministic score. Declarative
patches score `operation_count + ceil(canonical_patch_bytes / 1024)`.
Executables add one point per 100 AST nodes (rounded up), five per source file,
ten per non-standard-library dependency, and 25 per privileged capability. The
score is an engineering proxy, not a safety gate; its formula/version is frozen
in the campaign and raw components are reported so later policies can replace
it.

At `seal-search`, all discovery-frontier members enter validation when phase
budget allows. If its `max_candidate_experiments` is smaller, entrants are
selected by discovery hard-constraint status, primary-objective lower
confidence bound, critical regression count, cost upper bound, then candidate
ID. After validation, the same ordering is recomputed from validation evidence
over candidates that pass all validation hard gates to create the holdout
finalist batch. The two maximum counts and both selection policies are frozen
before search begins. Operators may abort but cannot add, remove, or reorder
entrants or finalists.

## Candidate Validation

Validation runs from cheapest to most expensive.

### Manifest Validation

- schema version and required fields;
- campaign and parent references;
- artifact digests;
- changed surfaces subset of allowed surfaces;
- at least one evidence reference;
- falsifiable hypothesis and predictions.

### Declarative Patch Validation

- operation is allowlisted;
- target field exists and is mutable;
- values meet type, range, and size limits;
- patch cannot address evaluator or governance configuration;
- inverse rollback patch can be generated.

### Executable Source Validation

- parses successfully;
- implements the required protocol or base class;
- imports are allowlisted;
- forbidden strings and benchmark references are absent;
- filesystem, network, subprocess, dynamic import, reflection, and native
  extension use match policy;
- source complexity and file count stay within budget;
- no absolute path or parent traversal;
- capability declaration matches static analysis.

### Interface Smoke Validation

- import and instantiate;
- run required methods on tiny synthetic inputs;
- validate structured output;
- verify cancellation and timeout behavior;
- verify no writes outside candidate workspace;
- verify resource ceilings.

Only candidates passing all applicable stages enter discovery evaluation.

## Sandbox and Security Model

### Threat Model

Loom assumes proposer output and candidate code can accidentally or
deliberately attempt to:

- edit evaluators, benchmarks, traces, gates, or prior candidates;
- infer hidden tasks from names, environment variables, processes, caches, or
  filesystem metadata;
- read `.env`, API keys, user files, or host credentials;
- forge evaluation output;
- escape through absolute paths, parent traversal, or symbolic links;
- install dependencies or open network connections;
- create fork bombs, infinite loops, excessive output, or disk exhaustion;
- exploit prompt injection in historical traces or tool output;
- hard-code task strings, expected answers, or benchmark-specific branches.

Prompt instructions are guidance, not a security boundary.

### Trusted Solver Inference

Provider-backed task solving uses a controller-owned `SolverInferenceBroker`,
parallel to the proposer inference broker. Provider credentials, network
egress, model selection, generation parameters, tool definitions, metering,
and retry policy remain outside the candidate sandbox.

Candidate components cannot open the broker socket or invoke a model. They
return only their narrow, schema-validated harness output (for example a
context selection, prompt delta, completion decision, or tool-selection
ranking) to the trusted Loom runtime supervisor. The supervisor combines that
output with the current trial's permitted observation, canonicalizes and caps
it, and constructs the frozen `ModelRequest`. The broker accepts only
supervisor-authenticated requests containing campaign, experiment, trial,
solver, permission, and request digests; it rejects candidate identities,
arbitrary URLs/headers, model overrides, undeclared tool schemas, and data not
derived from that trial's permitted observation graph.

The candidate sandbox has neither the broker file descriptor nor a route to
the broker namespace. Broker request/response digests and metering are audited.
Hidden task text may reach the frozen solver provider only as part of the
intended task-scoped model request; reference answers, verifier internals,
other tasks, credentials, and host data are never eligible request inputs.

### Filesystem View

During discovery, each executable candidate trial receives a new disposable
environment:

```text
/candidate  read-only validated candidate artifact
/workspace  task-specific input/workspace
/runtime    read-only approved Loom runtime surface
/scratch    bounded, writable, destroyed after the trial
```

Validation and holdout data, verifiers, reference solutions, governance
configuration, host `.env`, and prior active secrets are not mounted.
Candidates never receive `/history`; history is a proposer-only query
capability. Validation and holdout use the stricter per-trial hidden-task
environment described above.

### Default Restrictions

- Network is disabled for validation, holdout, and promotion evidence with no
  policy override. Discovery network is also disabled by default; a campaign
  that explicitly permits bounded egress becomes non-promotable and is useful
  only for research.
- Host environment variables and credentials are not inherited.
- Dependencies and environment image are frozen by CampaignSpec.
- Candidates cannot install packages at runtime.
- CPU, memory, disk, process count, file handles, output bytes, and wall-clock
  are capped.
- Subprocess, socket, dynamic import, reflection, and native extensions require
  explicit policy capability.
- Every path is resolved and checked against allowed roots before use.
- The evaluator runs outside the candidate sandbox and writes results to a path
  unavailable to the candidate.
- Candidate output crosses a length-bounded, framed, schema-validated channel;
  the controller canonicalizes structured values, escapes log text, treats
  filenames as opaque IDs, and never passes candidate-controlled text as
  evaluator instructions or filesystem paths.

The reference enforcement profile uses a rootless container or equivalent
worker isolation with a read-only root filesystem, explicit bind mounts, user
and mount namespaces, dropped Linux capabilities, `no_new_privs`, seccomp,
network namespace with no interface, cgroup CPU/memory/process limits, disk
quota, and audited syscall/process termination. A plain subprocess is not an
acceptable executable-candidate sandbox. Platforms unable to enforce this
profile may evaluate declarative candidates only.

### CapabilityManifest

Executable candidates declare:

- imports and dependencies;
- filesystem read and write roots;
- subprocess requirement;
- network requirement;
- callable tools;
- input and output schemas;
- maximum resource requirements.

Runtime behavior that exceeds the manifest rejects the candidate and creates a
security finding.

## Risk Classification

Risk is computed deterministically from changed surfaces, patch operations,
capabilities, and source analysis. The highest applicable rule wins.

Default classification:

| Change | Risk |
| --- | --- |
| Token/context numeric limit within policy | low |
| Documentation-only wording with no instruction semantics | low |
| Prompt, system rule, few-shot example, or tool-description instruction | medium |
| Required/enum schema change | medium |
| Resolver priority or completion-policy change | medium |
| Executable Python, runtime hook, or tool implementation | high |
| Evaluator, permission, provider, secret, or governance change | forbidden |

The proposer may explain anticipated risk, but cannot set the authoritative
risk value.

Every assessment records the versioned risk-rule artifact digest and every
matched rule. A prompt-like declarative patch is security-sensitive: it must
pass prompt-injection, permission-escalation, tool-misuse, secret-exfiltration,
and refusal-regression suites and is never eligible for the low-risk automatic
path. Declarative form alone does not imply low risk.

## Promotion

Promotion is an atomic governance operation. A transactional `GovernanceStore`
is the sole source of truth for versioned registry entries, active pointers,
promotion/rollback decisions, governance events, and monitor-registration
state. There is no non-transactional fallback: a registry backend that cannot
atomically compare-and-swap its active pointer with the decision and event is
read-only for Loom promotion. `CampaignStore` later records a content-addressed
reference to the governance decision; that projection is not part of activation
atomicity.

Promotion uses this fixed write order:

1. Authenticate the actor and acquire a lease bound to the expected active
   registry version.
2. Verify baseline, candidate, policy, gate, approval, and dependency digests.
3. Re-evaluate all required gate artifacts and publish candidate plus tested
   rollback artifacts without making either active.
4. Register monitoring for the exact prospective version and require a healthy
   acknowledgement.
5. In one `GovernanceStore` transaction, verify the lease and expected active
   version again, insert the immutable `PromotionDecision`, compare-and-swap the
   active pointer, activate the monitor registration, and append the activation
   event.
6. Release the lease. Reconciliation uses the operation ID to remove orphaned
   pre-activation monitoring registrations or acknowledge the already committed
   transaction; it never guesses from separate campaign events.

Low-risk declarative candidates may be auto-promoted only when:

- evidence resolves successfully;
- validation and sandbox gates pass;
- validation and holdout contain no critical regression;
- a primary objective improves by the configured engineering or statistical
  threshold;
- resource metrics remain within budget;
- rollback artifact is verified;
- monitoring policy is active.

Mandatory gates cannot be waived by a human or policy exception: artifact and
evidence integrity, task-set isolation, no contamination, evaluator
independence, sandbox conformance, forbidden-surface check, security regression
suite, budget correctness, baseline/policy freshness, rollback verification,
and monitoring readiness. Human approval may resolve only a gate explicitly
marked `review_required`; it cannot turn a failed mandatory gate into a pass.

Medium-risk changes and all executable candidates enter `awaiting_approval`.
Human review covers source diff, dependencies, capabilities, task-level
regressions, evaluator results, and rollback plan.

High-risk declarative candidates are rejected by default. A deployment may
allow a named high-risk surface only through a non-campaign change-management
policy requiring two independent approvals; it is never auto-promoted. Any
candidate touching a forbidden surface is rejected rather than routed to
approval.

Executable candidates are installed into a versioned component registry. They
never overwrite Loom source files in place.

An `ApprovalRecord` contains authenticated subject, role, timestamp, expiry,
candidate/source digest, baseline digest, policy and risk-rule digests, complete
gate-result digest, decision, and optional rationale. Any input drift or expiry
invalidates approval. CLI actors and services present a signed assertion from a
configured `IdentityProvider`; authorization is rechecked inside the same store
transaction as the protected action.

The non-configurable minimum action matrix is:

| Action | Required identity/role | Separation rule |
| --- | --- | --- |
| Create/derive campaign; import experience | `campaign_creator` | Cannot weaken global mandatory gates |
| Run/pause/resume/abort; author manual candidates | `campaign_operator` | Only while search is open |
| Seal search; select validation entrants; finalize/unseal holdout | `campaign_finalizer` | Cannot approve or activate a candidate from that campaign |
| Propose/validate/evaluate/update frontier | `campaign_controller` service | No finalization or governance permission |
| Review and approve/reject medium or executable candidate | `governance_approver` | Cannot be finalizer, candidate author, or activator for that candidate |
| Activate an approved candidate manually | `registry_operator` | Cannot be its approver or finalizer |
| Manual rollback or expiry | `registry_operator` | Must name active version and expected pointer digest |
| Auto-promote eligible low-risk override; execute monitor-requested rollback | `governance_automation` service | Cannot approve medium/high risk or change policy |
| Submit signed monitoring evidence/rollback request | `monitor_service` | Cannot mutate registry pointers |
| Assign/revoke privileged roles or publish governance policy | `governance_admin` | No self-grant; two distinct admins for high-risk-surface authorization |

Role conflicts are hard denials, not warning-only policy. There is no
campaign-level exception to finalizer/approver/activator separation. A named
high-risk declarative surface requires the external change-management record,
two distinct `governance_admin` authorizations, and two independent approvers;
none may be the registry operator. Automatic service identities use scoped,
rotatable credentials and cannot authenticate through the human CLI.

Automatic promotion is limited to reversible, internal declarative overrides.
A candidate that can cause an irreversible external side effect, data
migration, outbound message, or stateful third-party action is never
auto-promotable and must supply a reviewed compensation plan. A tested rollback
artifact means its inverse was materialized from the exact prospective version,
applied in an isolated registry fixture, and verified to restore the previous
canonical digest and behavior smoke suite.

## Monitoring and Rollback

`MonitoringPolicy` defines:

- monitored task profiles;
- comparison baseline;
- minimum sample size;
- quality regression threshold;
- token, latency, cost, timeout, and tool-failure ceilings;
- observation window;
- TTL;
- rollback target.

Automatic rollback triggers include:

- statistically or operationally significant outcome regression;
- new permission or security violation;
- timeout or tool-failure increase above policy;
- token, cost, or latency hard-budget violation;
- artifact or dependency digest drift;
- monitoring TTL reached without enough evidence.

Monitoring records the exact active version and observation watermark. Before
each sample and rollback, it verifies with compare-and-swap that this version is
still active; stale monitors close without changing a newer version. Promotion
fails closed if monitoring registration is unhealthy, and an active monitor
that stops heartbeating beyond policy grace causes an alert and automatic
rollback for auto-promoted candidates.

Rollback compare-and-swaps the exact observed active pointer to the recorded
prior version. It does not delete the failed version, traces, promotion
decision, or monitoring evidence.
That history becomes offline experience for later campaigns.

The pointer CAS, immutable `GovernanceDecision(action=rolled_back)`, monitor
closure, and rollback event commit in one `GovernanceStore` transaction. An
expiry uses the same transaction shape. In-flight runs remain pinned to their
start version and are drained or quarantined by policy; automatic candidates
cannot own external effects, so pointer rollback plus cache namespace eviction
fully contains their production state.

## Error Model

### Candidate Errors

Syntax, schema, interface, capability, or permission failures mark a candidate
`invalid`. They do not consume expensive evaluation budget.

### Candidate Behavioral Failures

Task failure, candidate timeout, tool error, context overflow, and bad output
are valid experiment results and remain available to the proposer in discovery
history.

### Infrastructure Failures

Provider outage, sandbox startup failure, or artifact-store interruption marks
an experiment `partial` or `infrastructure_failed`. It cannot enter the
frontier. Retry uses bounded exponential backoff and a separate infrastructure
retry budget.

### Governance Failures

Digest drift, holdout access, unauthorized capability, approval conflict, or
active-version race freezes the affected operation and emits a high-severity
governance event. Authorization and consistency errors are not automatically
retried.

All errors use structured Loom error codes and metadata. Domain status is not
communicated only through exception text.

## Resume and Concurrency

Campaign operations reserve budget and acquire a durable operation lease in the
same `CampaignStore` transaction that records the `started` event. Each
operation has:

- operation ID;
- expected aggregate version;
- input digest;
- status;
- output artifact references.

Every external side effect uses a derived idempotency key and is followed by a
transaction that publishes its artifact reference, consumes or releases the
budget reservation, advances the aggregate version, and records the completed
event. A reconciliation worker examines expired leases and the external
system's idempotency record before retrying, compensating, or closing the
operation. “Before” and “after” JSONL writes alone are not a durability
mechanism.

Replaying a completed operation with the same input returns its recorded
result. Reusing an operation ID with a different input is rejected.

Interrupted experiments remain partial and do not enter the frontier. A resume
operation may retry them under the same frozen conditions or close them as
infrastructure failures. Multiple controllers use optimistic concurrency; only
one may commit a state transition at a given aggregate version.

Budget availability includes committed usage plus active reservations, so
concurrent controllers cannot overspend before compare-and-swap. Operations
lock in this order: campaign aggregate, budget class, candidate aggregate,
experiment aggregate, registry pointer. They never acquire an earlier lock
while holding a later one. Holdout has one campaign-wide lease; finalization
cannot unseal it until all proposal and discovery leases reach the persisted
barrier.

## Observability

A campaign is itself a traced Loom workflow.

Required events include:

```text
campaign.created
campaign.started
campaign.paused
campaign.resumed
campaign.finalized
campaign.aborted

proposal.started
proposal.completed
proposal.failed

candidate.created
candidate.validation.started
candidate.validation.completed
candidate.rejected

experiment.started
experiment.trial.started
experiment.trial.completed
experiment.completed
experiment.failed

frontier.updated
budget.consumed
budget.exhausted

promotion.requested
promotion.gate.completed
promotion.approved
promotion.rejected
promotion.activated
promotion.monitored
promotion.rolled_back
```

Events reference campaign, candidate, experiment, proposer session, artifact
digests, task-set digest, model/evaluator versions, resource usage, and parent
operation ID.

Recording policy removes secrets and protected holdout content before
persistence. Complete host environment variables are never trace payloads.

## Shared Primitive and Serialization Contracts

The first implementation must define these shared primitives before feature
packages add private variants:

- `ArtifactRef(schema_version, kind, relative_path, sha256, byte_size)`;
- `TaskSetRef(task_set_id, role, manifest_ref, fingerprint_digest)`;
- `CandidateDraft`, `CandidatePolicy`, `HistoryVisibilityPolicy`,
  `TrialPlan`, `CampaignBudget`, and `BudgetUsage`;
- string enums for candidate kind, lifecycle, experiment phase/status, risk
  level, gate result, promotion disposition, and governance action;
- `ApprovalRecord`, `OperationLease`, `CampaignEvent`, `GateDecision`, and
  `RiskAssessment`;
- `GovernanceReview` as the authenticated approval-or-rejection command and
  immutable `GovernanceDecision` as its persisted result.

IDs are UUIDv7 values prefixed by aggregate kind, such as `cmp_`, `cand_`,
`exp_`, and `op_`. Timestamps are UTC RFC 3339 strings with microseconds and a
`Z` suffix. Digests are lowercase SHA-256 over RFC 8785 canonical JSON after
schema validation; files use their exact bytes. JSON forbids NaN and Infinity,
sorts semantically unordered collections before serialization, and preserves
declared tuple order where order is meaningful. Enum values, digest input,
path normalization, and timestamp rules have golden cross-version tests.

All references are relative to a declared artifact root, never to process CWD.
Readers reject absolute paths, `..`, symlink escapes, unexpected schemas,
incorrect byte size, and digest mismatch before parsing content.

## Campaign Store Contract

```python
class CampaignStore(Protocol):
    async def load(self, campaign_id: str) -> Result[CampaignProjection]: ...
    async def transact(
        self,
        operation: CampaignOperation,
        expected_version: int,
    ) -> Result[CommittedOperation]: ...
    async def reconcile(self, lease_id: str) -> Result[Reconciliation]: ...
    async def export(self, campaign_id: str) -> Result[ArtifactRef]: ...
```

The reference SQLite implementation owns events, projections, current
pointers, operation leases, budget reservations, and aggregate versions for
campaign exploration. Governance approvals and active-version state belong
only to `GovernanceStore`. Content-addressed files own large immutable payloads.
Projection tables are disposable caches: recovery verifies the event sequence
and artifact digests, rebuilds projections, then compares them with exported
JSON. A corrupt or missing referenced artifact freezes the aggregate rather
than silently dropping evidence.

Store migrations are explicit, forward-only schema versions with backup and
dry-run support. `events.jsonl` is a signed/checksummed portability export with
monotonic sequence numbers; importing it verifies chain continuity and never
merges two histories under one campaign ID.

Governance uses a separate authority-bearing store surface:

```python
class GovernanceStore(Protocol):
    async def active(self, surface_id: str) -> Result[ActiveVersion]: ...
    async def record_review(
        self, review: GovernanceReview, expected_gate_digest: str
    ) -> Result[GovernanceDecision]: ...
    async def acquire_lease(
        self, operation: GovernanceOperation, expected_active_version: int
    ) -> Result[GovernanceLease]: ...
    async def activate(
        self, lease: GovernanceLease, decision: PromotionDecision
    ) -> Result[ActiveVersion]: ...
    async def rollback(
        self, lease: GovernanceLease, decision: GovernanceDecision
    ) -> Result[ActiveVersion]: ...
    async def reconcile(self, operation_id: str) -> Result[Reconciliation]: ...
```

`activate` and `rollback` are single-store transactions with the decision,
pointer CAS, monitor state, and governance event semantics defined in
Promotion and Monitoring. `record_review` authenticates/authorizes the actor
and atomically persists the digest-bound approval or rejection plus its event.
The protocol has no unsafe “set active pointer” method.

## Candidate Materialization and Registry Adapters

A declarative candidate is not evaluated directly from an ad hoc patch. A
versioned `DeclarativePatchCompiler` validates the operations against a typed
surface registry and materializes a normal Loom `MutationBundle` plus its exact
inverse. The compiler records target base digest, normalized operations,
resulting digest, inverse digest, and compiler version. It refuses unknown
fields, ambiguous selectors, and non-invertible operations.

Executable candidates implement a narrow protocol selected by a versioned
`CandidateComponentRegistry`; adapters convert the validated bundle into a
read-only runtime component for the experiment sandbox. Registry installation
is a governance action and is unavailable to campaign controllers or
proposers.

The dependency direction is:

```text
trace_analysis <- evaluation <- evolution
       evaluation.experiment_contracts <- campaigns
                         campaigns.contracts <- governance
```

The neutral experiment contracts live in `loom.evaluation.experiments` (or a
dependency-free contracts module), so `evaluation` never imports `campaigns`.
`campaigns` imports governance-neutral policy and decision contract types only;
it never imports governance controllers. Governance adapters consume immutable
campaign contracts and evaluation artifacts, so there is no
`campaigns <-> governance` cycle.

The legacy `run_shadow_evaluation()` compatibility shim lives at the higher
campaign/evolution integration edge and delegates to `ExperimentRunner`; it
must not introduce an `evaluation -> campaigns` import cycle.

## Public Protocols

```python
class CandidateValidator(Protocol):
    async def validate(
        self,
        candidate: CandidateDraft,
        policy: CandidatePolicy,
    ) -> Result[ValidationResult]: ...


class ExperimentRunner(Protocol):
    async def evaluate(
        self,
        candidate: ValidatedCandidate,
        task_set: TaskSetRef,
        trial_plan: TrialPlan,
    ) -> Result[ExperimentBundle]: ...


class FrontierPolicy(Protocol):
    def update(
        self,
        current: FrontierSnapshot,
        experiment: ExperimentBundle,
    ) -> Result[FrontierSnapshot]: ...


class PromotionController(Protocol):
    async def decide(
        self,
        candidate: CandidateBundle,
        state: CandidateState,
        policy: PromotionPolicy,
    ) -> Result[PromotionDecision]: ...
```

Protocols return Loom `Result` values. Implementations may use subprocesses,
containers, remote sandboxes, or test fakes without changing the campaign
domain model.

## CLI

```text
loom campaign create --config campaign.yaml
loom campaign derive <campaign-id> --config overrides.yaml
loom campaign import-experience <campaign-id> <artifact-path>
loom campaign run <campaign-id>
loom campaign resume <campaign-id>
loom campaign pause <campaign-id>
loom campaign status <campaign-id>
loom campaign frontier <campaign-id>
loom campaign inspect <campaign-id> <candidate-id>
loom campaign seal-search <campaign-id> --expect-frontier-digest <sha256>
loom campaign select-finalists <campaign-id> --dry-run
loom campaign finalize <campaign-id> --expect-finalist-digest <sha256>
loom campaign abort <campaign-id>

loom task-set validate <manifest> --against <manifest> [--against <manifest> ...]
loom task-set fingerprint <manifest>
loom candidate create <campaign-id> --patch <patch.json> --hypothesis <file>
loom candidate import <campaign-id> <candidate-dir>
loom candidate validate <campaign-id> <candidate-id>
loom experiment compare <campaign-id> <baseline-id> <candidate-id>

loom governance review <candidate-id>
loom governance approve <candidate-id>
loom governance reject <candidate-id>
loom governance rollback <promotion-id>
loom governance active [--surface <surface>]
loom governance monitors [--status unhealthy]
loom governance expire <promotion-id>
```

Commands default to concise human-readable output and support `--json` with a
stable versioned schema. Mutating commands require an explicit operation ID or
generate and print one, authenticate the actor, and support safe replay.

## Configuration Shape

An illustrative campaign configuration is:

```yaml
schema_version: loom.campaign.spec.v1
objective: Improve generic project-audit tasks without increasing token cost.

baseline:
  kind: task_profile
  ref: project_audit@v1

solver:
  model_profile: main
  temperature: 0

proposer:
  adapter: codex
  model: default

evaluator:
  deterministic_profile: agent_task_v1
  judge_profile: main

editable_surfaces:
  - tool_description
  - tool_schema_documentation
  - context_policy
  - completion_policy

forbidden_surfaces:
  - runtime_engine
  - evaluator
  - permissions
  - provider
  - secrets

discovery_set: datasets/project-audit-discovery.jsonl
validation_set: datasets/project-audit-validation.jsonl
holdout_set: datasets/project-audit-holdout.jsonl

objectives:
  - id: task_success
    direction: maximize
    aggregation: mean
    hard: true
    absolute_limit: 0.60
    max_baseline_regression: 0.00
    min_valid_pairs: 5
    confidence_level: 0.95
    missing_policy: fail_closed
  - id: total_tokens
    direction: minimize
  - id: wall_time_ms
    direction: minimize
  - id: tool_failure_rate
    direction: minimize

budget:
  iterations: 8
  candidates_per_iteration: 2
  phases:
    - phase: discovery
      max_candidate_experiments: 16
      max_task_side_runs: 1440
    - phase: validation
      max_candidate_experiments: 4
      max_task_side_runs: 168
    - phase: holdout
      max_candidate_experiments: 2
      max_task_side_runs: 96
  infrastructure_retry_task_side_runs: 80
  proposer_tokens: 1000000
  solver_tokens: 5000000
  maximum_cost: 100.00
  wall_time_seconds: 14400

promotion:
  auto_promote_max_risk: low
  executable_requires_human: true
  require_holdout: true

monitoring:
  ttl_runs: 20
  rollback_on_critical_regression: true
```

`campaign_id`, `created_at`, `derived_from`, canonical task-set references, and
all digests are generated or resolved by `loom campaign create`; they are shown
by `loom campaign inspect` but are not hand-authored in the input YAML. The
values above are campaign-specific policy rather than global constants. Input
YAML may omit soft-objective fields; the loader resolves them before hashing to
`aggregation: mean`, `hard: false`, `absolute_limit: null`,
`max_baseline_regression: null`, `min_valid_pairs: 1`,
`confidence_level: 0.95`, and `missing_policy: exclude`. No defaults are applied
to a hard objective.

## Integration with Existing Loom Code

### Trace Analysis and Evaluation

- Reuse `loom.trace_analysis.records`, `graph`, `schemas`, and `evidence`.
- Use `EvaluationBundle` as the only formal diagnosis input.
- Resolve raw events only through evidence references and source-trace digests.
- Candidate experiments use the normal Loom runtime and tracing path.

### Evolution

- Existing `EvolutionBundle` seeds a campaign with findings, signals,
  proposals, and evidence references.
- `loom.evolution.proposals` remains a bounded seed generator; its output does
  not mean a mutation has been accepted.
- Proposal risk fields are treated as draft metadata until governance computes
  `RiskAssessment`.
- Existing evidence aggregation remains useful for navigation, but the proposer
  may query underlying evaluation and trace artifacts instead of relying only
  on one representative message.

### Mutations and Registries

- Existing `MutationBundle`, loop mutation, structure mutation, and registry
  primitives remain reusable behind governance gates.
- Existing `run_shadow_evaluation()` becomes a compatibility shim over the
  general `ExperimentRunner` once campaign experiments are available.
- Active versions use registry pointers rather than in-place source changes.

### Tasks and Runtime

- Reuse named model profiles, task profiles, workspace tools, timeouts,
  streaming, TUI events, and trace-path behavior from `loom.tasks`.
- ExperimentRunner constructs normal task requests with additional frozen
  campaign metadata.
- Solver and proposer are separate roles and may use different adapters.

### Existing Artifacts

- Existing `.loom/evaluation` and `.loom/evolution` directories remain valid.
- Campaign import creates read-only `ImportedExperience` references rather than
  rewriting old artifacts.
- Compatibility loaders make migration incremental.

## Mapping from Official Implementation

| Official reference concept | Loom concept |
| --- | --- |
| `pending_eval.json` | `CandidateDraft` and `CandidateBundle` |
| `evolution_summary.jsonl` | `CampaignEvent` and `ExperimentBundle` |
| `frontier_val.json` | append-only `FrontierSnapshot` |
| `finalized.json` | frozen campaign state and `PromotionDecision` |
| `claude_wrapper.py` | `ProposerAdapter` implementation |
| import/smoke validation | `CandidateValidator` stages |
| `benchmark.py` | `ExperimentRunner` plus Loom Evaluation |
| bounded `evaluate_harness` | controller-owned budgeted evaluation operation |
| generated agent files | isolated candidate workspace |
| reports and raw job logs | TraceBundle, EvaluationBundle, and experiment refs |

## Test Strategy

### Unit Tests

- schema round-trip and unsupported-version rejection;
- canonical JSON, deep immutability, enum, ID, timestamp, and digest golden
  vectors;
- relative path resolution and digest mismatch;
- valid and invalid lifecycle transitions;
- idempotent resume and optimistic concurrency conflict;
- Pareto dominance, ties, hard constraints, and uncertainty;
- paired baseline deltas and trial aggregation;
- budget accounting and exhaustion;
- deterministic risk classification;
- promotion-gate combinations;
- rollback pointer switching;
- task fingerprint and near-duplicate split validation;
- reducer equivalence, lease expiry, budget reservation, and artifact
  publication crash points;
- deterministic finalist selection, fixed retry replacement, missing/NaN
  handling, and no optional stopping;
- exact maximize/minimize hard-constraint interval boundaries;
- phase budget reservation, baseline-cache accounting, retry accounting, and
  no cross-phase borrowing.

### Protocol Contract Tests

Every proposer adapter must demonstrate:

- writes remain inside candidate workspace;
- malformed output becomes an invalid draft;
- full session artifacts are recorded;
- cancellation and timeout are propagated;
- no formal evaluator or lifecycle API is available to the proposer;
- no direct campaign mount, unrestricted history shell, validation signal, or
  host environment is available to the proposer.

Every experiment runner must demonstrate:

- paired conditions are identical;
- baseline and candidate traces use normal Loom schemas;
- infrastructure and candidate failures are separated;
- hidden task data is not mounted into candidate or proposer environments;
- each hidden trial has fresh scratch and only sanitized fixed aggregates are
  published;
- only the trusted supervisor can call `SolverInferenceBroker`, and candidate
  output cannot override model, provider, tools, permissions, headers, or trial
  identity.

### Security Tests

- parent traversal and absolute paths;
- symlink escape;
- `.env` and host credential access;
- network access;
- dynamic dependency installation;
- subprocess and process exhaustion;
- unbounded stdout and large-file writes;
- evaluator-result tampering;
- benchmark and hidden-task string leakage;
- prompt injection in traces and tool output;
- cross-task cache leakage;
- false capability declarations;
- adaptive validation feedback and finalist-selection leakage;
- poisoned imported experience and prompt injection through every visible
  history field;
- approval replay after source/policy/gate drift;
- mandatory-gate override attempts and separation-of-duties violations;
- stale-monitor rollback races, monitoring heartbeat loss, and concurrent
  promotion compare-and-swap;
- crash injection before and after registry, monitoring, artifact, event, and
  budget side effects;
- every RBAC action, forbidden role combination, service-identity scope, and
  stale/revoked assertion.

### Integration Test

A deterministic local campaign must exercise:

```text
baseline
  -> two proposed candidates
  -> one invalid candidate
  -> one evaluated candidate
  -> frontier update
  -> pause and resume
  -> validation
  -> holdout
  -> promotion
  -> monitoring regression
  -> rollback
```

The test rebuilds campaign state from events and artifacts and verifies that the
same frontier and active version result.

### Live Tests

Tests using a real coding agent, LLM, container service, or paid evaluator are
disabled by default and require explicit environment flags. Each live test has
a hard candidate, token, cost, and time limit.

### Reference Behavior Tests

Research tests may compare Loom behavior with the pinned official repository:

- candidate handoff;
- validation/final-test isolation;
- accuracy/context frontier construction;
- proposer file-query logging;
- environment-bootstrap candidate reproduction.

These tests do not import the reference repository into production code.

## Delivery Phases

### Phase 0: Contracts and Read-Only Import

Implement canonical schemas, serialization, task-set references, transactional
CampaignStore, reducer/projections, artifact publication, leases, budget
reservations, create/derive/status/pause/resume/abort CLI, manual candidate
create/import, and read-only import of existing EvaluationBundle,
EvolutionBundle, and research reference artifacts.

No proposer, candidate execution, or promotion is enabled.

Acceptance: two concurrent controllers cannot overspend or advance the same
aggregate version; crash injection at every publication boundary reconciles to
one result; exported events rebuild byte-equivalent projections; corrupt
artifacts fail closed; imported artifacts are visibility-sanitized.

### Phase 1: Declarative Candidate Experiments

Implement campaign controller, declarative validator, paired experiment runner,
MutationBundle materializer, frontier policy, task-set contamination checks,
discovery/validation/holdout isolation, deterministic finalist selection, and
one-time finalization.

Allowed surfaces initially include prompt rules, context budgets, tool
descriptions, and bounded resolver/completion parameters. Promotion remains
disabled: Phase 1 emits a signed promotion recommendation/report but cannot
change an active registry pointer.

Acceptance: a manual declarative candidate completes the full paired discovery,
validation, and holdout path; validation feedback cannot reopen search; hidden
tasks never appear in proposer/candidate mounts or visible exports; the same
artifacts rebuild the same metrics, frontier, finalists, and recommendation.

### Phase 2: Coding-Agent Proposer

Implement ProposerAdapter, at least one subprocess coding-agent adapter,
read-only history CLI, proposer-session artifacts, bounded evaluation requests,
multi-iteration search, pause/resume, and complete budget enforcement.

Promotion remains unavailable.

Acceptance: proposer sandbox and history visibility contract tests pass;
malicious historical instructions cannot grant capabilities; a fixed fake
proposer produces a deterministic multi-iteration campaign across process
restart; validation and holdout signals never return to the proposer.

### Phase 3: Executable Candidates

Implement strong sandboxing, capability manifests, executable source
validation, evaluation-only component adapters, and source review reports.
Executable candidates remain non-promotable in this phase.

Candidate examples include context builders, retrieval policies, completion
policies, bounded loop strategies, and tool-selection policies.

Acceptance: escape, network, secret, fork-bomb, cache-leakage, and evaluator
tampering suites fail closed; each hidden task/trial receives a fresh sandbox;
unsupported platforms reject executable evaluation rather than falling back to
a plain subprocess.

### Phase 4: Governed Promotion

Implement low-risk declarative auto-promotion, active overrides, monitoring,
TTL/expiry, automatic rollback, version-bound human approval for medium/high
risk and executable candidates, registry adapters, RBAC, reconciliation, and
complete audit reports.

Acceptance: mandatory gates cannot be overridden; promotion is atomic under
crash and concurrency injection; stale monitors cannot roll back a newer
version; an unhealthy monitor blocks activation; auto-promotion is limited to
reversible low-risk overrides; monitoring regression restores the exact prior
digest.

### Phase 5: Scale and Transfer

Potential later work includes campaign warm starts, cross-model transfer,
cross-profile candidate reuse, lineage analysis, proposer benchmarking, and
campaign-level cost optimization. These are explicitly outside the first
implementation plan.

### Phase Acceptance Summary

| Phase | Writable production surface | Required end-to-end proof |
| --- | --- | --- |
| 0 | Campaign metadata/artifacts only | Transactional replay and import |
| 1 | None | Manual declarative full evaluation and report |
| 2 | None | Bounded proposer search without hidden feedback |
| 3 | None | Isolated executable evaluation |
| 4 | Versioned governance registry | Atomic approval/promotion/monitor/rollback |
| 5 | To be designed | Transfer and scale criteria defined separately |

## Initial Pilot

The first campaign targets problems already visible in Loom smoke traces:

- shell-command string/array schema confusion;
- intended tool actions that are not emitted or parsed correctly;
- reasoning-only rounds with no forward progress;
- an extra LLM round after completion;
- excessive context and token consumption.

The parser and tool-emission defects themselves remain outside editable
surfaces. The pilot tests whether documentation, context, and completion-policy
changes reduce those observed symptoms; it does not authorize a runtime parser
fix through candidate evolution.

The pilot permits only:

- `tool_description`;
- schema documentation and examples, not tool execution code;
- `context_policy`;
- declarative completion-policy parameters.

It forbids:

- tool implementations;
- runtime engine changes;
- trace recording changes;
- evaluator or judge changes;
- provider/model changes;
- permission changes.

The pilot is two campaigns because a campaign cannot resume search after
validation or holdout access:

1. Create a task registry from representative real-project smoke tasks.
2. Stratify tasks by project, failure type, and complexity.
3. Build two mutually disjoint discovery/validation/holdout cohorts.
4. Freeze model, tools, permissions, evaluator, and environment.
5. Establish paired multi-trial baselines.
6. Campaign A runs manually authored candidates through discovery, seals
   search, runs validation and holdout, and emits the Phase 1 report.
7. Campaign B derives from A with the second cohort and a new holdout digest;
   it imports only A's sanitized discovery experience and enables one proposer
   adapter before any validation access.
8. Campaign B seals search, runs validation and holdout, and emits a second
   report without automatic activation.

The default pilot registry contains 60 frozen tasks. Each campaign receives 30
unique tasks split before any candidate work into 15 discovery, 7 validation,
and 8 holdout tasks, stratified by project and failure class. Each uses three
paired trials per task, eight discovery iterations, at most two drafts per
iteration, no discovery egress, and no automatic promotion. Primary outcome is
deterministic task success; hard gates cover critical regression count,
tool/permission violations, hidden-set contamination, and evaluator integrity.
Secondary objectives are total tokens, wall time, tool failure rate, completion
rounds, and candidate complexity.

The checked-in pilot registry will live under `datasets/meta-harness-pilot/`
and each manifest row must record owner, source trace/evidence reference,
license or internal-use basis, project snapshot digest, task-template lineage,
sanitizer version, and split. Campaign creation refuses rows without this
provenance. Concrete model, image, evaluator, seed, and task digests are filled
from the execution environment during Phase 1 fixture creation and then
committed as the immutable pilot manifest; this design does not invent those
not-yet-existing digests.

The Phase 1 promotion report has a versioned JSON bundle plus Markdown view. It
contains campaign/baseline/candidate/policy digests, split and trial summaries,
paired objective intervals, hard-gate results, stratified regressions, frontier
and finalist rationale, computed risk, rollback materialization result, and a
non-activating disposition of `recommend`, `do_not_recommend`, or
`insufficient_evidence`.

Before fingerprinting, task manifests and imported traces are scrubbed for
credentials, user-identifying paths, hostnames, and incidental secrets using a
versioned sanitizer. The sanitized task payload is the frozen evaluated input;
the system does not retain a hidden unsanitized variant in a proposer-reachable
artifact root.

Pilot system acceptance does not require discovering a better candidate. It
requires proving that:

- campaign state pauses, resumes, and deterministically rebuilds;
- every result resolves to immutable trace evidence;
- validation and holdout remain sealed;
- baseline and candidate conditions are paired;
- proposer and candidate cannot modify evaluator or read secrets;
- invalid, failed, and dominated candidates remain queryable;
- the frontier rebuilds from persisted artifacts;
- non-activating promotion recommendations, gate/risk evidence, and inverse
  rollback-materialization tests are auditable.

## System Acceptance Criteria

The target system is complete when:

- A campaign can be created, run, paused, resumed, finalized, and aborted.
- Every frozen campaign condition has a stable digest.
- Proposer adapters pass the same protocol and security contract tests.
- A proposer can selectively query candidate code, metrics, findings, and trace
  slices across the complete visible history.
- Candidate code cannot access protected history, hidden task sets, evaluator,
  governance, or secrets.
- Invalid candidates are rejected before expensive evaluation.
- Discovery experiments produce normal Loom traces and EvaluationBundles.
- Validation and holdout isolation is enforced by mount and API boundaries, not
  only prompt instructions.
- The Pareto frontier is deterministic from persisted experiment artifacts.
- Risk classification is deterministic and independent of proposer claims.
- Executable candidates cannot be promoted without recorded human approval.
- Low-risk declarative auto-promotion requires all configured gates.
- Every promotion has a tested rollback artifact and monitoring policy.
- Monitoring can automatically roll back a regressing active version.
- Campaign state and active versions can be reconstructed after process loss.
- Existing Loom evaluation and evolution artifacts remain loadable.

## Consequences

### Benefits

- Loom gains automated harness search without giving the search agent authority
  over truth or deployment.
- Full-history access supports causal diagnosis that compressed signals cannot.
- Typed bundles make experiments portable, replayable, and inspectable.
- Failed attempts become reusable knowledge.
- Multi-objective search makes quality/cost tradeoffs explicit.
- Vendor-neutral proposer protocols avoid coupling Loom to one coding agent.

### Costs

- Campaigns introduce substantial artifact volume and lifecycle complexity.
- True dataset isolation requires task registries, fingerprints, and sealed
  evaluation infrastructure.
- Executable candidate support requires a real sandbox, not a subprocess-only
  convention.
- Paired multi-trial evaluation can be expensive.
- Monitoring and rollback extend responsibility beyond offline search.

These costs are accepted because they enforce the same safety and auditability
principles that distinguish Loom from an ungoverned self-editing script.

## Final Decision

Loom will integrate Meta-Harness as a governed Evolution Campaign system.

The coding-agent proposer receives broad, selective, read-only access to prior
candidate code, scores, evaluation findings, and raw trace slices. It may write
declarative patches and executable Python candidates only inside isolated
candidate workspaces. Deterministic controllers validate and evaluate all
candidates. A multi-objective frontier preserves useful tradeoffs and failed
experience. Validation and holdout sets remain sealed. Governance alone may
promote, monitor, expire, or roll back active artifacts.

The implementation will proceed from contracts and declarative experiments to
coding-agent search, executable candidates, and finally low-risk automatic
promotion. The official Meta-Harness code remains a pinned research reference,
not a production dependency.
