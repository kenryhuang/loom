# Loom Python

Python implementation of Loom, built as a sibling project to the TypeScript `loom`
implementation.

## Development Environment

This project uses `uv` for a reproducible local environment.

```bash
uv sync
```

That creates `.venv` and installs the project with its development tools from
`uv.lock`.

## Verify

```bash
uv run pytest
```

Developer checks:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
```

## Build

```bash
uv build
```

## Package Layout

The project uses a `src` layout. Subpackage `__init__.py` files are export
shims only; implementation lives in named modules such as `core/models.py`,
`runtime/engine.py`, `llm/api.py`, and `observability/traces.py`.

## General Task Execution Design

The [general task plugin runtime design](docs/superpowers/specs/2026-10-04-general-task-plugin-runtime-design.md)
defines separate plugins for `SessionEnvironment`, context management, tool
collections, `ToolExecutionRuntime`, and dynamic workflows. The native OS phase
is implemented for both one-shot tasks and durable sessions, including tasks
without a workspace, bounded context compaction, versioned plugin recovery,
resource claims, and sequential workflow revisions. See the
[configuration and plugin guide](docs/general-task-runtime.md) for runnable
examples. Docker and VM backends remain future extensions; unavailable backends
fail explicitly without falling back to native execution.

## LLM Configuration

LLM examples use the existing OpenAI-compatible provider. If no `api_key` is
passed, Loom reads local settings from the project `.env` file.

The minimal `.env` shape is:

```bash
LOOM_LLM_MODEL=qwen3.6-max-preview
LOOM_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LOOM_LLM_API_KEY=...
```

The `.env` file is local-only and ignored by git. The same values can also be
passed with process environment variables. Generic `OPENAI_MODEL`,
`OPENAI_BASE_URL`, and `OPENAI_API_KEY` are supported as fallbacks.

Live LLM smoke tests are skipped by default. To run one real OpenAI-compatible
LLM call through the full Loom runtime/tool/trace chain:

```bash
LOOM_RUN_LIVE_LLM=1 uv run pytest tests/integration/test_live_llm_smoke.py -q
```

Optional knobs:

```bash
LOOM_LIVE_ENV_FILE=.env
LOOM_LIVE_MAX_COMPLETION_TOKENS=512
LOOM_LIVE_TEMPERATURE=0
```

## Persistent Task Sessions

Loom can run as a local backend service. Each session keeps one task, its
conversation, execution checkpoints, tool results and event history across
frontend connections and service restarts. A completed execution returns the
task to `idle`; the next message starts another run of the same task.

Start the service from the directory containing your `.env`, or explicitly
select a server-owned model configuration:

```bash
uv run loom serve --data-dir .loom/service --config config.yaml
```

Omit `--config` to use the environment provider described above. Model names in
session requests refer to configuration entries; credentials remain on the
backend. The service binds to `127.0.0.1:8765` and writes its private credential
to `.loom/service/credential`. Set `--max-active-runs` to change the default of
two active workers. Sessions sharing the same canonical workspace execute
serially.

In another terminal, open a new interactive session:

```bash
uv run loom
```

The session is saved immediately and waits for a task. Type the first task in
the input box and press Enter to start execution. The current directory is its
workspace; use `uv run loom --workspace /path/to/project` to select another one.
The interface displays the session ID and lets you switch between sessions.
The repository's default uv dependency groups include the TUI dependencies, so
`--extra tui` is optional. Other installations can use `pip install 'loom[tui]'`.

Reconnect and resume an existing session with:

```bash
uv run loom --resume SESSION_ID
```

Paused and failed executions resume from their saved state. Running or queued
executions are connected without starting another worker. A pending question
or recovery verification is shown before execution can continue. Idle sessions
wait for the next instruction; completed tasks reopen and also wait for a new
instruction. Each explicit Resume grants a fresh active-time allowance (30
minutes by default), measured from the run's current accumulated active time.
Cumulative elapsed time is retained for reporting; it does not cap the total
time across resumes. Token, step, and model-call usage retain their existing
limits and counts.

New sessions default to a **10M token budget** (10,000,000 tokens). Set a
different total when creating or resuming a session:

```bash
uv run loom --token-budget 20M
uv run loom --resume SESSION_ID --token-budget 20M
uv run loom session create 'Maintain this project' --token-budget 500K
```

Inside the TUI, enter `/budget` to view usage or `/budget 20M` to change the
limit. The interface also displays used tokens and the current limit. Pause
active execution before changing its budget, then press Resume to continue.
From another terminal, use:

```bash
uv run loom session budget SESSION_ID       # show limit, used and remaining
uv run loom session budget SESSION_ID 20M   # change the saved limit
```

Counts accept integers, `K` (1,000), and `M` (1,000,000), including `2.5M`.
This budget counts cumulative input and output tokens in the current run;
it is not a byte count or the per-response `max_completion_tokens` limit.
Changing the limit preserves usage and checkpoints, and a new run starts its
own count. Existing sessions keep their saved limit until explicitly changed.

The backend must already be running. When it uses another data directory,
point the frontend at the credential file printed by `loom serve`:

```bash
export LOOM_SERVICE_TOKEN_FILE=/absolute/service/directory/credential
uv run loom
```

The same `--url` and `--token-file` options work on the default interactive
entry. Backend model configuration and `.env` belong to the service process;
when starting it from another directory, specify the environment file as well:

```bash
LOOM_ENV_FILE=/path/to/project/.env uv run loom serve \
  --data-dir /path/to/service/data --config /path/to/project/config.yaml
