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
  registry.py      # campaign/candidate state and optimistic concurrency
  events.py        # append-only CampaignEvent store
  proposer.py      # ProposerAdapter protocol and request/result contracts
  history.py       # read-only history and evidence queries
  workspace.py     # candidate workspace and sandbox specification
  validation.py    # manifest, patch, AST, interface, capability validation
  experiments.py   # paired baseline/candidate experiment orchestration
  frontier.py      # Pareto dominance and snapshots
  controller.py    # campaign state machine and budget enforcement
  artifacts.py     # manifests, JSONL, and reports
  cli.py            # campaign, candidate, and experiment commands
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
  cli.py            # review, approve, reject, and rollback commands
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

### CandidateBundle

`CandidateBundle` is the durable record of one proposed harness:

```python
@dataclass(frozen=True, slots=True)
class CandidateBundle:
    schema_version: str
    candidate_id: str
    campaign_id: str
    created_at: str
    status: str
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
    validations: tuple[ArtifactRef, ...]
    experiments: tuple[ArtifactRef, ...]
    risk: RiskAssessment | None
```

Candidate kinds are:

- `declarative_patch`
- `executable_component`

The proposer supplies a draft risk statement for explanation only. The
authoritative `RiskAssessment` is computed by governance.

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
    baseline: ArtifactRef
    phase: str
    task_set: TaskSetRef
    environment_digest: str
    solver_digest: str
    tool_digest: str
    trial_plan: TrialPlan
    baseline_runs: tuple[RunArtifactRef, ...]
    candidate_runs: tuple[RunArtifactRef, ...]
    metrics: tuple[PairedMetricResult, ...]
    failures: tuple[ExperimentFailure, ...]
    status: str
```

Phases are `discovery`, `validation`, `holdout`, and `monitoring`.

### FrontierSnapshot

The frontier is append-only:

```python
@dataclass(frozen=True, slots=True)
class FrontierSnapshot:
    schema_version: str
    snapshot_id: str
    campaign_id: str
    iteration: int
    candidate_ids: tuple[str, ...]
    objective_values: Mapping[str, Mapping[str, float]]
    dominance_edges: tuple[DominanceEdge, ...]
    excluded: tuple[FrontierExclusion, ...]
    budget_usage: BudgetUsage
```

The latest snapshot is a pointer; earlier snapshots are never overwritten.

### PromotionDecision

Each gate has an explicit result:

```python
@dataclass(frozen=True, slots=True)
class PromotionDecision:
    schema_version: str
    decision_id: str
    campaign_id: str
    candidate_id: str
    baseline_digest: str
    gates: tuple[GateDecision, ...]
    computed_risk: str
    human_approval: ApprovalRecord | None
    decision: str
    active_artifact: ArtifactRef | None
    rollback_artifact: ArtifactRef | None
```

Possible decisions include `rejected`, `awaiting_approval`, `promoted`,
`expired`, and `rolled_back`.

## Candidate Lifecycle

```text
proposed
  -> invalid
  -> validated
     -> evaluated
        -> dominated
        -> frontier
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
- The experiment controller owns `evaluated`, `dominated`, and `frontier`.
- Finalization owns validation and holdout states.
- Governance owns all promotion, expiry, and rollback states.
- State transitions use expected-version checks and idempotent operation IDs.
- Invalid transitions return a structured, non-retryable `LoomError`.

## Artifact Layout

```text
.loom/campaigns/<campaign-id>/
  campaign.json
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

## Proposer Model

### Protocol

```python
class ProposerAdapter(Protocol):
    async def propose(
        self,
        request: ProposalRequest,
        history: CampaignHistory,
        workspace: CandidateWorkspace,
    ) -> Result[CandidateDraft]: ...
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
  ask proposer for one to three CandidateDraft values
  materialize CandidateBundle(status=proposed)
  validate manifest, patch, source, interface, and capabilities
  reject invalid candidates without expensive benchmark cost
  run paired discovery experiments for valid candidates
  build candidate TraceBundles and EvaluationBundles
  compute paired metrics and uncertainty
  update append-only FrontierSnapshot
  append CampaignEvents
  stop or continue according to policy
