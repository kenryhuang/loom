from pathlib import Path
from types import SimpleNamespace

from loom.core import ok
from loom.runtime import PlanMode
from loom.tasks.cli import parse_task_cli_args


def test_parse_task_cli_defaults_plan_mode_to_auto(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parsed = parse_task_cli_args(["Summarize"])

    assert parsed.options.plan_mode is PlanMode.AUTO


def test_cli_plan_mode_overrides_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[run]\nplan_mode = "force"\n', encoding="utf-8")

    parsed = parse_task_cli_args(
        ["Summarize", "--config", str(config), "--plan-mode", "off"]
    )

    assert parsed.options.plan_mode is PlanMode.OFF


def test_parse_task_cli_args_supports_config_file_and_model_alias(tmp_path):
    config_path = tmp_path / "models.toml"
    trace_path = tmp_path / "trace.jsonl"
    config_path.write_text(
        """
default_model = "main"

[models.deep]
provider = "openai"
model = "deep-model"
base_url = "https://example.test/v1"
api_key_env = "MAIN_KEY"
""",
        encoding="utf-8",
    )

    parsed = parse_task_cli_args(
        [
            "Audit this project",
            "--workspace",
            str(tmp_path),
            "--profile",
            "project_audit",
            "--constraint",
            "Do not edit source files.",
            "--expected-output",
            "markdown report",
            "--config",
            str(config_path),
            "--model",
            "deep",
            "--tui",
            "--stream",
            "--trace-path",
            str(trace_path),
            "--max-steps",
            "2",
            "--timeout-ms",
            "5000",
        ]
    )

    assert parsed.request.objective == "Audit this project"
    assert parsed.request.workspace == tmp_path
    assert parsed.request.profile == "project_audit"
    assert parsed.request.constraints == ("Do not edit source files.",)
    assert parsed.request.expected_outputs == ("markdown report",)
    assert parsed.config_path == config_path
    assert parsed.model_name == "deep"
    assert parsed.options.tui is True
    assert parsed.options.stream is True
    assert parsed.options.trace_path == trace_path
    assert parsed.options.max_steps == 2
    assert parsed.options.timeout_ms == 5000


def test_parse_task_cli_args_defaults_workspace_to_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parsed = parse_task_cli_args(["Summarize the repo"])

    assert parsed.request.objective == "Summarize the repo"
    assert parsed.request.workspace == Path.cwd()
    assert parsed.config_path is None
    assert parsed.model_name is None


def test_parse_task_cli_args_defaults_trace_path_to_runs_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parsed = parse_task_cli_args(["Summarize the repo"])

    assert parsed.options.trace_path is not None
    assert parsed.options.trace_path.parent == Path("runs")
    assert parsed.options.trace_path.name.startswith("loom-task-")
    assert parsed.options.trace_path.suffix == ".jsonl"


def test_parse_task_cli_args_uses_config_task_and_run_defaults(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_model: main
task:
  objective: Audit this project briefly
  workspace: workspace
  profile: project_audit
  expected_outputs:
    - Brief markdown audit report.
run:
  tui: false
  stream: true
  trace_path_template: runs/{task_slug}-{model}-{timestamp}.jsonl
  max_steps: 4
  timeout_ms: 9000
models:
  main:
    provider: openai
    model: qwen-main
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
""",
        encoding="utf-8",
    )

    parsed = parse_task_cli_args(["--config", str(config_path)])

    assert parsed.request.objective == "Audit this project briefly"
    assert parsed.request.workspace == workspace
    assert parsed.request.profile == "project_audit"
    assert parsed.request.expected_outputs == ("Brief markdown audit report.",)
    assert parsed.model_name == "main"
    assert parsed.options.tui is False
    assert parsed.options.stream is True
    assert parsed.options.trace_path is not None
    assert parsed.options.trace_path.parent == tmp_path / "runs"
    assert parsed.options.trace_path.name.startswith("audit-this-project-briefly-main-")
    assert parsed.options.trace_path.suffix == ".jsonl"
    assert parsed.options.max_steps == 4
    assert parsed.options.timeout_ms == 9000


def test_parse_task_cli_args_cli_values_override_config_defaults(tmp_path):
    config_workspace = tmp_path / "config-workspace"
    cli_workspace = tmp_path / "cli-workspace"
    config_workspace.mkdir()
    cli_workspace.mkdir()
    config_path = tmp_path / "config.yaml"
    cli_trace_path = tmp_path / "cli-trace.jsonl"
    config_path.write_text(
        """
default_model: main
task:
  objective: Config task
  workspace: config-workspace
  profile: project_audit
  expected_outputs:
    - Config report.
run:
  tui: true
  stream: true
  trace_path_template: runs/{task_slug}.jsonl
  max_steps: 4
  timeout_ms: 9000
models:
  main:
    provider: openai
    model: qwen-main
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
  fast:
    provider: openai
    model: qwen-fast
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
""",
        encoding="utf-8",
    )

    parsed = parse_task_cli_args(
        [
            "CLI task",
            "--config",
            str(config_path),
            "--workspace",
            str(cli_workspace),
            "--profile",
            "general",
            "--expected-output",
            "CLI report.",
            "--model",
            "fast",
            "--no-tui",
            "--no-stream",
            "--trace-path",
            str(cli_trace_path),
            "--max-steps",
            "2",
            "--timeout-ms",
            "3000",
        ]
    )

    assert parsed.request.objective == "CLI task"
    assert parsed.request.workspace == cli_workspace
    assert parsed.request.profile == "general"
    assert parsed.request.expected_outputs == ("CLI report.",)
    assert parsed.model_name == "fast"
    assert parsed.options.tui is False
    assert parsed.options.stream is False
    assert parsed.options.trace_path == cli_trace_path
    assert parsed.options.max_steps == 2
    assert parsed.options.timeout_ms == 3000


def test_parse_task_cli_args_discovers_default_config_yaml(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    Path("config.yaml").write_text(
        """
default_model: main
task:
  objective: Audit this project briefly
  workspace: workspace
  profile: project_audit
run:
  stream: true
  trace_path_template: runs/{task_slug}-{model}-{timestamp}.jsonl
models:
  main:
    provider: openai
    model: qwen-main
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
  glm:
    provider: openai
    model: glm-model
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
""",
        encoding="utf-8",
    )

    parsed = parse_task_cli_args(["--model", "glm"])

    assert parsed.config_path == Path("config.yaml")
    assert parsed.request.objective == "Audit this project briefly"
    assert parsed.request.workspace == workspace
    assert parsed.request.profile == "project_audit"
    assert parsed.model_name == "glm"
    assert parsed.options.stream is True
    assert parsed.options.trace_path is not None
    assert parsed.options.trace_path.parent == tmp_path / "runs"
    assert parsed.options.trace_path.name.startswith("audit-this-project-briefly-glm-")


def test_top_level_cli_dispatches_task_arguments_unchanged(monkeypatch):
    calls = []

    def fake_task_main(argv):
        calls.append(argv)
        return 23

    monkeypatch.setattr("loom.tasks.cli.main", fake_task_main)
    from loom.cli import main as loom_main

    code = loom_main(["task", "Audit this project", "--workspace", ".", "--model", "main"])

    assert code == 23
    assert calls == [["Audit this project", "--workspace", ".", "--model", "main"]]


def test_task_cli_main_returns_zero_after_success(monkeypatch, capsys):
    parsed = object()

    async def fake_run_task_cli(options):
        assert options is parsed
        return ok(SimpleNamespace(output="Task complete"))

    monkeypatch.setattr("loom.tasks.cli.parse_task_cli_args", lambda argv: parsed)
    monkeypatch.setattr("loom.tasks.cli.run_task_cli", fake_run_task_cli)
    from loom.tasks.cli import main

    code = main(["Audit this project"])

    assert code == 0
    assert capsys.readouterr().out == "Task complete\n"
