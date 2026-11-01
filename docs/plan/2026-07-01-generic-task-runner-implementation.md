# Generic Task Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first generic Loom task runner with config-file model selection, multi-model support, project-audit profile, basic tools, trace/TUI integration, and a CLI.

**Architecture:** Add a focused `loom.tasks` package that compiles a `TaskRequest` into existing Loom primitives: `Context`, `ToolRef`, `MinimalLoopDefinition`, `create_llm_step_function`, `RuntimeRegistry`, trace store, and optional TUI plugin. Provider selection comes from a TOML config file with multiple named models and falls back to current env-based OpenAI config.

**Tech Stack:** Python 3.11 dataclasses, `tomllib`, existing Loom core/runtime/llm/tools/tui modules, pytest.

---

## Files

- Create `src/loom/tasks/__init__.py`: public task runner exports.
- Create `src/loom/tasks/request.py`: task request/result dataclasses.
- Create `src/loom/tasks/config.py`: TOML config loading and named model provider construction.
- Create `src/loom/tasks/profiles.py`: deterministic profile selection and profile constraints.
- Create `src/loom/tasks/tools.py`: workspace-scoped basic task tools.
- Create `src/loom/tasks/runner.py`: context/loop assembly and `run_generic_task`.
- Create `src/loom/tasks/cli.py`: command-line parser and main function.
- Create `src/loom/tasks/run.py`: `python -m loom.tasks.run` entrypoint.
- Modify `src/loom/__init__.py` only if package export is necessary.
- Create `tests/tasks/test_task_config.py`.
- Create `tests/tasks/test_task_runner.py`.
- Create `tests/tasks/test_task_cli.py`.
- Modify `tests/test_package_structure.py` to include the new `tasks` package.

## TOML Config Shape

The first implementation supports:

```toml
default_model = "main"

[models.main]
provider = "openai"
model = "qwen3.7-max"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
temperature = 0
max_tokens = 8192

[models.fast]
provider = "openai"
model = "qwen-plus"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
```

`api_key` is also accepted for local/private configs, but `api_key_env` is preferred.

## Task 1: Config File and Multi-Model Provider

**Files:**
- Create `tests/tasks/test_task_config.py`
- Create `src/loom/tasks/config.py`
- Create `src/loom/tasks/__init__.py`

- [x] **Step 1: Write failing tests**

Tests should cover:

```python
def test_load_task_config_reads_multiple_named_models(tmp_path):
    path = tmp_path / "loom-task.toml"
    path.write_text(
        '''
default_model = "main"

[models.main]
provider = "openai"
model = "qwen-main"
base_url = "https://example.test/v1"
api_key_env = "MAIN_KEY"
temperature = 0.2
max_tokens = 1234

[models.fast]
provider = "openai"
model = "qwen-fast"
base_url = "https://example.test/v1"
api_key = "inline-key"
''',
        encoding="utf-8",
    )

    loaded = load_task_config(path).unwrap()

    assert loaded.default_model == "main"
    assert loaded.models["main"].model == "qwen-main"
    assert loaded.models["main"].api_key_env == "MAIN_KEY"
    assert loaded.models["main"].temperature == 0.2
    assert loaded.models["main"].max_tokens == 1234
    assert loaded.models["fast"].api_key == "inline-key"
```

```python
def test_create_provider_from_task_config_selects_named_model(tmp_path):
    config = TaskRunnerConfig(
        default_model="main",
        models={
            "main": ModelConfig(provider="openai", model="qwen-main", base_url="https://example.test/v1", api_key_env="MAIN_KEY"),
            "fast": ModelConfig(provider="openai", model="qwen-fast", base_url="https://example.test/v1", api_key="inline-key"),
        },
    )

    provider = create_provider_from_task_config(config, model_name="fast").unwrap()

    assert provider.model == "qwen-fast"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "inline-key"
```

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/tasks/test_task_config.py -q
```

Expected: fail because `loom.tasks.config` does not exist.

- [x] **Step 3: Implement minimal config module**

Create dataclasses:

```python
ModelConfig(provider, model, base_url, api_key=None, api_key_env=None, temperature=None, max_tokens=None)
TaskRunnerConfig(default_model=None, models={})
```

Implement:

```python
load_task_config(path) -> Result
create_provider_from_task_config(config, model_name=None, env=None) -> Result
```

Use `tomllib` and existing `create_openai_provider`.

- [x] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/tasks/test_task_config.py -q
```

Expected: pass.

## Task 2: Task Contracts, Profiles, and Context Assembly

**Files:**
- Create `tests/tasks/test_task_runner.py`
- Create `src/loom/tasks/request.py`
- Create `src/loom/tasks/profiles.py`
- Create `src/loom/tasks/runner.py`

- [x] **Step 1: Write failing tests**

Tests should cover:

```python
def test_make_task_context_maps_request_to_loom_layers(tmp_path):
    request = TaskRequest(
        objective="Audit this project",
        workspace=tmp_path,
        profile="project_audit",
        constraints=("Do not modify source files.",),
        expected_outputs=("markdown report",),
    )

    context = make_task_context(request).unwrap()

    assert context.goal.objective == "Audit this project"
    assert context.goal.criteria[0].description == "markdown report"
    assert context.identity.role == "project audit task runner"
    assert any("Do not modify source files." in item.description for item in context.identity.constraints)
    assert context.metadata["profile"] == "project_audit"
    assert context.metadata["workspace"] == str(tmp_path)
    assert {tool.id for tool in context.affordances.tools} >= {"read_file", "write_file", "shell_execute", "finish"}
```