```

Every formal evaluation consumes an explicit budget unit. Static or interface
validation does not consume the expensive benchmark budget.

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

- Run only by the controller for discovery-frontier finalists.
- Task content and per-task traces are hidden from the proposer.
- Results determine finalist eligibility.
- Detailed validation failures are not returned as next-iteration search hints.

### Holdout Set

- Sealed until one-time campaign finalization.
- Hidden from the proposer and candidate workspace.
- Used only for the promotion decision.
- Finalization freezes the campaign against further candidate creation.

Continuing search after observing holdout results requires a new derived
campaign and a newly sealed holdout set.

Task sets store stable fingerprints. Exact and near-duplicate detection prevents
the same task, template, project snapshot, or trivially transformed instance
from crossing set boundaries.

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

Infrastructure failure is distinct from candidate failure:

- provider outage or sandbox startup failure makes an experiment partial and
  ineligible for the frontier;
- a candidate-triggered context overflow, tool failure, or task timeout is a
  valid behavioral result;
- whether task timeout receives zero reward is defined by task policy, not by
  a generic infrastructure handler.

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

### Filesystem View

Each executable candidate receives a new disposable environment:

```text
/history    read-only visible campaign history
/candidate  read-write current candidate workspace
/workspace  task-specific input/workspace
/runtime    read-only approved Loom runtime surface
```

Validation and holdout data, verifiers, reference solutions, governance
configuration, host `.env`, and prior active secrets are not mounted.

### Default Restrictions

- Network is disabled. Campaign policy may allowlist explicit domains and
  methods.
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
| Prompt or tool-description declarative change | low by default |
| Required/enum schema change | medium |
| Resolver priority or completion-policy change | medium |
| Executable Python, runtime hook, or tool implementation | high |
| Evaluator, permission, provider, secret, or governance change | forbidden |

The proposer may explain anticipated risk, but cannot set the authoritative
risk value.

## Promotion

Promotion is an atomic governance operation:

1. Lock the current active version.
2. Verify that baseline and policy digests still match CampaignSpec.
3. Re-evaluate all required gate artifacts.
4. Write the new versioned artifact and rollback artifact.
5. Record `PromotionDecision` and provenance.
6. Atomically switch the active pointer.
7. Start the monitoring window.

Low-risk declarative candidates may be auto-promoted only when:

- evidence resolves successfully;
- validation and sandbox gates pass;
- validation and holdout contain no critical regression;
- a primary objective improves by the configured engineering or statistical
  threshold;
- resource metrics remain within budget;
- rollback artifact is verified;
- monitoring policy is active.

Medium-risk changes and all executable candidates enter `awaiting_approval`.
Human review covers source diff, dependencies, capabilities, task-level
regressions, evaluator results, and rollback plan.

Executable candidates are installed into a versioned component registry. They
never overwrite Loom source files in place.

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

Rollback switches an active pointer to the recorded prior version. It does not
delete the failed version, traces, promotion decision, or monitoring evidence.
That history becomes offline experience for later campaigns.

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

Campaign operations append a `CampaignEvent` before and after each external
side effect. Each operation has:

- operation ID;
- expected aggregate version;
- input digest;
- status;
- output artifact references.

Replaying a completed operation with the same input returns its recorded
result. Reusing an operation ID with a different input is rejected.

Interrupted experiments remain partial and do not enter the frontier. A resume
operation may retry them under the same frozen conditions or close them as
infrastructure failures. Multiple controllers use optimistic concurrency; only
one may commit a state transition at a given aggregate version.

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
        policy: PromotionPolicy,
    ) -> Result[PromotionDecision]: ...
```

Protocols return Loom `Result` values. Implementations may use subprocesses,
containers, remote sandboxes, or test fakes without changing the campaign
domain model.

## CLI

```text
loom campaign create --config campaign.yaml
loom campaign run <campaign-id>
loom campaign resume <campaign-id>
loom campaign pause <campaign-id>
loom campaign status <campaign-id>
loom campaign frontier <campaign-id>
loom campaign inspect <campaign-id> <candidate-id>
loom campaign finalize <campaign-id>
loom campaign abort <campaign-id>

loom candidate validate <candidate-dir>
loom experiment compare <baseline-id> <candidate-id>

loom governance review <candidate-id>
loom governance approve <candidate-id>
loom governance reject <candidate-id>
loom governance rollback <promotion-id>
```

Commands default to concise human-readable output and support `--json` with a
stable versioned schema.

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

