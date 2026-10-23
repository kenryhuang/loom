# Loom

[中文文档](README.zh-CN.md)

Loom is a local AI agent runtime for coding, research, and general tasks, with
persistent execution, independent acceptance checks, and inspectable evidence.
Give it an objective, a model, and tools; follow the work from Web or TUI and
resume from saved checkpoints.

## Key features

- **Deep eval memory:** extract stage contributions, scoped environment and configuration
  facts, failure lessons, and more efficient workflows from traces. Records link back
  to goals, stages, actions, and source evidence; observed findings stay distinct from
  proposed improvements. See [Deep eval Memory](docs/deep-eval-memory.md).
- **Independent acceptance before completion:** actual command checks and a
  separate semantic review assess task- and workspace-specific requirements.
  Failed candidates enter bounded repair; missing evidence retains a concrete
  blocker. Check results and evidence remain available for review.
- **Real browser and WebGL evidence:** optional browser tools replay pages,
  wait for rendering, and capture multiple Canvas/WebGL frames. An independent
  image model reviews the screenshots alongside browser assertions and runtime
  facts; collection alone does not establish acceptance.
- **Durable sessions across Web and TUI:** conversations, checkpoints, tool
  results, artifacts, and events survive frontend disconnects and support
  recovery after service restarts. Both interfaces share streaming output,
  controls, history, and budgets. Completed sessions accept follow-up rounds.
- **Inspectable execution and context:** review model requests, tool inputs and
  results, traces, and session trajectories. Context compaction retains original
  content in artifacts so omitted evidence can be retrieved.
- **Configurable models, tools, and workflows:** use OpenAI-compatible model
  profiles, file/shell tools, URL readers, MCP servers, and CLI connections.
  Choose tools manually or through LLM recommendations; configure dynamic
  workflows or Plan & Execute separately from resources and context management.
- **Commands informed by the execution host:** execution and acceptance prompts
  include the worker's OS, architecture, actual shell, and container information.
  Platform-specific guidance covers command syntax and utility flags; resuming
  a session refreshes these facts.
- **Local knowledge and cited reports:** keyword, hybrid vector, YakDB, and
  LightRAG retrieval support repeated research. LightRAG adds resumable website
  sync and graph exploration. Reports retain sources and artifacts, and can also
  be saved to a writable workspace with visible file paths.
- **Failure diagnosis and evaluation:** investigate saved runs in a read-only
  loop or an isolated worktree, retain patches and test receipts, and review
  changes before merging. Trace analysis, Meta-Harness, and opt-in
  [behavior evaluation v3](docs/behavior-evaluation-v3.md) support experiments
  and distinguish goal acceptance from execution quality.

The current general task runtime executes tools on the host OS. Loom targets
macOS/Linux and a single local user; Docker/VM task backends are not implemented.
Environment detection can identify container markers and the Docker CLI; it
does not create containers or automatically isolate tasks in Docker.

## Quick start

Requirements: Python 3.11+, `uv`, and access to an OpenAI-compatible model API.
Run the following `uv` commands from the Loom repository directory.

### 1. Install

```bash
uv sync
```

This installs the project, development tools, and TUI dependencies into `.venv`.
For LightRAG, also install the optional graph dependencies:

```bash
uv sync --extra knowledge-graph
```

For Web / Canvas / WebGL visual features, install the optional browser
dependencies and Chromium:

```bash
uv sync --extra browser --inexact
PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv/bin/python -m playwright install chromium
```

`--inexact` preserves other optional dependencies already installed. Browser
downloads happen during setup, not automatically while tasks run.

### 2. Configure a model

Create a local `.env` file. Replace the model, endpoint, and key with your
provider's values:

```dotenv
LOOM_LLM_MODEL=your-model-id
LOOM_LLM_BASE_URL=https://your-provider.example/v1
LOOM_LLM_API_KEY=your-api-key
```