```python
def test_auto_profile_selects_project_audit_for_workspace_audit(tmp_path):
    request = TaskRequest("Audit this project and suggest improvements", workspace=tmp_path)

    profile = select_task_profile(request)

    assert profile.id == "project_audit"
```

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py::test_make_task_context_maps_request_to_loom_layers tests/tasks/test_task_runner.py::test_auto_profile_selects_project_audit_for_workspace_audit -q
```

Expected: fail because contracts/context assembly do not exist.

- [x] **Step 3: Implement minimal request/profile/context code**

Implement `TaskRequest`, `TaskRunOptions`, `TaskRunResult`, `TaskProfile`.

Implement deterministic `select_task_profile`:

- explicit profile wins
- objective containing audit/smoke/project with workspace selects `project_audit`
- otherwise `general`

Implement `make_task_context(request) -> Result`:

- validate workspace if provided
- compile constraints into `IdentityLayer.constraints`
- compile expected outputs into `GoalLayer.criteria`
- add tool refs to `AffordanceLayer`
- attach profile/workspace/task metadata

- [x] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py::test_make_task_context_maps_request_to_loom_layers tests/tasks/test_task_runner.py::test_auto_profile_selects_project_audit_for_workspace_audit -q
```

Expected: pass.

## Task 3: Workspace Tools and Generic Runner

**Files:**
- Modify `tests/tasks/test_task_runner.py`
- Create `src/loom/tasks/tools.py`
- Modify `src/loom/tasks/runner.py`

- [x] **Step 1: Write failing tests**

Tests should cover:

```python
def test_run_generic_task_executes_llm_tool_loop_and_returns_finish_report(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nA demo project.\n", encoding="utf-8")
    provider = FakeTaskProvider()

    result = asyncio.run(
        run_generic_task(
            TaskRequest("Audit this project", workspace=tmp_path, profile="project_audit"),
            provider=provider,
        )
    )

    assert result.ok
    assert "Demo audit" in result.value.output
    assert provider.calls >= 2
    assert any(message.role == "tool" for message in provider.messages_seen[-1])
```

The fake provider should first return `read_file` and `finish` tool calls, then
return final JSON with the same report.

- [x] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py::test_run_generic_task_executes_llm_tool_loop_and_returns_finish_report -q
```

Expected: fail because runner/tools do not exist.

- [x] **Step 3: Implement tools and runner**

Implement workspace-scoped tools:

- `read_file`
- `write_file`
- `shell_execute`
- `finish`

Implement `make_task_loop(provider, stream=False)`.

Implement `run_generic_task(request, provider=None, options=None, config=None, model_name=None)`.

Use:

- `create_llm_step_function(provider, stream=options.stream, max_tool_calls_per_step=None)`
- `create_runtime_registry(tools=make_task_tools(request))`
- `JsonlTraceStore` when trace path exists
- `run_with_plugins(..., plugins=(TuiPlugin(),))` when TUI is enabled
- existing runtime `run` otherwise

- [x] **Step 4: Run test to verify pass**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py::test_run_generic_task_executes_llm_tool_loop_and_returns_finish_report -q
```

Expected: pass.

## Task 4: CLI

**Files:**
- Create `tests/tasks/test_task_cli.py`
- Create `src/loom/tasks/cli.py`
- Create `src/loom/tasks/run.py`

- [x] **Step 1: Write failing tests**

Tests should cover:

```python
def test_parse_args_accepts_config_and_model(tmp_path):
    options = parse_args(
        (
            "Audit this project",
            "--workspace",
            str(tmp_path),
            "--config",
            str(tmp_path / "loom-task.toml"),
            "--model",
            "fast",
            "--profile",
            "project_audit",
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
            "--tui",
            "--stream",
        )
    )

    assert options.request.objective == "Audit this project"
    assert options.request.workspace == tmp_path
    assert options.config_path == tmp_path / "loom-task.toml"
    assert options.model == "fast"
    assert options.options.tui is True
    assert options.options.stream is True
```

- [x] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/tasks/test_task_cli.py -q
```

Expected: fail because CLI module does not exist.

- [x] **Step 3: Implement CLI**

Support:

```bash
python -m loom.tasks.run "Audit this project" \
  --workspace /path/to/project \
  --config loom-task.toml \
  --model fast \
  --profile auto \
  --trace-path .loom/debug/task.jsonl \
  --tui \
  --stream
```

Load config if provided. Select provider from named model. Fall back to
`create_env_openai_provider` when no config file is provided.

- [x] **Step 4: Run test to verify pass**

Run:

```bash
uv run pytest tests/tasks/test_task_cli.py -q
```

Expected: pass.

## Task 5: Package Structure and Verification

**Files:**
- Modify `tests/test_package_structure.py`
- Modify `src/loom/tasks/__init__.py`

- [x] **Step 1: Write/update package structure test**

Add `tasks` to expected submodules.

- [x] **Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/tasks tests/test_package_structure.py -q
```

Expected: pass.

- [x] **Step 3: Run full verification**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Expected: all pass.