```

Existing session commands remain available for scripted interaction:

```bash
uv run loom session create 'Maintain this project and fix regressions' --workspace .
uv run loom session list
uv run loom session message SESSION_ID 'Prioritize the failing parser tests'
uv run loom session snapshot SESSION_ID
uv run --extra tui loom session connect SESSION_ID
```

The TUI lists sessions, shows the current plan and model/tool execution feed,
and accepts additional guidance while work runs. Enter submits a message or
answers the pending question. Redirect explicitly supersedes a clarification.
History pages backward through stored events; Detail opens a selected large
execution artifact. The feed retains up to 500 visible rows. `Ctrl+C` or `Ctrl+Q` disconnects
the frontend; the backend keeps running.

Commands can also be used without the TUI:

```bash
uv run loom session pause SESSION_ID
uv run loom session resume SESSION_ID
uv run loom session stop SESSION_ID
uv run loom session answer SESSION_ID 'Payments module' --request-id INPUT_ID
uv run loom session history SESSION_ID --limit 100
uv run loom session events SESSION_ID --after 0
uv run loom session artifact SESSION_ID DIGEST --output detail.json
uv run loom session complete SESSION_ID
uv run loom session reopen SESSION_ID
```

Pause preserves the current run and resumes at its saved boundary. Stop ends
the run while retaining its observations; resuming starts a new run. Complete
explicitly closes the task after execution is idle or paused. User input has
separate `accepted` and `applied` states: guidance applies at a model/tool
boundary, and cancels calls from an older response that have not executed.
`request_input` releases the worker while the task awaits an answer.

When a worker disappears during a side effect, the task enters `recovering`.
Verify the workspace before answering the recovery question with JSON such as
`{"resolution":"completed","evidence":"Verified the requested file contents"}`.
The alternatives are `not_applied` (permit retry after verifying no effect) and
`stop` (end the uncertain run). Recovery questions cannot be redirected.
Committed tool results are reused; uncertain effects are never blindly retried.
Pause, stop and complete cannot bypass a pending recovery question. Process
cleanup checks the entire recorded group, including children whose leader has
exited. Until cleanup confirms exit, all sessions sharing that workspace remain
blocked. Reconnecting during model output restores the saved text baseline and
continues from its absolute offset.

Use `LOOM_SERVICE_URL`, `LOOM_SERVICE_TOKEN_FILE`, or explicit `--url` and
`--token-file` before the session subcommand when connecting from another
working directory. `LOOM_SERVICE_TOKEN` is an optional environment override.
Supplying `--command-id` on create/message/control commands makes retries
idempotent; reusing the ID with different content returns a conflict.

The HTTP API uses bearer credentials, JSON commands under `/v1/sessions`,
consistent snapshots, paged history and SSE with persisted session sequence
numbers. See the [design](docs/superpowers/specs/2026-10-03-persistent-service-sessions-design.md)
for the protocol and lifecycle. Keep the data directory between restarts.
This release targets macOS/Linux, a single local user and a terminal frontend.
Domain plugins and automatic workflow learning remain separate follow-up work.
Verification uses deterministic providers and spawned processes; live-provider
integration, prolonged load and storage-failure handling still require separate
validation.

## One-shot Task Runner

Run an arbitrary task through the Loom LLM loop with workspace tools:

```bash
uv run python -m loom.tasks.run \
  --model main \
  --tui
