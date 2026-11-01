# Loom

[中文文档](README.zh-CN.md)

Loom is a local AI agent runtime for coding, research, and general tasks. Give it an objective, a model, and tools; it plans, executes, verifies, and learns from every run. Follow the work from Web or TUI and resume from saved checkpoints.

## Key features

- **Autonomous agents (opt-in):** assign a long-term objective and Loom plans roadmaps and stages, dispatches work packages through an execution queue, recovers bounded budget and execution failures, and reuses related sessions. A separate analysis queue studies every finished run without blocking delivery; human corrections and method guidance are recorded as memory. Enable with `loom serve --enable-agents`; see the [runtime and scheduling design (Chinese)](docs/design/autonomous-agents.zh-CN.md).
- **Self-iteration from execution paths:** Deep Eval analyzes each terminal run and extracts accepted memories — stage contributions, environment facts, failure lessons, and better workflows — which later sessions retrieve within the same workspace. Trace and trajectory analysis distinguish goal acceptance from execution quality, and Meta-Harness turns the accumulated evidence into governed optimization experiments. See [Deep eval Memory](docs/design/deep-eval-memory.md), [session trajectory analysis](docs/design/session-trajectory-analysis.md), and [Meta-Harness](docs/design/meta-harness.md).
- **Independent acceptance before completion:** task- and workspace-specific criteria are checked with actual commands plus a separate semantic review; optional visual criteria replay the app in a real browser and let an image model judge actual screenshots. Failed candidates enter bounded repair with concrete blockers. [Acceptance design](docs/design/2026-10-10-runtime-acceptance-gate-design.md).
- **Durable sessions across Web and TUI:** conversations, checkpoints, tool results, artifacts, and events survive frontend disconnects and service restarts. Completed sessions accept follow-up rounds.
- **Configurable models, tools, and workflows:** OpenAI-compatible model profiles; file, shell, URL, MCP, and CLI tools with LLM recommendation; dynamic workflows or Plan & Execute.
- **Local knowledge and cited reports:** keyword, hybrid vector, YakDB, and LightRAG retrieval with resumable website sync. Reports keep source citations and can be saved to a writable workspace. [Knowledge bases](docs/design/knowledge-bases.md).
- **Failure diagnosis:** investigate a saved run read-only or in an isolated worktree with regression tests, then review and merge the patch. [Session self-diagnosis (Chinese)](docs/design/session-self-diagnosis.zh-CN.md).

The current general task runtime executes tools on the host OS and targets macOS/Linux with a single local user; Docker/VM task backends are not implemented.

## Quick start

Requirements: Python 3.11+, `uv`, and access to an OpenAI-compatible model API. Run the following `uv` commands from the Loom repository directory.

### 1. Install

```bash
uv sync
```

Optional extras: `--extra knowledge-graph` for LightRAG, and `--extra browser --inexact` plus `PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv/bin/python -m playwright install chromium` for browser interaction and visual acceptance.

### 2. Configure a model

Create a local `.env` file (ignored by Git) with your provider's values:

```dotenv
LOOM_LLM_MODEL=your-model-id
LOOM_LLM_BASE_URL=https://your-provider.example/v1
LOOM_LLM_API_KEY=your-api-key
```

