# One-Command Meta-Harness Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resumable `loom optimize` command that transforms a seed trace and simple task corpus into a governed Meta-Harness campaign outcome through Loom's existing APIs.

**Architecture:** Add a leaf `loom.optimize` package containing strict configuration, task preparation, transactional orchestration state, native proposer/trial adapters, governance composition, reporting, and CLI observers. Extend only the narrow existing seams needed to apply declarative harness overlays and dotted surface IDs; campaign, experiment, evaluation, evolution, and governance stores remain authoritative for their domains.

**Tech Stack:** Python 3.11 immutable dataclasses, `Result`/`LoomError`, SQLite, JSON/JSONL, existing Loom task/evaluation/evolution/campaign/governance APIs, pytest/pytest-asyncio, optional Textual TUI.

## Global Constraints

- The approved specification is `docs/superpowers/specs/2026-07-19-meta-harness-optimize-command-design.md`.
- The governed base specification is `docs/superpowers/specs/2026-07-18-governed-meta-harness-design.md`; its stricter evidence, authorization, sandbox, and holdout rule wins.
- `loom.optimize` may import public lower-layer APIs; lower layers must not import `loom.optimize`.
- The orchestrator calls Python APIs, never parses Loom CLI output.
- Only `max_completion_tokens` is supported; non-stream calls never send `stream_options`.
- Default candidate generation is declarative-only. Executable candidates remain opt-in, sandboxed, and approval-required.
- User workspaces are snapshotted before trials; baseline and candidate receive fresh materializations of the same snapshot.
- Holdout content is unavailable to proposer/search code and cannot be reused after disclosure.
- All mutating domain actions retain actor separation and immutable evidence references.
- Every task follows red-green-refactor and ends with a focused commit.

---

## File Structure

New production files:

- `src/loom/optimize/__init__.py`: public optimization exports.
- `src/loom/optimize/contracts.py`: immutable configuration-independent orchestration contracts and enums.
- `src/loom/optimize/config.py`: strict `meta_harness` parsing and resolved campaign policy inputs.
- `src/loom/optimize/task_sets.py`: simple task parsing, workspace snapshots, lineage groups, deterministic split, and internal manifests.
- `src/loom/optimize/store.py`: transactional stage events, leases, replay, projections, and exports.
- `src/loom/optimize/proposer.py`: native chat proposer that writes bounded declarative candidate drafts.
- `src/loom/optimize/trial_executor.py`: fresh workspace materialization, task execution, verifier, evaluation, and `TrialExecution` adaptation.
- `src/loom/optimize/governance.py`: local role bootstrap, policy/rule evidence, approval lookup, promotion, and monitor registration.
- `src/loom/optimize/orchestrator.py`: end-to-end stage machine and composition of existing controllers.
- `src/loom/optimize/reporting.py`: stable `result.json`, `optimization.json`, `events.jsonl`, and `report.md` exports.
- `src/loom/optimize/ui.py`: shared observer protocol, text/JSON observers, and optional TUI adapter.
- `src/loom/optimize/cli.py`: run/status/approve parsing, execution, exit codes, and resume routing.

Existing production files changed:

- `src/loom/cli.py`: dispatch `optimize`.
- `src/loom/llm/api.py`: request-mode filtering for `stream_options`.
- `src/loom/tasks/request.py`: typed `TaskHarness` overlay.
- `src/loom/tasks/runner.py`: apply prompt/tool/loop/request-option overlays.
- `src/loom/campaigns/materialization.py`: resolve dotted surface IDs safely.
- `src/loom/governance/promotion.py`: derive dotted surface IDs from governed patch paths.
- `src/loom/governance/risk.py`: classify approved optimize surfaces deterministically.
- `src/loom/campaigns/proposer.py`: expose the existing candidate-draft mapping parser as a public helper.
- `src/loom/campaigns/__init__.py`: export the public parser if package exports are curated.
- `README.md`: document the primary and dry-run workflows.
- `config.yaml`: add the local `meta_harness` example without committing credentials.

New tests:

- `tests/optimize/__init__.py`
- `tests/optimize/test_config.py`
- `tests/optimize/test_task_sets.py`
- `tests/optimize/test_store.py`
- `tests/optimize/test_proposer.py`
- `tests/optimize/test_trial_executor.py`
- `tests/optimize/test_governance.py`
- `tests/optimize/test_orchestrator.py`
- `tests/optimize/test_cli.py`
- `tests/integration/test_optimize_end_to_end.py`

Existing tests changed:

- `tests/llm/test_llm.py`
- `tests/tasks/test_task_runner.py`
- `tests/campaigns/test_materialization.py`
- `tests/governance/test_risk.py`
- `tests/governance/test_promotion.py`
- `tests/test_package_structure.py`

---

### Task 1: Make Provider Request Options Safe for Optimization Roles

**Files:**
- Modify: `src/loom/llm/api.py:730-805`
- Test: `tests/llm/test_llm.py`

**Interfaces:**
- Consumes: `OpenAIProvider.request_options` produced by task configuration.
- Produces: `OpenAIProvider._request_body(..., stream: bool)` that omits `stream_options` for non-stream calls and retains it for stream calls.

- [ ] **Step 1: Write failing request-body tests**

Add tests that use a recording HTTP client and assert exact bodies:

```python
def test_openai_provider_omits_stream_options_for_non_stream_chat():
    provider = create_openai_provider(
        api_key="secret",
        model="judge",
        request_options={"reasoning_effort": "high", "stream_options": {"include_usage": True}},
    )
    body = provider._request_body((LlmMessage("user", "judge"),), None, None, stream=False)
    assert body["reasoning_effort"] == "high"
    assert "stream_options" not in body


def test_openai_provider_keeps_stream_options_for_stream_chat():
    provider = create_openai_provider(
        api_key="secret",
        model="solver",
        request_options={"stream_options": {"include_usage": True}},
    )
    body = provider._request_body((LlmMessage("user", "solve"),), None, None, stream=True)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
```

- [ ] **Step 2: Run the focused tests and confirm the non-stream assertion fails**

Run: `uv run pytest tests/llm/test_llm.py -k stream_options -q`

Expected: the non-stream test fails because `_request_body` currently merges `stream_options` unconditionally.

- [ ] **Step 3: Filter only the mode-incompatible option**

In `_request_body`, materialize into a local mapping and remove `stream_options` only when `stream` is false:

```python
options = materialize_request_options(self.request_options)
if not stream:
    options.pop("stream_options", None)
body.update(options)
```

Do not remove `reasoning_effort`, `parallel_tool_calls`, or other provider-specific options.

- [ ] **Step 4: Run provider and task-configuration tests**

Run: `uv run pytest tests/llm/test_llm.py tests/tasks/test_task_config.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "fix: scope stream options to streaming requests"
```

### Task 2: Add Immutable Optimize Contracts and Strict Configuration

**Files:**
- Create: `src/loom/optimize/__init__.py`
- Create: `src/loom/optimize/contracts.py`
- Create: `src/loom/optimize/config.py`
- Create: `tests/optimize/__init__.py`
- Create: `tests/optimize/test_config.py`

**Interfaces:**
- Consumes: `parse_yaml_document()`, `load_task_config()`, configured model names, paths supplied by CLI.
- Produces: `load_optimize_config(path) -> Result[LoadedOptimizeConfig]`, `OptimizationSpec`, `OptimizationStage`, `OptimizationLifecycle`, `OptimizationResult`.

- [ ] **Step 1: Write failing strict-config tests**

Cover defaults, model references, split validation, unknown fields, `reasoning_effort`, executable policy, and canonical identity inputs:

```python
def test_load_optimize_config_resolves_meta_harness_defaults(config_file):
    loaded = load_optimize_config(config_file).unwrap()
    assert loaded.meta.proposer_model == "main"
    assert loaded.meta.tasks.split == SplitRatios(Decimal("0.60"), Decimal("0.20"), Decimal("0.20"))
    assert loaded.meta.tasks.repetitions == 3
    assert loaded.meta.governance.auto_promote_max_risk == "low"


def test_load_optimize_config_rejects_unknown_meta_harness_field(config_file):
    config_file.write_text(valid_config() + "\n  surprise: true\n", encoding="utf-8")
    result = load_optimize_config(config_file)
    assert result.error.code == "OPTIMIZE_CONFIG_INVALID"


def test_load_optimize_config_rejects_invalid_reasoning_effort(config_file):
    config_file.write_text(valid_config(reasoning_effort="max"), encoding="utf-8")
    result = load_optimize_config(config_file)
    assert result.error.metadata["field"] == "models.kimi.request_options.reasoning_effort"
```

- [ ] **Step 2: Run config tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_config.py -q`

Expected: collection fails because `loom.optimize.config` does not exist.

- [ ] **Step 3: Implement immutable config values**

Define frozen, slotted values for:

```python
class OptimizationStage(StrEnum):
    CREATED = "created"
    PREFLIGHT_COMPLETE = "preflight_complete"
    SEED_ANALYSIS_COMPLETE = "seed_analysis_complete"
    CAMPAIGN_INITIALIZED = "campaign_initialized"
    SEARCH_RUNNING = "search_running"
    SEARCH_SEALED = "search_sealed"
    VALIDATION_COMPLETE = "validation_complete"
    FINALISTS_SELECTED = "finalists_selected"
    HOLDOUT_COMPLETE = "holdout_complete"
    GOVERNANCE_COMPLETE = "governance_complete"


