# Governed Meta-Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved governed Meta-Harness design through Phase 4: durable campaign contracts and imports, declarative paired experiments, bounded proposer search, fail-closed executable-candidate support, and transactional governance with monitoring and rollback.

**Architecture:** Add `loom.campaigns` as the exploration authority and `loom.governance` as the activation authority. Immutable, content-addressed artifacts hold payloads; SQLite stores append-only events, projections, leases, reservations, registry versions, approvals, and active pointers. Campaigns consume normal Loom `EvaluationBundle` and `EvolutionBundle` artifacts, while governance consumes immutable campaign/evaluation evidence and is the only package allowed to mutate active registry state.

**Tech Stack:** Python 3.11+, standard-library dataclasses/enums/hashlib/json/sqlite3/pathlib/ast/asyncio/subprocess, existing Loom `Result`/`LoomError`, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-18-governed-meta-harness-design.md` as the normative contract.
- Implement Phase 0 through Phase 4. Phase 5 remains explicitly deferred.
- Preserve the dependency direction `trace_analysis <- evaluation <- evolution`, with campaign-neutral experiment contracts under `loom.evaluation.experiments`.
- Never introduce `evaluation -> campaigns` or `campaigns -> governance controller` imports.
- Public contracts use `dataclass(frozen=True, slots=True)` and recursively freeze nested collections.
- Use content digests, relative paths, optimistic versions, idempotent operation IDs, and fail-closed readers at every authority boundary.
- Never evaluate executable candidates in a plain subprocess. Unsupported isolation reports a structured non-retryable error.
- Promotion and rollback must compare-and-swap the expected active version in the same SQLite transaction as decision, monitor, and event persistence.
- Add no live provider/container requirement to the default test suite. Live and platform isolation tests remain opt-in.
- Follow TDD RED -> GREEN for every task and use `apply_patch` for source/test edits.

---

### Task 1: Shared Immutable Contracts and Canonical Artifacts

**Files:**
- Create: `src/loom/campaigns/__init__.py`
- Create: `src/loom/campaigns/contracts.py`
- Create: `src/loom/campaigns/serialization.py`
- Create: `src/loom/campaigns/artifacts.py`
- Create: `tests/campaigns/test_contracts.py`
- Create: `tests/campaigns/test_artifacts.py`
- Modify: `src/loom/core/models.py`

**Interfaces:**
- `new_prefixed_id(prefix: str) -> str` emits UUIDv7-shaped IDs for `cmp_`, `cand_`, `exp_`, `op_`, `evt_`, and governance IDs.
- `canonical_json_bytes(value: Any) -> bytes` rejects non-finite numbers and unstable values.
- `canonical_digest(value: Any) -> str` computes lowercase SHA-256 over canonical bytes.
- Immutable enums and values: `ArtifactRef`, `TaskSetRef`, `ObjectiveSpec`, `PhaseBudget`, `CampaignBudget`, `BudgetUsage`, `CampaignSpec`, `CandidateDraft`, `CandidateBundle`, `CandidateState`, `ExperimentBundle`, `FrontierSnapshot`, `CampaignEvent`, `OperationLease`, `ApprovalRecord`, `GateDecision`, `RiskAssessment`, `PromotionDecision`, and supporting policy/trial/metric contracts.
- `ArtifactStore.publish_bytes()` atomically publishes exact bytes by digest; `resolve()` rejects absolute paths, traversal, symlink escape, size mismatch, digest mismatch, and schema mismatch.

- [x] **Step 1: Write failing contract and artifact tests** covering deep immutability, enum values, IDs/timestamps, canonical digest golden vectors, NaN/Infinity rejection, relative paths, atomic deduplication, traversal/symlink rejection, and digest/size mismatch.
- [x] **Step 2: Run `uv run pytest tests/campaigns/test_contracts.py tests/campaigns/test_artifacts.py -q` and verify RED** because the package does not exist.
- [x] **Step 3: Implement the minimal contracts, canonical serializer, ID/timestamp validators, and content-addressed artifact store.**
- [x] **Step 4: Re-run the focused tests and `uv run ruff check src/loom/campaigns tests/campaigns`.**

### Task 2: Transactional Campaign Store, Reducer, and Phase 0 Imports

**Files:**
- Create: `src/loom/campaigns/operations.py`
- Create: `src/loom/campaigns/reducer.py`
- Create: `src/loom/campaigns/store.py`
- Create: `src/loom/campaigns/history.py`
- Create: `src/loom/campaigns/workspace.py`
- Create: `src/loom/campaigns/cli.py`
- Create: `tests/campaigns/test_store.py`
- Create: `tests/campaigns/test_reducer.py`
- Create: `tests/campaigns/test_imports.py`
- Create: `tests/campaigns/test_cli.py`

**Interfaces:**
- `SQLiteCampaignStore.create/load/transact/reconcile/export/rebuild` implements append-only events, aggregate versions, operation leases, budget reservations, and projections in WAL mode.
- `reduce_campaign(events) -> CampaignProjection` is deterministic and owns lifecycle transitions.
- `import_experience()` verifies and publishes existing Evaluation/Evolution bundles and research artifacts after visibility sanitization; imported files are never rewritten.
- `CandidateWorkspace` allocates a new bounded directory and rejects escaping writes.
- CLI supports Phase 0 `create`, `derive`, `import-experience`, `status`, `pause`, `resume`, `abort`, and manual candidate `create`/`import`, with stable `--json` output and operation replay.

- [x] **Step 1: Write failing reducer/store tests** for lifecycle transitions, expected-version conflicts, same-ID/different-input rejection, operation replay, concurrent budget reservation, lease reconciliation, export/rebuild equivalence, and corrupt referenced artifacts freezing the campaign.
- [x] **Step 2: Write failing import/workspace/CLI tests** for Evaluation/Evolution compatibility, sanitizer labels, bounded writes, immutable derived specs, and safe JSON command replay.
- [x] **Step 3: Run the four focused test files and verify RED.**
- [x] **Step 4: Implement schema/migrations, reducer, store transactions, imports, workspace, and Phase 0 CLI.**
- [x] **Step 5: Re-run focused tests plus `uv run pytest tests/evaluation tests/evolution -q`.**

### Task 3: Task-Set Isolation, Declarative Patches, and Candidate Validation

**Files:**
- Create: `src/loom/evaluation/experiments.py`
- Create: `src/loom/campaigns/task_sets.py`
- Create: `src/loom/campaigns/materialization.py`
- Create: `src/loom/campaigns/validation.py`
- Create: `tests/evaluation/test_experiment_contracts.py`
- Create: `tests/campaigns/test_task_sets.py`
- Create: `tests/campaigns/test_materialization.py`
- Create: `tests/campaigns/test_validation.py`

**Interfaces:**
- Neutral `RunArtifactRef`, `ExperimentFailure`, `PairedMetricResult`, `ExperimentBundle`, and `ExperimentRunner` contracts live in `loom.evaluation.experiments`.
- `fingerprint_task_set()` records normalized SHA-256, snapshot digest, template lineage, sanitizer version, and token-shingle MinHash.
- `validate_task_set_isolation()` rejects cross-split exact matches and estimated Jaccard >= 0.80.
- `DeclarativePatchCompiler.compile()` validates allowlisted operations against a typed surface registry and returns a normal `MutationBundle`, canonical materialized artifact, and tested exact inverse.
- `DefaultCandidateValidator.validate()` runs manifest, patch/source, interface, and capability stages from cheapest to most expensive.

- [x] **Step 1: Write failing tests** for neutral import direction, fingerprints/near duplicates/provenance, editable/forbidden surfaces, unknown/ambiguous/non-invertible patch operations, exact inverse restoration, falsifiable hypotheses, static source/capability checks, and early rejection before runner invocation.
- [x] **Step 2: Run the focused tests and verify RED.**
- [x] **Step 3: Implement experiment contracts, task-set validation, patch compiler, and staged validator.**
- [x] **Step 4: Re-run focused tests and dependency-cycle smoke imports.**

### Task 4: Paired Experiments, Budgets, Frontier, and One-Time Finalization

**Files:**
- Create: `src/loom/campaigns/experiments.py`
- Create: `src/loom/campaigns/frontier.py`
- Create: `src/loom/campaigns/controller.py`
- Create: `src/loom/campaigns/reports.py`
- Create: `tests/campaigns/test_experiments.py`
- Create: `tests/campaigns/test_frontier.py`
- Create: `tests/campaigns/test_controller.py`
- Create: `tests/integration/test_governed_campaign_declarative.py`
- Modify: `src/loom/evolution/mutations.py`

**Interfaces:**
- `PairedExperimentRunner` freezes ordered trial entries, counterbalances execution, distinguishes infrastructure from candidate failures, reuses only exact-digest baseline cache entries, and persists normal Loom evaluation references.
- Paired aggregation implements fixed retry replacement, fail-closed missing values, and deterministic 10,000-resample paired percentile bootstrap for >=5 valid pairs.
- `DefaultFrontierPolicy.update()` applies hard gates and conservative interval dominance with append-only snapshots.
- `CampaignController` admits/evaluates candidates, enforces phase budgets, pauses/resumes, seals discovery, selects validation entrants/finalists deterministically, and unseals holdout once.
- `PromotionRecommendation` JSON plus Markdown view is non-activating in Phase 1.
- Existing `run_shadow_evaluation()` delegates through a compatibility adapter without introducing an import cycle.

- [x] **Step 1: Write failing experiment/frontier tests** for paired digests/order, retry semantics, interval boundaries, NaN/missing handling, no optional stopping, phase reservation/no borrowing, dominance/ties/exclusions, and deterministic snapshots.
- [x] **Step 2: Write failing controller/integration tests** for one invalid plus one evaluated manual candidate, pause/resume, seal barrier, deterministic entrants/finalists, one-time holdout, hidden export filtering, event replay, and non-activating recommendation.
- [x] **Step 3: Run focused tests and verify RED.**
- [x] **Step 4: Implement paired execution, aggregation, frontier, controller/finalization, reports, and shadow shim.**
- [x] **Step 5: Re-run focused and all campaign/evaluation/evolution tests.**

### Task 5: Bounded Proposer and Read-Only History Search

**Files:**
- Create: `src/loom/campaigns/proposer.py`
- Create: `tests/campaigns/test_history.py`
- Create: `tests/campaigns/test_proposer.py`
- Create: `tests/integration/test_governed_campaign_proposer.py`
- Modify: `src/loom/campaigns/controller.py`
- Modify: `src/loom/campaigns/cli.py`

**Interfaces:**
- `ProposerAdapter.propose(request, history, workspace) -> Result[ProposalBatch]` is vendor-neutral; the batch carries supervisor-measured usage separately from candidate-controlled draft JSON.
- `SubprocessProposerAdapter` starts/cancels a configured coding-agent command with an empty inherited environment, bounded workspace, session logging, timeout, and typed draft output.
- `CampaignHistory` exposes policy-filtered status/frontier/candidate/diff/experiment/finding/evidence/trace/failure queries and records every query.
- `InferenceBroker` protocol owns model credentials, egress, metering, and frozen request constraints; sandboxes receive no provider configuration.
- Controller supports deterministic multi-iteration search, duplicate rejection, proposer token/cost accounting, restart-safe resume, and no validation/holdout feedback.

- [x] **Step 1: Write failing history tests** for visibility policy, untrusted-evidence labels, secret removal, query audit, protected split fields, and prompt-injection text lacking authority.
- [x] **Step 2: Write failing proposer/controller tests** for malformed/empty/excess/duplicate drafts, session persistence, cancellation/timeout, empty environment, inaccessible evaluator/lifecycle APIs, fixed fake-proposer restart determinism, and all budget ceilings.
- [x] **Step 3: Run focused tests and verify RED.**
- [x] **Step 4: Implement history service/CLI, proposer protocol/adapter, session artifacts, controller loop, and budgets.**
- [x] **Step 5: Re-run focused and integration tests.**

### Task 6: Executable Candidate Validation and Fail-Closed Isolation

**Files:**
- Create: `src/loom/campaigns/sandbox.py`
- Create: `src/loom/campaigns/components.py`
- Create: `tests/campaigns/test_sandbox.py`
- Create: `tests/campaigns/test_components.py`
- Create: `tests/security/test_executable_candidates.py`
- Modify: `src/loom/campaigns/validation.py`

**Interfaces:**
- `IsolationBackend` reports enforceable capabilities; `RootlessContainerSandbox` is the only executable runner admitted by default policy.
- `UnsupportedIsolationBackend` rejects executable evaluation with `SANDBOX_UNAVAILABLE`; it never falls back to subprocess execution.
- `CandidateComponentRegistry` maps versioned narrow component protocols to read-only experiment adapters; installation is not exposed through campaigns.
- Static and runtime validation enforce imports, paths, network/subprocess/reflection/native-extension declarations, framed output, size/resource limits, fresh trial scratch, and supervisor-only inference.

- [x] **Step 1: Write failing platform/component tests** for capability matching, protocol adapters, fresh roots, cancellation/output framing, and unsupported-platform rejection.
- [x] **Step 2: Write failing security tests** for traversal/symlinks, `.env`/credentials, network, installs, subprocess/fork attempts, output/disk exhaustion, evaluator tampering, hidden strings, cross-task caches, false declarations, and broker override attempts.
- [x] **Step 3: Run focused tests and verify RED.**
- [x] **Step 4: Implement static enforcement, component adapters, isolation backend contract, and fail-closed platform behavior.**
- [x] **Step 5: Re-run focused tests; keep real container tests opt-in behind an explicit environment flag.**

### Task 7: Deterministic Governance, Atomic Promotion, Monitoring, and Rollback

**Files:**
- Create: `src/loom/governance/__init__.py`
- Create: `src/loom/governance/policy.py`
- Create: `src/loom/governance/risk.py`
- Create: `src/loom/governance/gates.py`
- Create: `src/loom/governance/registry.py`
- Create: `src/loom/governance/promotion.py`
- Create: `src/loom/governance/monitor.py`
- Create: `src/loom/governance/rollback.py`
- Create: `src/loom/governance/cli.py`
- Create: `tests/governance/test_risk.py`
- Create: `tests/governance/test_gates.py`
- Create: `tests/governance/test_registry.py`
- Create: `tests/governance/test_promotion.py`
- Create: `tests/governance/test_monitor.py`
- Create: `tests/governance/test_cli.py`
- Create: `tests/security/test_governance_authorization.py`

**Interfaces:**
- `classify_risk()` computes highest matched rule and never trusts candidate-declared risk.
- `evaluate_gates()` distinguishes mandatory pass/fail/insufficient evidence from `review_required`; mandatory failures cannot be overridden.
- `SQLiteGovernanceStore.active/record_review/acquire_lease/activate/rollback/reconcile` persists decisions/events/monitor state and active-pointer CAS atomically.
- `IdentityProvider` validates signed actor assertions; RBAC and separation-of-duty conflicts are hard denials inside protected transactions.
- `PromotionController` only auto-promotes reversible low-risk declarative candidates with fresh evidence, verified inverse, healthy monitoring, and all mandatory gates.
- Monitoring is version/watermark bound; stale monitors close, unhealthy heartbeats roll back eligible auto-promotions, TTL can expire, and rollback restores the exact prior digest.

- [x] **Step 1: Write failing risk/gate tests** for every default surface/rule, prompt security sensitivity, interval gates, required evidence, mandatory override denial, executable approval, and drift-invalidated approval.
- [x] **Step 2: Write failing store/promotion/monitor/security tests** for active CAS, transaction crash points, operation replay, monitor registration health, stale rollback race, heartbeat loss, TTL, exact rollback digest, RBAC action matrix, revoked assertions, and separation-of-duty conflicts.
- [x] **Step 3: Run focused tests and verify RED.**
- [x] **Step 4: Implement governance policy/risk/gates/store/controller/monitor/rollback and CLI.**
- [x] **Step 5: Re-run all governance/security tests.**

### Task 8: Full Governed Lifecycle, Public Exports, and Documentation

**Files:**
- Create: `tests/integration/test_governed_meta_harness_lifecycle.py`
- Modify: `src/loom/campaigns/__init__.py`
- Modify: `src/loom/governance/__init__.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-18-governed-meta-harness-implementation.md`

**Scenario:**

```text
baseline -> two candidates -> one invalid -> one evaluated -> frontier
-> pause/resume -> validation -> holdout -> low-risk promotion
-> monitoring regression -> atomic rollback -> event/artifact rebuild
```

- [x] **Step 1: Write the failing end-to-end deterministic lifecycle test** including exact active digest, immutable failed candidate history, hidden-set non-disclosure, and byte-equivalent campaign rebuild.
- [x] **Step 2: Run it and verify RED.**
- [x] **Step 3: Add only the integration adapters and public exports needed to make it pass.**
- [x] **Step 4: Document campaign configuration, CLI, authority boundaries, platform limitations, and Phase 5 deferral in README.**
- [x] **Step 5: Run focused tests, then `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pytest -q`, and `uv build`.**
- [x] **Step 6: Review the normative design acceptance criteria line by line and record any intentional deferral explicitly.**
- [x] **Step 7: Commit the complete implementation on `feat/governed-meta-harness` without merging to `main`.**

## Implementation Result

Phases 0–4 are implemented on `feat/governed-meta-harness`. Phase 5 remains the
only intentional product-scope deferral: cross-campaign transfer, distributed
execution, lineage analytics, and campaign-scale optimization are not included.
The production executable path requires a live rootless Podman profile with a
digest-pinned image and explicit seccomp/cgroup controls; unsupported hosts fail
closed and continue to support declarative candidates only.

The final feature-file format check, full Ruff lint, deterministic test suite,
and package build pass. The repository-wide format check still reports four
pre-existing, unrelated files (`evaluation/assessments.py`,
`evaluation/judge.py`, `tasks/profiles.py`, and `tests/evaluation/test_judge.py`),
which this branch intentionally leaves untouched.