Named models and provider options are covered under [Configuration](#configuration).

### 3. Start the service

```bash
uv run loom serve --data-dir .loom/service
```

Keep this process running. By default it listens on `127.0.0.1:8765`, hosts the Web frontend, and writes its connection credential to `.loom/service/credential`. Keep the data directory between restarts to retain sessions and knowledge bases.

### 4. Open Web or TUI

For **Web**, open <http://127.0.0.1:8765/web/> and load the credential file. For **TUI**, open another terminal in the Loom repository:

```bash
uv run loom --workspace /absolute/path/to/project
```

A new session is saved immediately and waits for your first task.

## Using the Web frontend

1. Connect using the credential printed by `loom serve`.
2. Open the new-session panel, enter your objective, and pick **General**, **Research**, or **Coding**; **Recommend with LLM** can suggest the tools, model, and knowledge bases. For page interaction or visual acceptance, select **Browser & visual evidence** plus a workspace.
3. Follow the conversation and expand **Process** to inspect model requests, tool arguments, results, and errors.
4. Send follow-up guidance or answer pending questions; **Pause**, **Resume**, and **Stop** control execution.

**Complete task** closes the task; **Restart task** reruns the current goal in a new run with a fresh plan, fresh acceptance checks, and reset budgets — history is retained as bounded background, old checkpoints are not reused, and workspace files stay as they are. **Session info → Edit task → Save & restart** changes the objective, model, limits, plugins, tool collections, knowledge bases, and external tools atomically. CLI equivalent: `loom session restart SESSION_ID`.

**Evaluation / Evolve → Session diagnosis** investigates a failed session in a read-only loop or an isolated worktree, and merges reviewed patches back after conflict checks. The **Knowledge bases** view manages imports, embeddings, website sync, and graph exploration; the **External tools** view registers MCP and CLI connections; **Trajectory** opens execution analysis; **Outputs** lists saved reports. Details: [Web guide](docs/design/web-frontend.md) and [session setup guide](docs/design/session-setup.md).

Closing the tab leaves backend tasks running; reloading requires reconnecting with the credential. The frontend ships inside the Python package and needs no Node runtime.

### Visual acceptance

With the browser collection installed, acceptance criteria can replay the candidate in an independent browser: scenarios wait for real rendering, run interaction steps, capture frames, and let a separate image model judge the screenshots alongside deterministic assertions. Configure an image-capable `visual_verification_model` (example below) or pick one under **Settings → Models**. Setup, scenarios, and evidence boundaries: [visual observation and acceptance guide (Chinese)](docs/design/visual-observation-and-acceptance.zh-CN.md).

## Autonomous agents

```bash
uv run loom serve --data-dir .loom/service --enable-agents
```

An agent holds a role and a long-term objective. The scheduler plans roadmaps and stages, dispatches work packages into a bounded execution queue, renews exhausted budgets within configured limits, and pauses with a concrete reason and proposal when recovery is impossible. Finished runs flow into a separate analysis queue for Deep Eval and memory extraction. Operation, recovery, and human oversight: [autonomous agents guide (Chinese)](docs/design/autonomous-agents.zh-CN.md).

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

The TUI shows sessions, the plan, streaming execution, expandable details, and the final result. `Ctrl+C` or `Ctrl+Q` disconnects the frontend while the backend continues. Session commands also work without the TUI:

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

When an interrupted tool's effect cannot be confirmed, Loom asks for recovery verification before continuing. New sessions default to a **10M token budget**; inspect or change it with `/budget` in the TUI or `uv run loom session budget SESSION_ID [20M]`. Pause active work before changing its budget; resume renews the active-time allowance while preserving token, step, and model-call usage.

## Configuration

### Model profiles: `config.yaml`

For multiple models or provider-specific options, create a configuration file:

```yaml
default_model: dashscope/your-model-id
# Optional: select a separately configured judge model for evaluation.
# evaluation_model: dashscope/your-fast-model-id
# Optional judge-only overrides; use values supported by the selected model.
# evaluation_request_options:
#   enable_thinking: true
#   reasoning_effort: low
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

The key under `models` is the actual API model ID; all selections use `provider/model` references, and `api_key_env` resolves the key from the backend's environment or `.env`. Provider groups share only `base_url` and `api_key_env`; keep different parameter profiles for the same API model in separate provider groups. Legacy top-level `models` configurations remain readable and are migrated when saved in **Settings → Models**.

`request_options` passes provider-specific fields (such as thinking options) to streaming and non-streaming requests. Acceptance budgets input at 60% of the verification model's context window automatically; set `context_window_tokens` per model to override the labeled fallback. Without `--config`, the service uses `LOOM_LLM_*` environment settings, with `OPENAI_*` as fallbacks.

### Service and client settings

Model keys and model configuration belong to the **service process**. To start it from a different directory, use absolute paths:

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
| `--enable-agents`         | Off; enable autonomous agent scheduling.                                      |
| `--no-web`                | Disable bundled Web assets and retain the API.                                |
| `LOOM_ENV_FILE`           | `.env`; model/embedding secrets read by the backend.                          |
| `LOOM_SERVICE_URL`        | `http://127.0.0.1:8765`; client connection URL.                               |
| `LOOM_SERVICE_TOKEN_FILE` | `.loom/service/credential`; client credential file.                           |

If the backend uses a different data directory, point clients at its credential (`export LOOM_SERVICE_TOKEN_FILE=/path/to/credential`) or pass `--url URL --token-file PATH`. These connection credentials are separate from the model API key.

### Task tools and workflows

Model configuration selects **which model to call**; a task specification selects **which tools, resources, context, workflow, and outputs the task uses**. Web templates provide defaults; advanced configurations use `--task-spec` (see the [General](examples/task-specs/general.yaml), [Research](examples/task-specs/research.yaml), and [Coding](examples/task-specs/coding.yaml) examples). Details: [general task/plugin guide](docs/design/general-task-runtime.md).

### Execution host and shell

Execution and acceptance prompts include the worker's actual OS, architecture, shell, and container markers, refreshed when a session resumes. The execution shell defaults to `/bin/bash -o pipefail -c`; override it in the task specification:

```yaml
execution_runtime:
  plugin: native_os
  shell: ["/bin/bash", "-o", "pipefail", "-c"]
```

### Knowledge bases and embeddings

| Engine           | Setup and use                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `sqlite_fts`     | Default keyword retrieval; no embedding service.                                               |
| `sqlite_hybrid`  | Keyword + vector retrieval; requires an embedding profile.                                     |
| `yakdb_local`    | Separately installed YakDB; local PDF/Office/text parsing and retrieval.                       |
| `lightrag_local` | `uv sync --extra knowledge-graph`; requires a configured indexing model and embedding profile. |

In Web, add an embedding profile (complete embeddings URL, model ID, optional API-key variable), create the base with the selected engine, and attach it to a session. Limits, timeouts, and recovery: [Knowledge bases](docs/design/knowledge-bases.md).

## One-shot tasks

Run a task directly without the persistent service:

```bash
uv run loom task 'Audit this project and suggest improvements' \
  --workspace /absolute/path/to/project --config config.yaml --model dashscope/your-model-id --tui
```

Unlike `loom serve`, `loom task` discovers `./config.yaml` automatically, and can reuse accepted Deep eval memories from a service data directory with `--memory-store`. Options, the `run` defaults block, and trace output: [general task/plugin guide](docs/design/general-task-runtime.md).

## Further reading

- [Documentation index](docs/index.md)
- [Web frontend](docs/design/web-frontend.md) and [TUI presentation](src/loom/tui/README.md)
- [Session creation and recommendations](docs/design/session-setup.md)
- [External tools: MCP and CLI](docs/design/external-tools.md)
- [Runtime acceptance: implementation and configuration](docs/design/2026-10-10-runtime-acceptance-gate-design.md)
- [Trace effectiveness analysis](docs/design/trace-effectiveness-analysis.md)
- [Meta-Harness: optimization, experiments, and governance](docs/design/meta-harness.md)

## Development

```bash
uv run python -m pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build

# Web tests; Node is needed only for development
npm --prefix tests/web ci
npm --prefix tests/web test
```

Live model smoke tests are opt-in:

```bash
LOOM_RUN_LIVE_LLM=1 uv run python -m pytest tests/integration/test_live_llm_smoke.py -q
```

The project uses a `src/loom/` package layout; the Web frontend is packaged inside the Python distribution.