class OptimizationLifecycle(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"
```

Add `SplitRatios`, `SearchConfig`, `TaskSetConfig`, `ObjectiveConfig`,
`BudgetConfig`, `ExecutionConfig`, `GovernanceConfig`, `MetaHarnessConfig`,
`LoadedOptimizeConfig`, `OptimizationSpec`, `OptimizationState`, and
`OptimizationResult`. Normalize tuple/mapping fields in `__post_init__`.

- [ ] **Step 4: Implement strict parsing and validation**

`load_optimize_config()` must parse the YAML once, call `load_task_config()` for
the existing model/task settings, require `meta_harness`, reject unknown nested
keys, verify referenced models, and validate:

```python
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
_CANDIDATE_KINDS = frozenset({"declarative_patch", "executable_component"})
_RISK_LEVELS = frozenset({"low", "medium", "high"})
```

Use `Decimal(str(value))` for split and cost values. Require ratios to sum to
`Decimal("1")`, positive limits, `auto_promote_max_risk == "low"`, and approval
for executable candidates. Return `OPTIMIZE_CONFIG_INVALID` with a stable
`metadata.field` for validation failures.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/optimize/test_config.py tests/tasks/test_task_config.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/loom/optimize tests/optimize
git commit -m "feat: add optimize contracts and configuration"
```

### Task 3: Prepare Simple Tasks, Snapshots, and Isolated Sets

**Files:**
- Create: `src/loom/optimize/task_sets.py`
- Create: `tests/optimize/test_task_sets.py`

**Interfaces:**
- Consumes: a simple task JSONL path, `TaskSetConfig`, optimize input directory, authenticated owner.
- Produces: `prepare_task_sets(...) -> Result[PreparedTaskSets]`, internal manifest files and snapshot references; `publish_prepared_task_sets(prepared, artifact_store) -> Result[PublishedTaskSets]` produces the `TaskSetRef` values used by `CampaignSpec`.

- [ ] **Step 1: Write failing parser, snapshot, split, and contamination tests**

Use real temporary workspaces and assert:

```python
def test_load_simple_tasks_rejects_unknown_fields(tmp_path):
    source = write_tasks(tmp_path, [{"task_id": "a", "objective": "Audit", "workspace": "project", "objctive": "typo"}])
    result = load_simple_tasks(source)
    assert result.error.code == "TASK_SET_INVALID"


def test_prepare_task_sets_is_stable_and_keeps_snapshot_groups_together(tmp_path):
    source = independent_task_corpus(tmp_path, count=9)
    first = prepare_task_sets(source, config=task_config(seed=42), output_dir=tmp_path / "one", owner="tester").unwrap()
    second = prepare_task_sets(source, config=task_config(seed=42), output_dir=tmp_path / "two", owner="tester").unwrap()
    assert first.fingerprint_digests == second.fingerprint_digests
    assert roles_for_snapshot(first).values() == roles_for_snapshot(second).values()


def test_prepare_task_sets_rejects_one_project(tmp_path):
    source = same_workspace_tasks(tmp_path, count=6)
    result = prepare_task_sets(source, config=task_config(), output_dir=tmp_path / "out", owner="tester")
    assert result.error.code == "TASK_SPLIT_INSUFFICIENT"
```

Also test duplicate IDs, symlinks, missing workspace, invalid verifier, exact
content overlap, common structural lineage, explicit three-set mode, and seed
trace lineage exclusion.

- [ ] **Step 2: Run task-set tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_task_sets.py -q`

Expected: collection fails because `loom.optimize.task_sets` does not exist.

- [ ] **Step 3: Implement simple task and verifier contracts**

Define:

```python
@dataclass(frozen=True, slots=True)
class VerifierSpec:
    argv: tuple[str, ...]
    timeout_ms: int = 120_000
    expected_exit_code: int = 0


@dataclass(frozen=True, slots=True)
class OptimizeTask:
    task_id: str
    objective: str
    workspace: Path
    profile: str = "auto"
    constraints: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    risk_level: str = "auto"
    verifier: VerifierSpec | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict)