task_sets:
  discovery: datasets/project-audit-discovery.jsonl
  validation: datasets/project-audit-validation.jsonl
  holdout: datasets/project-audit-holdout.jsonl

objectives:
  - id: task_success
    direction: maximize
    hard: true
  - id: total_tokens
    direction: minimize
  - id: wall_time_ms
    direction: minimize
  - id: tool_failure_rate
    direction: minimize

budget:
  iterations: 8
  candidates_per_iteration: 2
  formal_evaluations: 16
  wall_time_minutes: 240

promotion:
  auto_promote_max_risk: low
  executable_requires_human: true
  require_holdout: true

monitoring:
  ttl_runs: 20
  rollback_on_critical_regression: true
```

The values are campaign-specific policy rather than global constants.

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
- relative path resolution and digest mismatch;
- valid and invalid lifecycle transitions;
- idempotent resume and optimistic concurrency conflict;
- Pareto dominance, ties, hard constraints, and uncertainty;
- paired baseline deltas and trial aggregation;
- budget accounting and exhaustion;
- deterministic risk classification;
- promotion-gate combinations;
- rollback pointer switching;
- task fingerprint and near-duplicate split validation.

### Protocol Contract Tests

Every proposer adapter must demonstrate:

- writes remain inside candidate workspace;
- malformed output becomes an invalid draft;
- full session artifacts are recorded;
- cancellation and timeout are propagated;
- no formal evaluator or lifecycle API is available to the proposer.

Every experiment runner must demonstrate:

- paired conditions are identical;
- baseline and candidate traces use normal Loom schemas;
- infrastructure and candidate failures are separated;
- hidden task data is not mounted into candidate or proposer environments.

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
- false capability declarations.

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

Implement campaign schemas, artifact layout, event store, loaders, and import of
existing EvaluationBundle/EvolutionBundle and research reference artifacts.

No proposer, candidate execution, or promotion is enabled.

### Phase 1: Declarative Candidate Experiments

Implement campaign controller, declarative validator, paired experiment runner,
frontier policy, task-set isolation, and manual candidate creation.

Allowed surfaces initially include prompt rules, context budgets, tool
descriptions, and bounded resolver/completion parameters. Promotion remains
manual.

### Phase 2: Coding-Agent Proposer

Implement ProposerAdapter, at least one subprocess coding-agent adapter,
read-only history CLI, proposer-session artifacts, bounded evaluation requests,
multi-iteration search, pause/resume, and complete budget enforcement.

Promotion remains manual.

### Phase 3: Executable Candidates

Implement strong sandboxing, capability manifests, executable source
validation, component registry, source review reports, and human approval.

Candidate examples include context builders, retrieval policies, completion
policies, bounded loop strategies, and tool-selection policies.

### Phase 4: Governed Promotion

Implement low-risk declarative auto-promotion, active overrides, monitoring,
TTL/expiry, automatic rollback, approval CLI, and complete audit reports.

### Phase 5: Scale and Transfer

Potential later work includes campaign warm starts, cross-model transfer,
cross-profile candidate reuse, lineage analysis, proposer benchmarking, and
campaign-level cost optimization. These are explicitly outside the first
implementation plan.

## Initial Pilot

The first campaign targets problems already visible in Loom smoke traces:

- shell-command string/array schema confusion;
- intended tool actions that are not emitted or parsed correctly;
- reasoning-only rounds with no forward progress;
- an extra LLM round after completion;
- excessive context and token consumption.

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

Pilot sequence:

1. Create a task registry from representative real-project smoke tasks.
2. Stratify tasks by project, failure type, and complexity.
3. Build disjoint discovery, validation, and holdout sets.
4. Freeze model, tools, permissions, evaluator, and environment.
5. Establish paired multi-trial baselines.
6. Run manually authored candidates through the complete campaign pipeline.
7. Enable one proposer adapter after experiment facts and frontiers are stable.
8. Produce a promotion report without automatic activation.

Pilot system acceptance does not require discovering a better candidate. It
requires proving that:

- campaign state pauses, resumes, and deterministically rebuilds;
- every result resolves to immutable trace evidence;
- validation and holdout remain sealed;
- baseline and candidate conditions are paired;
- proposer and candidate cannot modify evaluator or read secrets;
- invalid, failed, and dominated candidates remain queryable;
- the frontier rebuilds from persisted artifacts;
- promotion and rollback decisions are auditable.

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
