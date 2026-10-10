# Loom

[中文文档](README.zh-CN.md)

Loom is a local AI agent runtime for coding, research, and general tasks. Give it
an objective, a model, and the tools it may use; it runs a model–tool loop,
maintains a plan or workflow, and records the work and its results.

Use Loom to inspect a codebase and fix problems, read sources and write an
analysis report, or build a local knowledge base for repeated questions. Its
persistent service lets you follow the same task from a browser or terminal,
disconnect while work continues, and return to its saved state.

## Key features

- **Persistent sessions:** conversations, execution checkpoints, tool results,
  artifacts, and event history survive frontend disconnects and service restarts.
  A new message after a completed round starts another round in the same session.
- **Web and TUI:** both connect to the same backend and sessions, with streaming
  model output, tool details, task controls, history, and token usage.
- **Configurable tools and workflows:** choose file/shell tools, URL reading,
  report output, and knowledge retrieval. Task specifications configure the
  workspace, context management, execution runtime, and workflow separately.
- **Plans and bounded context:** support dynamic workflows and Plan & Execute,
  with context compaction that retains full original content in artifacts.
- **Reports with saved documents:** research reports retain their sources and
  artifact. When a writable workspace is bound, `create_report` also writes the
  report there, and the UI shows its saved path.
- **Local knowledge bases:** keyword, hybrid vector, YakDB, and LightRAG engines.
  LightRAG supports website sync, graph exploration, visible indexing progress,
  and resuming from completed document checkpoints.
- **Observable execution:** inspect model/tool activity and stored evidence;
  analyze traces or session trajectories. Advanced evaluation and Meta-Harness
  commands support experiments and governed optimization. Opt-in [behavior evaluation v3](docs/behavior-evaluation-v3.md) separates goal verification from intent, planning, progress, reasoning efficiency, and recovery.

The current general task runtime executes tools on the host OS. Loom targets
macOS/Linux and a single local user; Docker/VM task backends are not implemented.

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

Keep this process running. By default it listens on `127.0.0.1:8765), hosts the
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
   a configuration; review it before clicking **Create session**.
4. Follow the conversation and expand **Process** to inspect model requests,
   tool arguments, results, and errors. Workflow, budget, and output panels show
   the current task state.
5. Send additional guidance or answer a pending question. Use **Pause**,
   **Resume**, and **Stop** to control execution.

Coding and file/command tools require an existing workspace directory on the
service host. Research can run without a workspace; bind a writable directory
when you want analysis documents saved as files as well as artifacts. The
built-in research tools read supplied HTTP(S) URLs; they do not provide a search
engine or execute a JavaScript browser.

The **Knowledge bases** view manages imports, embedding profiles, website
sources, and graph exploration. Attach selected bases to a session so its agent
can retrieve them. The LightRAG **Sync activity** area shows the current
page/document, stage, saved document count, request usage, and recent activity;
**Resume sync** continues from saved progress after failure or cancellation.

**Trajectory** opens execution analysis for a session. **Outputs** exposes saved
document paths and report artifacts. See the [Web guide](docs/web-frontend.md)
and [session setup guide](docs/session-setup.md) for details.

Closing the tab leaves backend tasks running. The browser keeps its credential
in tab memory, so reloading requires reconnecting. The frontend ships with the
Python package; it needs no Node runtime or separate frontend server.

## Using the TUI and session CLI

```bash
# New session; the default workspace is the current directory
uv run loom

# New session using a named model from the service configuration
uv run loom --workspace /absolute/path/to/project --model main

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
default_model: main
# Optional: select a separately configured judge model for evaluation.
# evaluation_model: judge

models:
  main:
    provider: openai
    model: your-model-id
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 8192

  fast:
    provider: openai
    model: your-fast-model-id
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
```

Start the backend with this file explicitly:

```bash
uv run loom serve --data-dir .loom/service --config config.yaml
```

`main` and `fast` are Loom aliases; `model` is the provider's actual model ID.
Web/TUI model selections refer to the aliases configured on the service.
`api_key_env` resolves the key from the backend's environment or `.env`.

`provider: openai` means the OpenAI-compatible Chat API adapter.
`max_completion_tokens` caps output including reasoning content.
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
  --workspace /absolute/path/to/project --config config.yaml --model main --tui

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