```

Reject empty IDs/objectives/argv, shell-string verifiers, duplicate normalized
IDs, unknown fields, missing directories, and symlinks that escape a workspace.

- [ ] **Step 4: Implement content-addressed workspace snapshots**

Walk entries in sorted POSIX-relative order, skip only `.git` and `.loom`,
reject special files and escaping symlinks, and hash file mode plus bytes. Copy
each unique tree once to `inputs/workspaces/<sha256>` with a temporary sibling
and atomic rename. Store task rows against the immutable snapshot path.

- [ ] **Step 5: Implement conservative lineage and deterministic splitting**

Create connected components where any tasks share snapshot digest or structural
lineage. Structural lineage hashes profile, normalized constraints, expected
outputs, verifier shape, and an objective with workspace/project identifiers
redacted. Order components by `sha256(f"{seed}:{component_digest}")`, assign
whole components toward ratio targets, and require each set to contain enough
task repetitions for `minimum_pairs`.

Generate `TaskManifestRow` values, write canonical JSONL, call
`fingerprint_task_rows()` and `validate_task_set_isolation()`, and return a
lookup from `canonical_digest(TaskFingerprint)` to prepared task for trial
execution. `publish_prepared_task_sets()` publishes the three manifests through
the supplied campaign artifact store and binds their exact fingerprint digests
into `TaskSetRef` values.

- [ ] **Step 6: Run focused and existing task-set tests**

Run: `uv run pytest tests/optimize/test_task_sets.py tests/campaigns/test_task_sets.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/loom/optimize/task_sets.py tests/optimize/test_task_sets.py
git commit -m "feat: prepare isolated optimize task sets"
```

### Task 4: Add the Transactional Optimization Store

**Files:**
- Create: `src/loom/optimize/store.py`
- Create: `tests/optimize/test_store.py`

**Interfaces:**
- Consumes: `OptimizationSpec`, stable operation IDs/input digests, stage transitions, and JSON payloads.
- Produces: `SQLiteOptimizationStore.create/load/events/begin/complete/fail/pause/resume/export` with replay and lease semantics.

- [ ] **Step 1: Write failing lifecycle, replay, and lease tests**

```python
@pytest.mark.asyncio
async def test_store_replays_completed_operation_without_advancing_version(tmp_path, spec):
    store = SQLiteOptimizationStore(tmp_path)
    created = (await store.create(spec)).unwrap()
    lease = (await store.begin(spec.optimization_id, "op-preflight", "input-a", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()
    completed = (await store.complete(lease.lease_id, {"task_manifest": "sha256:a"})).unwrap()
    replay = (await store.begin(spec.optimization_id, "op-preflight", "input-a", OptimizationStage.PREFLIGHT_COMPLETE)).unwrap()
    assert replay.replayed is True
    assert replay.aggregate_version == completed.aggregate_version
    assert created.aggregate_version == 1


@pytest.mark.asyncio
async def test_store_rejects_operation_id_with_changed_input(tmp_path, spec):
    store = SQLiteOptimizationStore(tmp_path)
    await store.create(spec)
    await store.begin(spec.optimization_id, "op", "first", OptimizationStage.PREFLIGHT_COMPLETE)
    result = await store.begin(spec.optimization_id, "op", "changed", OptimizationStage.PREFLIGHT_COMPLETE)
    assert result.error.code == "OPERATION_CONFLICT"
```

Also test illegal stage transitions, expired lease reconciliation, pause/resume,
terminal state replay, event hash-chain rebuilding, and concurrent expected
version conflicts.

- [ ] **Step 2: Run store tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_store.py -q`

Expected: collection fails because `loom.optimize.store` does not exist.

- [ ] **Step 3: Implement the SQLite schema and reducer**

Use WAL mode and tables for one optimization manifest, ordered events,
operations, and leases. An event includes sequence, aggregate version,
event type, stage, lifecycle, operation ID, canonical payload, previous hash,
and event hash. Validate transitions with an explicit mapping; do not update a
mutable stage column without a corresponding event.

- [ ] **Step 4: Implement idempotent operations**

`begin()` must atomically check expected state, insert a started operation,
create a lease, and write `optimization.stage_started`. `complete()` must bind
the same lease, publish output payload, write `optimization.stage_completed`,
and close the operation. Exact replay returns the stored completion; changed
input returns `OPERATION_CONFLICT`.

- [ ] **Step 5: Implement projections and reproducible exports**

`load()` reduces verified events into `OptimizationState`. `export()` writes
`optimization.json` and `events.jsonl` through temporary files, fsync, and
atomic rename. Hash mismatch returns `OPTIMIZATION_INTEGRITY_FAILED`.

- [ ] **Step 6: Run store tests**

Run: `uv run pytest tests/optimize/test_store.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/loom/optimize/store.py tests/optimize/test_store.py
git commit -m "feat: add resumable optimize store"
```

### Task 5: Make Declarative Harness Surfaces Executable

**Files:**
- Modify: `src/loom/tasks/request.py`
- Modify: `src/loom/tasks/runner.py`
- Modify: `src/loom/campaigns/materialization.py`
- Modify: `src/loom/governance/promotion.py`
- Modify: `src/loom/governance/risk.py`
- Test: `tests/tasks/test_task_runner.py`
- Test: `tests/campaigns/test_materialization.py`
- Test: `tests/governance/test_risk.py`
- Test: `tests/governance/test_promotion.py`

**Interfaces:**
- Consumes: a compiled materialization containing `agent.system_prompt`, `agent.tool_policy`, `agent.loop_policy`, or `models.solver.request_options`.
- Produces: `TaskHarness`, dotted-surface patch compilation/risk derivation, and a task runner whose behavior is actually changed by the overlay.

- [ ] **Step 1: Write failing dotted-surface and runtime-overlay tests**

```python
def test_compiler_resolves_longest_dotted_surface_prefix():
    compiler = DeclarativePatchCompiler((SurfaceDefinition("agent.loop_policy", ("max_tool_calls_per_step",), {"max_tool_calls_per_step": int}, {"max_tool_calls_per_step": (1, 64)}),))
    result = compiler.compile(
        {"agent.loop_policy": {"max_tool_calls_per_step": 32}},
        ({"op": "set_limit", "path": "agent.loop_policy.max_tool_calls_per_step", "value": 8},),
        dotted_policy(),
        evidence_trace_ids=("trace-1",),
    ).unwrap()
    assert result.materialized["agent.loop_policy"]["max_tool_calls_per_step"] == 8


def test_task_harness_limits_available_tools(tmp_path):
    request = TaskRequest("Audit", workspace=tmp_path)
    harness = TaskHarness(allowed_tools=("read_file",), max_tool_calls_per_step=4)
    context = make_task_context(request, harness=harness).unwrap()
    assert tuple(tool.id for tool in context.affordances.tools) == ("read_file",)
```

Add an async runner test asserting provider request options are merged without
changing model credentials, and governance tests asserting dotted surface
extraction plus low/medium classification.

- [ ] **Step 2: Run focused tests and confirm failures**

Run: `uv run pytest tests/tasks/test_task_runner.py tests/campaigns/test_materialization.py tests/governance/test_risk.py tests/governance/test_promotion.py -q`

Expected: new tests fail because dotted paths and `TaskHarness` are unsupported.

- [ ] **Step 3: Add `TaskHarness` and apply it consistently**

Define:

```python
@dataclass(frozen=True, slots=True)
class TaskHarness:
    system_prompt_addendum: str = ""
    allowed_tools: tuple[str, ...] | None = None
    max_tool_calls_per_step: int | None = None
    max_history_steps: int = 5
    request_options: Mapping[str, Any] = field(default_factory=dict)
```

Validate positive limits and freeze request options. Pass `harness` through
`make_task_context`, `make_task_loop`, and `run_generic_task`; append the prompt
addendum as an explicit must-constraint, filter both tool references and tool
handlers to the same allowlist, pass loop/history limits to
`create_llm_step_function`, and clone the provider with merged non-secret
request options using `dataclasses.replace`.

- [ ] **Step 4: Resolve patch surfaces by admitted longest prefix**

In the compiler, replace the two-segment assumption with a helper that matches
exactly one configured surface prefix and one field suffix. Use the same helper
shape in governance candidate-risk derivation. Reject missing fields,
ambiguous prefixes, or paths not covered by the materialized mapping.

- [ ] **Step 5: Extend deterministic risk rules**

Classify `agent.loop_policy` as low only because compiler numeric bounds apply;
classify `agent.system_prompt`, `agent.tool_policy`, and
`models.solver.request_options` as medium. Preserve forbidden and unknown
surface behavior.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/tasks/test_task_runner.py tests/campaigns/test_materialization.py tests/governance/test_risk.py tests/governance/test_promotion.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/loom/tasks src/loom/campaigns/materialization.py src/loom/governance/promotion.py src/loom/governance/risk.py tests/tasks/test_task_runner.py tests/campaigns/test_materialization.py tests/governance/test_risk.py tests/governance/test_promotion.py
git commit -m "feat: apply declarative task harness overlays"
```

### Task 6: Add the Loom-Native Proposer Adapter

**Files:**
- Modify: `src/loom/campaigns/proposer.py`
- Create: `src/loom/optimize/proposer.py`
- Create: `tests/optimize/test_proposer.py`

**Interfaces:**
- Consumes: existing `ProposerAdapter` request/history/workspace, a configured chat provider, frozen baseline harness, and seed evidence refs.
- Produces: `LoomNativeProposerAdapter.propose(...) -> Result[ProposalBatch]` with measured token usage and candidate files confined to `CandidateWorkspace`.

- [ ] **Step 1: Write failing proposer tests with a fake provider**

```python
@pytest.mark.asyncio
async def test_native_proposer_writes_bounded_single_surface_drafts(tmp_path, request, history):
    provider = FakeChatProvider(proposal_response())
    workspace = CandidateWorkspace.allocate(tmp_path, "iteration-1").unwrap()
    adapter = LoomNativeProposerAdapter(provider, baseline_harness(), evidence_refs=("trace:seed",))
    batch = (await adapter.propose(request, history, workspace)).unwrap()
    assert len(batch.drafts) == request.max_candidates
    assert all(len(draft.changed_surfaces) == 1 for draft in batch.drafts)
    assert all((workspace.root / draft.patch_path).is_file() for draft in batch.drafts)
    assert batch.usage.proposer_tokens == 42
```

Also test Markdown-fenced JSON, malformed/truncated responses, duplicate drafts,
forbidden surfaces, multiple surfaces per candidate, missing regression
prediction, path escape, and history query exclusion of validation/holdout.

- [ ] **Step 2: Run proposer tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_proposer.py -q`

Expected: collection fails because the adapter does not exist.

- [ ] **Step 3: Expose candidate-draft parsing without duplicating schema**

Rename `_draft_from_dict` to `candidate_draft_from_mapping`, update existing
adapters to call it, and export it. Existing proposer tests must remain green.

- [ ] **Step 4: Implement prompt, parsing, metering, and file publication**

The system prompt labels all history as untrusted evidence, lists exact
editable/forbidden surfaces, requires one changed surface per draft, and asks
for a JSON object with `drafts`. Query only allowlisted discovery history.
Parse the provider content using the same fenced/object extraction behavior as
Loom LLM parsing, inject trusted artifact/patch paths and seed evidence refs,
write canonical `draft-N/artifact.json` and `draft-N/patch.json`, then call
`candidate_draft_from_mapping()`.

Use `LlmResponse.usage.total_tokens` for `ProposalUsage.proposer_tokens`, a
configured zero cost when provider pricing is unavailable, and monotonic whole
seconds for wall time. Return `PROPOSAL_FAILED` with raw-response artifact path
metadata for malformed output.

- [ ] **Step 5: Run proposer suites**

Run: `uv run pytest tests/optimize/test_proposer.py tests/campaigns/test_proposer.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/loom/campaigns/proposer.py src/loom/optimize/proposer.py tests/optimize/test_proposer.py tests/campaigns/test_proposer.py
git commit -m "feat: add native meta-harness proposer"
```

### Task 7: Bridge Frozen Tasks to Paired Experiment Trials

**Files:**
- Create: `src/loom/optimize/trial_executor.py`
- Create: `tests/optimize/test_trial_executor.py`

**Interfaces:**
- Consumes: `PreparedTaskSets`, campaign artifact store, task/model config, candidate compiled overlays, and `TrialEntry`.
- Produces: callable `OptimizeTrialExecutor.execute(side, candidate, entry, trial_id) -> TrialExecution` for `PairedExperimentRunner`.

- [ ] **Step 1: Write failing baseline/candidate/verifier/retry tests**

```python
@pytest.mark.asyncio
async def test_trial_executor_materializes_fresh_paired_workspaces(executor, candidate, entry):
    baseline = await executor.execute("baseline", candidate, entry, "trial-1")
    changed = await executor.execute("candidate", candidate, entry, "trial-1")
    assert baseline.failure_kind is None
    assert changed.failure_kind is None
    assert baseline.metrics["task_success_rate"] == 1.0
    assert changed.metrics["task_success_rate"] == 1.0
    assert executor.workspace_for("trial-1", "baseline") != executor.workspace_for("trial-1", "candidate")


@pytest.mark.asyncio
async def test_verifier_failure_is_scored_behavior_not_infrastructure(executor, candidate, failing_verifier_entry):
    result = await executor.execute("candidate", candidate, failing_verifier_entry, "trial-fail")
    assert result.failure_kind is None
    assert result.metrics["task_success_rate"] == 0.0
```

Also assert infrastructure provider errors use `failure_kind="infrastructure"`,
trace/evaluation refs are published, token/duration metrics are finite, the
candidate overlay is absent from baseline, and verifier uses argv without a
shell.

- [ ] **Step 2: Run trial tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_trial_executor.py -q`

Expected: collection fails because `loom.optimize.trial_executor` does not exist.

- [ ] **Step 3: Implement task lookup and fresh workspace materialization**

Resolve `TrialEntry.task_fingerprint` through `PreparedTaskSets`, create a
filesystem-safe directory from `sha256(trial_id)`, copy the immutable snapshot
to separate `baseline` and `candidate` roots, and never reuse a dirty trial
directory.

- [ ] **Step 4: Execute task, verifier, evaluation, and metrics**

Call `run_generic_task()` with the solver model and candidate `TaskHarness` only
on the candidate side. Always allocate the trace path before execution. Run the
verifier with `asyncio.create_subprocess_exec`, the frozen workspace as cwd,
empty stdin, inherited minimal environment, and `asyncio.wait_for` timeout.

On a completed trace, call `evaluation.analyze_trace()` with the judge provider,
publish trace and evaluation-bundle bytes to the campaign artifact store, and
return metrics:

```python
{
    "task_success_rate": 1.0 if task_ok and verifier_ok else 0.0,
    "total_tokens": float(total_tokens),
    "wall_time_ms": float(duration_ms),
}
```

Behavioral failures retain evidence and a zero success metric so paired
experiments remain complete. Retryable provider/sandbox failures return
`TrialExecution.infrastructure_failure()`.

- [ ] **Step 5: Run trial and paired-experiment tests**

Run: `uv run pytest tests/optimize/test_trial_executor.py tests/campaigns/test_experiments.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/loom/optimize/trial_executor.py tests/optimize/test_trial_executor.py
git commit -m "feat: execute optimize paired trials"
```

### Task 8: Compose Campaign Search and Finalization

**Files:**
- Create: `src/loom/optimize/orchestrator.py`
- Create: `tests/optimize/test_orchestrator.py`

**Interfaces:**
- Consumes: resolved config, prepared task sets, optimize store, providers, actors, and existing campaign services.
- Produces: `OptimizeOrchestrator.run_campaign() -> Result[CampaignOutcome]` through seed analysis, search, validation, holdout, and recommendation; Task 9 adds the public governance-complete `run()` method.

- [ ] **Step 1: Write failing staged-orchestrator tests**

Use deterministic fake proposer/trial services and assert:

```python
@pytest.mark.asyncio
async def test_orchestrator_runs_seed_search_validation_and_holdout(services):
    outcome = (await services.orchestrator.run_campaign()).unwrap()
    state = (await services.store.load(outcome.optimization_id)).unwrap()
    assert state.stage is OptimizationStage.HOLDOUT_COMPLETE
    assert services.calls["seed_evaluation"] == 1
    assert services.calls["seed_evolution"] == 1
    assert services.calls["holdout"] == 1


@pytest.mark.asyncio
async def test_orchestrator_resume_does_not_repeat_completed_seed_calls(services):
    services.fail_once_at("search")
    await services.orchestrator.run_campaign()
    await services.orchestrator.run_campaign()
    assert services.calls["seed_evaluation"] == 1
    assert services.calls["seed_evolution"] == 1
```

Also test no-entrant rejection, no-finalist rejection, exact holdout cohort,
search sealing before holdout, stable operation IDs, and JSON observer event
order.

- [ ] **Step 2: Run orchestrator tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_orchestrator.py -q`

Expected: collection fails because the orchestrator does not exist.

- [ ] **Step 3: Implement preflight and seed-analysis stages**

Preflight resolves input digests, config, identities, snapshots, task sets, and
budget before `store.complete()`. Seed analysis calls
`evaluation.analyze_trace(EvaluationConfig(..., judge=True))`, then
`evolution.analyze_trace(AnalyzeConfig(evaluation_bundle_path=...))`. Publish
and record both artifact references.

- [ ] **Step 4: Build and start the existing campaign**

Construct `CampaignSpec` directly from resolved config and task refs. Publish
the baseline harness, configure `ObjectiveSpec` values for success, token cost,
and latency, create the campaign store with distinct actors, write
`campaign.started`, and import the evaluation/evolution bundles through
`import_experience()`.

- [ ] **Step 5: Run search iterations through `CampaignController`**

For every configured iteration call `run_iteration()` with
`LoomNativeProposerAdapter`, `CampaignHistory`, a new iteration workspace, and
an `evaluate_draft` closure that:

1. validates with `DefaultCandidateValidator` and dotted surface definitions;
2. publishes a full `CandidateBundle` artifact;
3. publishes validation evidence;
4. runs/publishes a discovery `ExperimentBundle` through
   `PairedExperimentRunner` when valid;
5. retains the compiled candidate overlay keyed by candidate ID.

- [ ] **Step 6: Seal, validate, select, and finalize once**

Call `seal_search()` with the current frontier digest. Evaluate only sealed
entrants on validation, publish `publish_phase_results()`, call
`select_finalists()`, evaluate only frozen finalists on holdout, publish the
holdout phase result, and call `finalize()` with the expected finalist digest.
If entrants/finalists are empty, emit a normal rejected result without opening
holdout.

- [ ] **Step 7: Run orchestrator and campaign tests**

Run: `uv run pytest tests/optimize/test_orchestrator.py tests/campaigns/test_controller.py tests/integration/test_governed_meta_harness_lifecycle.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/loom/optimize/orchestrator.py tests/optimize/test_orchestrator.py
git commit -m "feat: orchestrate meta-harness campaigns"
```

### Task 9: Compose Governance, Approval, and Reports

**Files:**
- Create: `src/loom/optimize/governance.py`
- Create: `src/loom/optimize/reporting.py`
- Create: `tests/optimize/test_governance.py`
- Modify: `src/loom/optimize/orchestrator.py`

**Interfaces:**
- Consumes: campaign recommendation, compiled candidate, task-set/experiment evidence, local or production actors, and optional approval.
- Produces: governed candidate, gate evidence, `PromotionDecision`, monitor registration, stable result/report exports, and resume after approval.

- [ ] **Step 1: Write failing promotion/rejection/approval/report tests**

```python
@pytest.mark.asyncio
async def test_low_risk_candidate_auto_promotes_and_registers_monitor(governance_fixture):
    result = (await governance_fixture.promote(surface="agent.loop_policy", approved=False)).unwrap()
    assert result.decision.value == "promoted"
    monitors = (await governance_fixture.store.monitors("active")).unwrap()
    assert len(monitors) == 1


@pytest.mark.asyncio
async def test_medium_risk_candidate_waits_for_explicit_approval(governance_fixture):
    first = (await governance_fixture.promote(surface="agent.system_prompt", approved=False)).unwrap()
    assert first.decision.value == "awaiting_approval"
    governance_fixture.record_approval(first)
    second = (await governance_fixture.promote(surface="agent.system_prompt", approved=True)).unwrap()
    assert second.decision.value == "promoted"
```

Also test separate-role authorization, policy/risk artifact authorization,
stale approval binding, rejected gates, active baseline freshness, report
redaction, and result replay.

- [ ] **Step 2: Run governance tests and confirm import failure**

Run: `uv run pytest tests/optimize/test_governance.py -q`

Expected: collection fails because the optimize governance composer does not exist.

- [ ] **Step 3: Implement local actor bootstrap and production resolution**

Create one `ActorAssertion` per required role with non-overlapping role tuples.
Local mode writes redacted public identity metadata under the optimize input
directory and builds `StaticIdentityProvider`; it never exposes the approver to
automatic promotion code. Production mode loads named assertion files from
environment references and fails before paid calls when any claim is missing or
over-broad.

- [ ] **Step 4: Publish governance inputs and call the existing controller**

Publish the governed candidate from compiled operations/materialization, the
fail-closed governance policy, default risk rules, candidate evidence,
controller gate-source evidence, and final gate evidence. Initialize the
changed surface to the exact baseline artifact once. Build `PromotionRequest`
and `MonitorRegistration`, then call `GovernedPromotionController.promote()`.

For an existing review, load the latest signed `ApprovalRecord` whose candidate,
baseline, policy, risk-rule, and gate digests match exactly. Never fabricate an
approval during `run()`.

- [ ] **Step 5: Implement stable result and Markdown exports**

`write_result()` writes canonical `result.json`; `render_report()` includes seed
artifacts, task digests, candidates/frontier, validation/holdout summary, risk,
gates, decision, monitor, usage, and next action. Redact secret-bearing fields
and hidden holdout content. Write with atomic replacement and export the
optimize store projection/events.

- [ ] **Step 6: Integrate governance as the final orchestrator stage**

Map `PromotionDisposition.PROMOTED`, `REJECTED`, and `AWAITING_APPROVAL` to the
matching optimization lifecycle and exit disposition. A promotion result must
contain an active monitor ref; rejected and awaiting results must not.

- [ ] **Step 7: Run governance, orchestrator, and existing governance tests**

Run: `uv run pytest tests/optimize/test_governance.py tests/optimize/test_orchestrator.py tests/governance -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/loom/optimize/governance.py src/loom/optimize/reporting.py src/loom/optimize/orchestrator.py tests/optimize/test_governance.py tests/optimize/test_orchestrator.py
git commit -m "feat: govern and report optimize outcomes"
```

### Task 10: Add CLI, Observers, Status, Approval, and Pause

**Files:**
- Create: `src/loom/optimize/ui.py`
- Create: `src/loom/optimize/cli.py`
- Modify: `src/loom/cli.py`
- Create: `tests/optimize/test_cli.py`
- Modify: `tests/test_package_structure.py`

**Interfaces:**
- Consumes: CLI argv, optimize config/tasks/trace, optimize store, orchestrator, and approval service.
- Produces: top-level `loom optimize`, stable JSON events, concise text/TUI observation, and exit codes 0/1/2/3.

- [ ] **Step 1: Write failing CLI parsing and dispatch tests**

```python
def test_parse_primary_optimize_command(tmp_path):
    args = parse_args(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--tui"])
    assert args.command == "run"
    assert args.tui is True
    assert args.json is False


def test_tui_and_json_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--tui", "--json"])


def test_top_level_cli_dispatches_optimize(monkeypatch):
    monkeypatch.setattr("loom.optimize.cli.main", lambda argv: 23)
    assert loom_main(["optimize", "status", "opt_123"]) == 23
```

Also test explicit sets, dry-run, status JSON, approval args, returned exit codes,
same-command resume lookup, and `TASK_SET_INVALID` output.

- [ ] **Step 2: Run CLI tests and confirm failures**

Run: `uv run pytest tests/optimize/test_cli.py tests/test_package_structure.py -q`

Expected: tests fail because optimize is not dispatched.

- [ ] **Step 3: Implement the shared observer model**

Define `OptimizationObserver.emit(event)`, `TextObserver`, and `JsonObserver`.
All receive the same versioned mapping emitted after committed stage events.
Implement `TuiObserver` as an optional Textual/Rich adapter over that stream;
when TUI dependencies are unavailable return `TUI_UNAVAILABLE` with installation
guidance. TUI selection must not change provider streaming.

- [ ] **Step 4: Implement run/status/approve routing**

The primary parser treats no subcommand as `run`; `status` and `approve` are
recognized supporting subcommands. Enforce task option exclusivity. `run`
loads config and inputs, derives or finds the optimization ID, performs dry-run
without model calls, installs signal handlers that request a graceful pause,
and runs the orchestrator.

`approve` resolves the stored evidence digests, authenticates only the approver
identity, records a `GovernanceReview`, and prints the original run command as
the resume action. It does not call promotion itself.

- [ ] **Step 5: Implement output and exit codes**

Return `0` for promoted/rejected, `2` for awaiting approval, `3` for paused,
and `1` for errors. JSON output includes the complete `OptimizationResult` or
structured `LoomError`. Human output prints ID, disposition, report path, and
next action.

- [ ] **Step 6: Run CLI/package tests**

Run: `uv run pytest tests/optimize/test_cli.py tests/test_package_structure.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/loom/optimize/ui.py src/loom/optimize/cli.py src/loom/cli.py tests/optimize/test_cli.py tests/test_package_structure.py
git commit -m "feat: add loom optimize command"
```

### Task 11: Prove the End-to-End Workflow and Document It

**Files:**
- Create: `tests/integration/test_optimize_end_to_end.py`
- Modify: `README.md`
- Modify locally: `config.yaml`

**Interfaces:**
- Consumes: all prior optimize components with deterministic fake providers and isolated fixture projects.
- Produces: a full trace-to-governance regression test and user-facing command/task examples.

- [ ] **Step 1: Write the deterministic end-to-end test**

Build at least nine independent fixture workspaces and a seed trace. Use fake
solver/judge/proposer providers, then invoke the real optimize CLI entry point:

```python
def test_optimize_command_runs_trace_to_governed_result(tmp_path, monkeypatch):
    trace, tasks, config = build_optimize_fixture(tmp_path, monkeypatch)
    exit_code = optimize_main([
        "--trace", str(trace),
        "--tasks", str(tasks),
        "--config", str(config),
        "--json",
    ])
    assert exit_code == 0
    result = load_only_result(tmp_path / ".loom" / "optimize")
    assert result["disposition"] in {"promoted", "rejected"}
    assert Path(result["report_path"]).is_file()
```

Add a second test that interrupts after seed analysis, resumes, asserts the
provider call ledger contains no duplicate completed call, and asserts holdout
fingerprints never occur in proposer messages.

- [ ] **Step 2: Run the end-to-end test and confirm the first integration gap**

Run: `uv run pytest tests/integration/test_optimize_end_to_end.py -q`

Expected: fail at the first missing composition or fixture behavior, not during
test collection.

- [ ] **Step 3: Close only integration gaps revealed by the test**

Make focused corrections in the owning modules. Keep domain decisions inside
existing campaign/governance APIs, preserve stable operation IDs, and add a
unit regression test beside each correction before rerunning the end-to-end
test.

- [ ] **Step 4: Document primary, dry-run, explicit-set, resume, and approval workflows**

Add README examples for:

```bash
uv run loom optimize --trace runs/seed.jsonl --tasks datasets/project-audit.jsonl --config config.yaml --tui
uv run loom optimize --trace runs/seed.jsonl --tasks datasets/project-audit.jsonl --config config.yaml --dry-run
uv run loom optimize status opt_<id> --json
uv run loom optimize approve opt_<id> --candidate candidate_<id> --identity approver
```

Document the exact simple JSONL row and the minimum independent-task rule.
Add the approved `meta_harness` block to the developer's ignored `config.yaml`
using existing model profile names; do not add credentials or force-track the
file.

- [ ] **Step 5: Run the focused integration and CLI suites**

Run: `uv run pytest tests/integration/test_optimize_end_to_end.py tests/optimize tests/integration/test_governed_meta_harness_lifecycle.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit tracked integration and documentation changes**

```bash
git add tests/integration/test_optimize_end_to_end.py README.md
git commit -m "test: cover one-command meta-harness optimization"
```

### Task 12: Final Verification and Design-Conformance Audit

**Files:**
- Modify only files required by a failing verification; add a regression test for every correction.

**Interfaces:**
- Consumes: complete implementation and approved specifications.
- Produces: verified branch ready for review.

- [ ] **Step 1: Run formatting and static checks**

Run: `uv run ruff check src tests`

Expected: exit 0 with no diagnostics.

- [ ] **Step 2: Run the complete test suite**

Run: `uv run pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run CLI smoke checks**

Run:

```bash
uv run loom optimize --help
uv run loom optimize status --help
uv run loom optimize approve --help
```

Expected: each exits 0 and shows its documented arguments.

- [ ] **Step 4: Audit specification invariants**

Verify from tests and stored artifacts that:

- no model call occurs before preflight completion;
- same-input replay returns the recorded operation;
- holdout content is absent from proposer prompts and discovery history;
- `--new-run` cannot reuse a disclosed holdout digest;
- actor assertions do not combine proposer/controller/finalizer/approver roles;
- low-risk declarative promotion has rollback and active monitor evidence;
- medium/high/executable candidates cannot activate without approval;
- JSON/text/TUI observe one committed event stream;
- reports contain no secret values or holdout task text.

- [ ] **Step 5: Inspect final repository state and commits**

Run:

```bash
git status --short --branch
git log --oneline --decorate -15
git diff main...HEAD --stat
```

Expected: no unintended tracked changes, focused task commits present, and the
diff limited to optimize integration plus required narrow seams.

- [ ] **Step 6: Commit any verification-only correction**

If Step 1-4 required a correction, commit its code and regression test with a
focused message. If no correction was required, do not create an empty commit.