```

The CLI automatically loads `./config.yaml` or `./config.yml` when present.
CLI flags override config values. By default, the generic task CLI persists run
trace records under `runs/loom-task-*.jsonl`. Set `run.trace_path_template` for
dynamic trace names, or pass `--config` / `--trace-path` to override those
locations.

Complex tasks can use the Plan & Execute workflow through the unified entry
point:

```bash
uv run loom task "Audit and improve this project" --plan-mode auto --tui
uv run loom task "Exercise the planning workflow" --plan-mode force --tui
uv run loom task "Run the legacy ReAct path" --plan-mode off
```

`auto` lets the model enter planning dynamically, including after exploratory
tool calls. `force` starts in planning and requires plan submission before task
tools are available. `off` exposes no planning tools and keeps the legacy ReAct
path. Plan-enabled traces retain every `plan.*` lifecycle event as a complete
checklist snapshot, while the TUI presents those snapshots as one live-updating
checklist.

The task config supports task defaults, run defaults, and multiple named
OpenAI-compatible models:

```yaml
default_model: main

task:
  objective: Audit this project briefly
  workspace: .
  profile: project_audit
  expected_outputs:
    - Brief markdown audit report with project purpose, smoke result, and improvement directions.

run:
  tui: false
  stream: true
  plan_mode: auto
  trace_path_template: runs/{task_slug}-{model}-{timestamp}.jsonl
  max_steps:
  timeout_ms:

models:
  main:
    provider: openai
    model: qwen3.7-max
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 8192
    request_options:
      enable_thinking: true
      thinking_budget: 2048
      tool_stream: true
      parallel_tool_calls: false

  fast:
    provider: openai
    model: qwen-plus
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
```

`max_completion_tokens` limits the complete model output, including reasoning
content. Provider-specific `request_options` are copied to the top level of
both streaming and non-streaming Chat API requests. Loom rejects options that
would override its request ownership (`model`, `messages`, `stream`, `tools`,
`tool_choice`, `temperature`, and completion limits); the provider validates
whether a configured option applies to the selected model. See the
[Bailian OpenAI-compatible Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
for supported thinking, generation, tool, and search parameters.

## Trace Evaluation

Generic task runs write JSONL traces under `runs/` by default. Analyze a trace
with version-two evidence ledgers and coverage reporting:

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/loom-task-xxx.jsonl \
  --out-dir .loom/evaluation
```

Add `--judge --config config.yaml --model <named-model>` for semantic diagnosis
of context effectiveness, tool effectiveness, loop progress, token efficiency,
and task verification. The judge can expand original evidence by source digest,
record line and field; missing or unreviewed evidence remains unknown. Reports
separate measured facts, supported diagnoses, proposed checks and preserved
behaviors, without a combined quality score.

Use `--analysis-version v1` for the legacy scoring/bundle consumed by the current
evolution pipeline. Existing Python `EvaluationConfig` callers retain the v1
default; select `analysis_version="v2"` explicitly in Python. A v1 consumer rejects
v2 bundles instead of silently interpreting their diagnoses as old scores.
See [trace effectiveness analysis](docs/trace-effectiveness-analysis.md) for
budgets, output files and verification limits.

## Governed Meta-Harness

### One-command optimization