`.env` is ignored by Git. Process environment variables override its values.
Named models and provider options are covered under [Configuration](#configuration).

### 3. Start the service

```bash
uv run loom serve --data-dir .loom/service
```

Keep this process running. By default it listens on `127.0.0.1:8765`, hosts the
Web frontend, and writes its connection credential to `.loom/service/credential`.
Keep the data directory between restarts to retain sessions and knowledge bases.

### 4. Open Web or TUI

For **Web**, open <http://127.0.0.1:8765/web/> and load the credential file, or
paste its contents into the connection dialog.

For **TUI**, open another terminal in the Loom repository and run:

```bash
uv run loom --workspace /absolute/path/to/project
```

A new session is saved immediately and waits for your first task. Type it and
press Enter to begin.

## Using the Web frontend

1. Connect using the credential printed by `loom serve`.
2. Open the new-session panel and enter your objective.
3. Select **General**, **Research**, or **Coding**, then review the tools, model,
   workspace, and knowledge base selections. **Recommend with LLM** can suggest
   a configuration; review it before clicking **Create session**. For page
   interaction or visual acceptance, select **Browser & visual evidence**;
   local application previews also need a workspace, Files, and Commands.
4. Follow the conversation and expand **Process** to inspect model requests,
   tool arguments, results, and errors. Workflow, budget, and output panels show
   the current task state.
5. Send additional guidance or answer a pending question. Use **Pause**,
   **Resume**, and **Stop** to control execution.

Task controls have distinct meanings: **Complete task** closes the task, and
**Reopen task** enables new messages without starting execution. **Restart task**
runs the current goal again with a new run, fresh plan and acceptance checks, and
reset run budgets. Earlier conversation and results are retained as bounded background;
old checkpoints and acceptance results are not reused. Workspace files remain as they are.
Use **Session info → Details → Edit task → Save & restart** to change the objective,
model, execution limits, context/workflow plugins, tool collections, knowledge
bases, and external tools. The workspace is read-only for an existing session;
create a new session to use another directory. Changes are validated and saved atomically with restart;
external bindings are refreshed from the registry. An explicitly edited objective becomes
the new request; otherwise pending guidance is included once.
You can restart directly from idle, paused, failed, or completed state after the
previous worker exits. TUI provides the same controls; CLI: `loom session restart SESSION_ID`.

Open **Evaluation / Evolve → Session diagnosis** to investigate a session failure.
Choose a run and model, optionally describe the problem, then click **Diagnose session**.
An independent Loom loop investigates source, trace, configuration, and assembly.
By default, **Worktree verification** creates a branch under `.worktrees/`, includes
your current uncommitted code, and lets the agent write regression tests, edit files,
and run offline checks in an OS sandbox. **Auto-delete worktree** removes the temporary
branch and workspace when the diagnosis ends; reports, test receipts, and the patch remain.
Choose **Keep for review** to retain it, or **Read-only investigation** to disable writes.
Use **Review patch → Merge reviewed changes** to apply the saved delta to the original
working directory after conflict checks. Merging does not stage, commit, or restart the service.
Validation requires macOS `sandbox-exec` or Linux `bubblewrap`; no unrestricted fallback.
Use **Budgets → Resume diagnosis** after increasing an exhausted budget; saved evidence
is reused. [Design and implementation notes](docs/session-self-diagnosis.zh-CN.md).

Coding and file/command tools require an existing workspace directory on the
service host. Research can run without a workspace; bind a writable directory
when you want analysis documents saved as files as well as artifacts. The
built-in research tools read supplied HTTP(S) URLs and do not provide a search
engine. Use the optional browser collection to execute page JavaScript or inspect
Canvas/WebGL output.

The **Knowledge bases** view manages imports, embedding profiles, website
sources, and graph exploration. Attach selected bases to a session so its agent
can retrieve them. The LightRAG **Sync activity** area shows the current
page/document, stage, saved document count, request usage, and recent activity;
**Resume sync** continues from saved progress after failure or cancellation.

The **External tools** view registers MCP (stdio, Streamable HTTP or legacy SSE) and CLI
connections. Discover MCP tools, then select them in **New session** or use
**Recommend with LLM**. Existing sessions can change their selection between
tasks. See the [external tools guide](docs/external-tools.md) for installation,
configuration, and examples.

**Trajectory** opens execution analysis for a session. **Outputs** exposes saved
document paths and report artifacts. See the [Web guide](docs/web-frontend.md)
and [session setup guide](docs/session-setup.md) for details.

Closing the tab leaves backend tasks running. The browser keeps its credential
in tab memory, so reloading requires reconnecting. The frontend ships with the
Python package; it needs no Node runtime or separate frontend server.

### Web / Canvas / WebGL visual acceptance

The browser collection provides `browser_open`, `browser_act`, `browser_observe`,
`browser_capture`, and `browser_close`. The execution agent can inspect pages;
formal acceptance replays the scenario in an independent browser and sends actual
screenshots to a separate image model. See the `visual_verification_model`
example below, or set Input modalities in **Settings → Models** and choose
Visual acceptance model under **General**.

Local previews use a bounded copy of the candidate source and a dedicated port,
starting the actual application through the `{port}` placeholder in
`preview.argv`. External pages require an explicitly bound URL resource. Formal
acceptance does not reuse an existing dev server whose source version is unknown.
Loom's bundled frontend needs no Node runtime; the application being verified
still needs its own runtime dependencies.

Before capture, Loom waits up to **12 seconds** for document and resource loading,
visible Canvas initialization, and actual rendering calls. For recognized React
Three Fiber pages, it also waits for content to mount into an empty scene root,
reducing captures of UI-only screens or black frames during texture loading.
When capture times are omitted and Canvas is requested or WebGL is detected,
the default is a first frame 1.5 seconds after readiness, followed by another
frame 1 second later. Readiness is not acceptance; timeouts retain actual images
and diagnostics.

Open **Processing / acceptance records → View screenshot** to inspect original
images and finding regions. Evidence also records readiness duration, timeout
phase, page exceptions, failed resource requests, HTTP errors, and available
scene facts. A model cannot override failed browser assertions. Exact object
counts require assertions or independent runtime evidence rather than image
guesswork. Insufficient views can trigger one recapture; unresolved gaps retain
a concrete blocked result.

**Input budget** is the per-request context allowance; **Acceptance budget**
tracks verification calls, time, and failed candidates. Visual criteria adjust
default verification limits, while explicit task limits remain authoritative.
After acceptance blocks or exhausts its allowance, an explicit **Resume** retains
the history and starts a fresh bounded acceptance allocation for new evidence.
Task-wide usage and original requirements remain. Restarting the service alone
does not replenish this allocation; pauses and environment blockers do not count
as failed candidate repairs.

The initial scope covers Web pages, Canvas, and WebGL. See the
[visual observation and acceptance guide (Chinese)](docs/visual-observation-and-acceptance.zh-CN.md)
for setup, scenarios, evidence boundaries, and recovery.

## Using the TUI and session CLI

```bash
# New session; the default workspace is the current directory
uv run loom

# New session using a named model from the service configuration
uv run loom --workspace /absolute/path/to/project --model dashscope/your-model-id

# Reconnect to an existing session and resume eligible paused/failed work
uv run loom --resume SESSION_ID

# List sessions or connect without requesting a resume
uv run loom session list
uv run loom session connect SESSION_ID
```

The TUI shows sessions, the plan, streaming execution, expandable details, and
the final result. Enter submits guidance or answers a question. History and
Detail retrieve stored events and large artifacts. `Ctrl+C` or `Ctrl+Q`
disconnects the frontend while the backend continues.

Session commands also work without the TUI:

```bash
uv run loom session create 'Inspect this project and fix the failing tests' --workspace /absolute/path/to/project
uv run loom session message SESSION_ID 'Prioritize the parser tests'
uv run loom session pause SESSION_ID
uv run loom session resume SESSION_ID
uv run loom session stop SESSION_ID
uv run loom session snapshot SESSION_ID
uv run loom session history SESSION_ID --limit 100
uv run loom session artifact SESSION_ID DIGEST --output report.json
```

| Control           | Behavior                                                       |
| ----------------- | -------------------------------------------------------------- |
| Pause / Resume    | Preserve and continue the current run from its saved boundary. |
| Stop              | End the current run; subsequent resumption starts a new run.   |
| Complete / Reopen | Explicitly close or reopen the task.                           |
| Disconnect        | Close the frontend connection; backend work continues.         |

When an interrupted tool's effect cannot be confirmed, Loom asks for recovery
verification before continuing. Committed tool results are reused.

New sessions default to a **10M token budget**. Use `--token-budget 500K` or
`--token-budget 20M` when creating a session, `/budget` and `/budget 20M` in
the TUI, or the CLI:

```bash
uv run loom session budget SESSION_ID
uv run loom session budget SESSION_ID 20M
```

Pause active work before changing its budget. The budget counts input and output
tokens in the current run; it is separate from the model's per-response output
limit. Resume renews the active-time allowance (30 minutes by default) while
preserving the current run's token, step, and model-call usage.

## Configuration

### Model profiles: `config.yaml`

For multiple models or provider-specific options, create a configuration file:

```yaml
default_model: dashscope/your-model-id
# Optional: select a separately configured judge model for evaluation.
# evaluation_model: dashscope/your-fast-model-id
# Optional independent runtime acceptance model (otherwise uses the task model).
# verification_model: dashscope/your-fast-model-id
# Optional independent visual verifier; the model must support image input.
# visual_verification_model: dashscope/qwen3.8-omni-flash

providers:
  dashscope:
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
    models:
      your-model-id:
        temperature: 0
        max_completion_tokens: 8192
        context_window_tokens: 128000
        request_options:
          enable_thinking: true
      your-fast-model-id:
        request_options:
          enable_thinking: false
      qwen3.8-omni-flash:
        input_modalities: [text, image]
        context_window_tokens: 1000000
        max_completion_tokens: 4096
        request_options:
          modalities: [text]
          reasoning_effort: none
          response_format:
            type: json_object
```

Start the backend with this file explicitly:

```bash
uv run loom serve --data-dir .loom/service --config config.yaml
```

The key under `models` is the actual API model ID; no separate `model` field is needed.
All model selections and role references use `provider/model`, such as `dashscope/your-model-id`.
`api_key_env` resolves the key from the backend's environment or `.env`.

To enable visual acceptance, uncomment `visual_verification_model` and use a
provider endpoint that serves the selected model. The Qwen3.8-Omni-Flash example
uses JSON Mode with local response validation. `verification_model` handles
general acceptance and `visual_verification_model` handles image judgments;
they can be selected separately.

Provider groups share only `base_url` and `api_key_env`; all other parameters belong to each model. The same model ID may appear under different providers. To keep different parameter profiles for the same API model, use separate provider groups (for example, `dashscope` and `dashscope_judge`). Settings → Models edits provider connections and their nested model profiles. Legacy configurations remain readable and are migrated when saved in Settings; do not mix top-level `models` with `providers`. Migration retains `model_aliases` only for old session/knowledge/evaluation references; aliases do not appear in selections.

Each model defaults to the OpenAI-compatible Chat API adapter (`provider: openai`).
`max_completion_tokens` caps output including reasoning content.

Acceptance automatically budgets input at 60% of the verification model's context window, using a multilingual token estimate and reserving space for output. It no longer applies a fixed character cap by default. Known model capacities are resolved locally; for custom models or deployments, set `context_window_tokens` under `providers.<provider>.models.<model-id>` in `config.yaml` (for example, `context_window_tokens: 128000`). Unknown models use a visibly labeled 32,768-token fallback. This metadata is not sent as an API parameter. An explicitly configured task-level `acceptance.max_prompt_chars` still acts as an additional cap; omit it or set it to `null` for automatic budgeting. Oversized evidence blocks with estimated usage and capacity details; it is not silently truncated. These estimates are separate from provider-reported usage.
`request_options` passes provider-specific fields, such as supported thinking
options, to both streaming and non-streaming requests. Options must match the
chosen provider/model and cannot override Loom-owned fields such as
`messages`, `tools`, or `stream`.

Without `--config`, the service uses `LOOM_LLM_*` environment settings;
`OPENAI_MODEL`, `OPENAI_BASE_URL`, and `OPENAI_API_KEY` are fallbacks.
An existing `config.yaml` is not automatically selected by `loom serve`.

### Service and client settings

Model keys and model configuration belong to the **service process**.
To start it from a different directory, use absolute paths:

```bash
LOOM_ENV_FILE=/absolute/path/to/.env uv run --project /absolute/path/to/loom loom serve \
  --data-dir /absolute/path/to/service-data \
  --config /absolute/path/to/config.yaml
```

| Setting                   | Default / purpose                                                             |
| ------------------------- | ----------------------------------------------------------------------------- |
| `--data-dir`              | `.loom/service`; persistent session and knowledge data.                       |
| `--port`                  | `8765`; local HTTP API and Web frontend.                                      |
| `--max-active-runs`       | `2`; concurrent session workers. Conflicting workspace writes are serialized. |
| `--no-web`                | Disable bundled Web assets and retain the API.                                |
| `LOOM_ENV_FILE`           | `.env`; model/embedding secrets read by the backend.                          |
| `LOOM_SERVICE_URL`        | `http://127.0.0.1:8765`; client connection URL.                               |
| `LOOM_SERVICE_TOKEN_FILE` | `.loom/service/credential`; client credential file.                           |

Use the printed credential path if the backend has a different data directory:

```bash
export LOOM_SERVICE_URL=http://127.0.0.1:8765
export LOOM_SERVICE_TOKEN_FILE=/absolute/path/to/service-data/credential
uv run loom
```

Alternatively use `loom --url URL --token-file PATH`, or
`loom session --url URL --token-file PATH list`.
These connection credentials are separate from the model API key.

### Task tools and workflows

Model configuration selects **which model to call**. A task specification selects
**which tools, resources, context, workflow, and outputs the task uses**.
Web templates provide defaults; advanced configurations use `--task-spec`:

```bash
uv run loom session create 'Read https://www.python.org/about/ and write a cited summary' \
  --task-spec examples/task-specs/research.yaml
```

See the [general task/plugin guide](docs/general-task-runtime.md) and the
[General](examples/task-specs/general.yaml),
[Research](examples/task-specs/research.yaml), and
[Coding](examples/task-specs/coding.yaml) examples. Directory resource URIs in an
explicit specification resolve relative to that file; `--workspace` does not
override those resource bindings.

For the legacy planning workflow, `--plan-mode auto` lets the model enter
planning as needed; `force` requires a plan first; `off` uses the ReAct path
without planning tools. General/Research templates use the dynamic workflow.

### Execution host and shell

Execution and acceptance system prompts include the current worker's OS,
version, CPU architecture, execution shell and arguments, login shell, container
detection, and Docker CLI availability. These describe where tools actually run:
inside a container, the reported OS is the container's OS. The browser client's
machine does not determine command syntax. Resuming an existing session refreshes
the environment facts.

`shell_execute` defaults to `/bin/bash -o pipefail -c`; it does not automatically
switch to the login shell. A macOS login shell may be zsh while tools execute
through Bash. To configure the execution shell, set it in the **task specification**:

```yaml
execution_runtime:
  plugin: native_os
  shell: ["/bin/bash", "-o", "pipefail", "-c"]
```

Prompts include platform-specific guidance, such as differences between macOS
BSD utilities and GNU flags. `process_execute` receives literal argv without
shell expansion. Container detection uses Docker/Podman markers and cgroups;
absent markers do not prove execution on a bare host. Docker CLI presence is
recorded separately from container detection. Daemon connectivity is labeled
unprobed and must be checked before relying on Docker.

### Knowledge bases and embeddings

| Engine           | Setup and use                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `sqlite_fts`     | Default keyword retrieval; no embedding service.                                               |
| `sqlite_hybrid`  | Keyword + vector retrieval; requires an embedding profile.                                     |
| `yakdb_local`    | Separately installed YakDB; local PDF/Office/text parsing and retrieval.                       |
| `lightrag_local` | `uv sync --extra knowledge-graph`; requires a configured indexing model and embedding profile. |

In Web, add an embedding profile with the **complete embeddings URL**
(for example `http://localhost:11434/v1/embeddings`), an installed model ID,
and an optional API-key environment-variable name. Then create the base with
the selected engine and attach it to a session.

LightRAG website indexing includes LLM entity/relation extraction and embeddings;
a large document may require many model calls and chunks. Data is stored locally,
while selected model/embedding services receive the content needed for their
requests. Changing the bound model or embedding identity requires a new base.
See [Knowledge bases](docs/knowledge-bases.md) for limits, timeouts, and recovery.

## One-shot tasks

Run a task directly without the persistent service:

```bash
uv run loom task 'Audit this project and suggest improvements' \
  --workspace /absolute/path/to/project --config config.yaml --model dashscope/your-model-id --tui

uv run loom task 'Read https://www.python.org/about/ and summarize with sources' \
  --task-spec examples/task-specs/research.yaml --config config.yaml
```

Unlike `loom serve`, `loom task` discovers `./config.yaml` or `./config.yml`
automatically. Command-line options override defaults. Optional `task` and
`run` blocks apply to one-shot execution:

```yaml
task:
  workspace: .
  expected_outputs:
    - A Markdown analysis report.

run:
  tui: true
  stream: true
  plan_mode: auto
  trace_path_template: runs/{task_slug}-{model}-{timestamp}.jsonl
  max_steps: 100
  timeout_ms: 120000
```

`timeout_ms` is a per-step timeout. These blocks do not configure persistent
session limits. With `--task-spec`, one-shot execution does not inherit the
`task` block; model and `run` settings still apply. One-shot runs write JSONL
traces under `runs/` by default and are tied to their launching process.

## Further reading

- [Web frontend](docs/web-frontend.md) and [TUI presentation](src/loom/tui/README.md)
- [Session creation and recommendations](docs/session-setup.md)
- [Task plugins, workflows, and report output](docs/general-task-runtime.md)
- [Knowledge bases and LightRAG sync](docs/knowledge-bases.md)
- [External tools: MCP and CLI](docs/external-tools.md)
- [Runtime acceptance: implementation and configuration](docs/superpowers/specs/2026-10-10-runtime-acceptance-gate-design.md)
- [Web / Canvas / WebGL visual observation and acceptance (Chinese)](docs/visual-observation-and-acceptance.zh-CN.md)
- [Session trajectory analysis](docs/session-trajectory-analysis.md)
- [Trace effectiveness analysis](docs/trace-effectiveness-analysis.md)
- [Meta-Harness: optimization, experiments, and governance](docs/meta-harness.md)
- [Tool execution and progress](docs/tool-execution-and-progress-review.md)

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build

# Web tests; Node is needed only for development
npm --prefix tests/web ci
npm --prefix tests/web test
```

Live model smoke tests are opt-in:

```bash
LOOM_RUN_LIVE_LLM=1 uv run pytest tests/integration/test_live_llm_smoke.py -q
```

The project uses a `src/loom/` package layout; the Web frontend is packaged
inside the Python distribution.