The recommended workflow composes trace evaluation, evolution analysis,
candidate search, paired trials, campaign finalization, and governance behind
one command:

```bash
uv run loom optimize \
  --trace runs/seed.jsonl \
  --tasks datasets/project-audit.jsonl \
  --config config.yaml \
  --tui
```

Use `--json` instead of `--tui` for a versioned event stream. A dry run resolves
models, snapshots workspaces, checks provenance and contamination, freezes the
three task sets, estimates the campaign, and prints the stable optimization ID
without calling a proposer, solver, or judge:

`--tui` opens the full-screen Operations Dashboard before any model call. It
keeps the pipeline, candidate/paired-trial progress, live nested task events,
budget consumption, sealed holdout status, and governance state visible at the
same time. The dashboard is a projection of the durable SQLite and artifact
state; it does not replace campaign evidence or alter model streaming.

TUI controls:

- `q` detaches the dashboard while the optimization keeps running and falls
  back to concise text progress.
- `p` requests a durable pause at the next safe stage checkpoint.
- `c` requests cancellation after an explicit confirmation; the resulting
  paused optimization remains resumable.
- `a` opens identity/reason input and submits the stored governance approval
  when the campaign is in `awaiting_approval`; Loom still enforces the existing
  authorization, evidence, risk, and staleness checks.
- Arrow keys or `j`/`k` move through candidates; `y` and `Y` copy event detail
  or the bounded event window.

The dashboard remains open on completion so the result can be inspected; press
`q` to return to the shell. Exit code `0` means a normal terminal result, `2`
means governance is awaiting approval, `3` means the run was durably paused,
and `1` is a failure. Re-run the identical command after a pause to load a
snapshot, replay completed stages, and continue at the next unfinished durable
operation.

```bash
uv run loom optimize \
  --trace runs/seed.jsonl \
  --tasks datasets/project-audit.jsonl \
  --config config.yaml \
  --dry-run
```

Each line in `--tasks` is a small JSON object. `task_id`, `objective`, and
`workspace` are required; paths are resolved relative to the JSONL file.
`profile`, `constraints`, `expected_outputs`, `risk_level`, `verifier`, and
`metadata` are optional:

```json
{"task_id":"audit-yakdb","objective":"Audit YakDB, run its smallest embedded smoke test, and report evidence-backed improvements.","workspace":"../workspaces/yakDB","profile":"project_audit","constraints":["Do not modify project source files."],"expected_outputs":["A Markdown audit report."]}
```

With the default three repetitions and five required pairs, every discovery,
validation, and holdout set needs at least two independent tasks. Automatic
splitting additionally needs at least three independent snapshot/template
groups; near-duplicates across sets fail closed. Advanced users can provide
already separated inputs, which still pass the same checks:

```bash
uv run loom optimize \
  --trace runs/seed.jsonl \
  --discovery-tasks datasets/discovery.jsonl \
  --validation-tasks datasets/validation.jsonl \
  --holdout-tasks datasets/holdout.jsonl \
  --config config.yaml \
  --tui
```

The optimization ID is derived from the frozen inputs. Repeating the same
command resumes incomplete work or replays the terminal result without
repeating completed model calls or holdout trials. After holdout disclosure,
`--new-run` is rejected until the corpus or split seed produces a new holdout
digest. Inspect the durable projection with:

```bash
uv run loom optimize status opt_<id> --json
```

Low-risk loop-policy changes may promote automatically. Medium-risk prompt,
tool-policy, and solver request-option changes stop at `awaiting_approval`
(exit code 2). Approval is explicit and bound to the stored candidate, policy,
risk, gate, baseline, and evidence digests:

```bash
uv run loom optimize approve opt_<id> \
  --candidate cand_<id> \
  --identity approver \
  --reason "Reviewed the bounded change and holdout evidence."
```

Terminal artifacts are written to
`.loom/optimize/opt_<id>/{result.json,report.md}`. A successful process exit can
mean either `promoted` or `rejected`; automation should read `disposition`.

The required configuration block reuses named model profiles from the generic
task runner:

```yaml
meta_harness:
  proposer_model: main
  solver_model: kimi
  judge_model: kimi_judge
  search:
    iterations: 4
    candidates_per_iteration: 2
    candidate_kinds: [declarative_patch]
    editable_surfaces:
      - agent.system_prompt
      - agent.tool_policy
      - agent.loop_policy
      - models.solver.request_options
  tasks:
    seed: 42
    repetitions: 3
    minimum_pairs: 5
    contamination_threshold: 0.80
  budgets:
    max_candidates: 8
    max_llm_calls: 200
    max_wall_clock_minutes: 180
  execution:
    max_parallel_trials: 2
    trial_timeout_seconds: 900
    verifier_timeout_seconds: 120
  governance:
    mode: local
    auto_promote_max_risk: low
    require_approval_for: [medium, high, executable]
```

### Domain APIs

Loom's governed Meta-Harness implementation adds two authority-separated
packages:

```text
TraceBundle -> EvaluationBundle -> EvolutionBundle
                                      |
                                      v
                              loom.campaigns
                                      |
                                      v
                              loom.governance
```

`loom.campaigns` owns immutable campaign specs, content-addressed artifacts,
SQLite/WAL event and budget transactions, task-set contamination checks,
declarative patch compilation, paired experiments, Pareto frontiers, read-only
history, proposer sessions, and one-time validation/holdout finalization.
`loom.governance` alone owns risk, mandatory gates, approvals, active registry
pointers, monitoring, expiry/rollback decisions, and their atomic audit log.

Create a campaign from a resolved JSON or YAML configuration. Authority-bearing
commands require an identity file containing only the presented actor
assertion. Trust is configured independently by the deployment launcher through
`LOOM_TRUST_STORE` (default `/etc/loom/trust.json`). Authority-bearing commands
do not accept a trust-root argument, so an identity file cannot make itself
trusted:

`operator.json`:

```json
{"actor":{"subject":"operator","roles":["campaign_operator"],"issued_at":"2026-07-18T00:00:00.000000Z","expires_at":"2026-07-19T00:00:00.000000Z","signature":"signed-operator"}}
```

`trust.json`, installed by the deployment authority:

```json
{"trusted_assertions":[{"subject":"operator","roles":["campaign_operator"],"issued_at":"2026-07-18T00:00:00.000000Z","expires_at":"2026-07-19T00:00:00.000000Z","signature":"signed-operator"}],"revoked_signatures":[]}
```

```bash
export LOOM_TRUST_STORE=/etc/loom/trust.json
uv run loom campaign create .loom/campaigns/my-campaign \
  --config campaign.json \
  --identity .loom/identities/campaign-creator.json \
  --operation-id op_<uuidv7> \
  --json
```

The input follows the shape in
`docs/superpowers/specs/2026-07-18-governed-meta-harness-design.md`. Task-set
fields point to frozen JSONL manifests; creation copies them into the campaign's
content-addressed artifact store and freezes their digests. Derivation requires
a different sealed holdout digest:

```bash
uv run loom campaign derive .loom/campaigns/child \
  --from-campaign-dir .loom/campaigns/parent \
  --config child-campaign.json \
  --identity .loom/identities/campaign-creator.json \
  --operation-id op_<uuidv7> \
  --json
```

Inspect or change only the campaign lifecycle projection:

```bash
uv run loom campaign status .loom/campaigns/my-campaign --campaign-id cmp_<uuidv7> --identity .loom/identities/operator.json --json
uv run loom campaign run    .loom/campaigns/my-campaign --campaign-id cmp_<uuidv7> --identity .loom/identities/operator.json --operation-id op_<uuidv7>
uv run loom campaign pause  .loom/campaigns/my-campaign --campaign-id cmp_<uuidv7> --identity .loom/identities/operator.json --operation-id op_<uuidv7>
uv run loom campaign resume .loom/campaigns/my-campaign --campaign-id cmp_<uuidv7> --identity .loom/identities/operator.json --operation-id op_<uuidv7>
uv run loom campaign import-experience .loom/campaigns/my-campaign evaluation-bundle.json \
  --campaign-id cmp_<uuidv7> --identity .loom/identities/campaign-creator.json --json

uv run loom candidate create cmp_<uuidv7> --campaign-dir .loom/campaigns/my-campaign \
  --patch patch.json --hypothesis hypothesis.md --identity .loom/identities/operator.json --json
uv run loom candidate validate cmp_<uuidv7> cand_<uuidv7> --campaign-dir .loom/campaigns/my-campaign \
  --candidate-ref candidate-ref.json --validation-ref validation-ref.json \
  --experiment-ref discovery-experiment-ref.json --identity .loom/identities/controller.json --json
uv run loom campaign frontier .loom/campaigns/my-campaign --campaign-id cmp_<uuidv7> \
  --identity .loom/identities/controller.json --json
uv run loom experiment compare cmp_<uuidv7> cand_<baseline> cand_<candidate> \
  --campaign-dir .loom/campaigns/my-campaign --identity .loom/identities/controller.json --json
uv run loom task-set fingerprint discovery.jsonl --role discovery --json
uv run loom task-set validate discovery.jsonl --against validation.jsonl --against holdout.jsonl --json
```

`CampaignController.run_iteration()` is the adapter boundary for bounded
proposer search: deployments inject the proposer, history view, candidate
workspace, validation, and experiment runner. Validation and holdout consume
only signed, content-addressed `ExperimentBundle` references. Search sealing,
finalist selection, and holdout finalization are digest-bound and safely
replayable; no CLI argument can directly assert a score or pass/fail result.
Production proposer backends return a supervisor-owned `MeteredProposalResult`;
token, cost, and wall-time usage is never read from candidate-controlled JSON.
Experiment usage is likewise recorded by the paired runner, and each phase
must use the deterministic three-repetition `TrialPlan` rebuilt from its frozen
task-set manifest.

Governance has a separate SQLite authority and resolves every baseline,
candidate, policy, risk, gate, rollback, and active reference through the
content-addressed artifact store before changing a pointer:

```bash
uv run loom governance active --surface context_policy \
  --governance-dir .loom/governance --artifact-root .loom/campaigns/my-campaign/artifacts \
  --identity .loom/identities/registry-reader.json --json
uv run loom governance approve cand_<uuidv7> \
  --candidate-digest <sha256> --baseline-digest <sha256> --policy-digest <sha256> \
  --risk-rules-digest <sha256> --gate-digest <sha256> --expires-at 2026-07-19T00:00:00Z \
  --governance-dir .loom/governance --artifact-root .loom/campaigns/my-campaign/artifacts \
  --identity .loom/identities/approver.json --json
```

Registry bootstrap plus exact policy/risk-rule digests require an independent
`governance_admin`. Campaign finalizers cannot self-assert mandatory gate
booleans: task isolation, contamination, evaluator independence, sandbox
conformance, and security regression results must be bound to signed
`campaign_controller` gate-source artifacts. Governance derives holdout metrics
from the signed recommendation and directly checks baseline, policy, rollback,
monitor, candidate lineage, and artifact integrity before activation.

Security boundaries are fail-closed. Historical text is sanitized, labeled as
untrusted evidence, and filtered by phase before reaching a proposer. The local
subprocess proposer is disabled unless `trusted_local_debug=True`; that flag is
for deterministic tests and is not a production isolation boundary. Production
proposers and executable candidates use framed sandbox adapters.
`PodmanRootlessRuntime` probes the runtime and constructs a fixed,
digest-pinned command with a read-only root, explicit mounts, a fresh bounded
scratch tmpfs, user/mount/network namespaces, dropped Linux capabilities,
`no_new_privs`, seccomp, cgroup limits, no network or host environment
forwarding, and fixed Loom runner entrypoints. If those controls are
unavailable, Loom returns `SANDBOX_UNAVAILABLE`; it never falls back to running
candidate code in the Loom process or a plain subprocess.

The initial implementation covers design Phases 0–4 with local deterministic
stores and injectable execution/isolation adapters. Phase 5 cross-campaign
transfer and distributed scale remain intentionally deferred by the approved
design.
