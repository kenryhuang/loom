from pathlib import Path

from loom.tasks.cli import parse_task_cli_args


def test_parse_task_cli_args_supports_config_file_and_model_alias(tmp_path):
    config_path = tmp_path / "models.toml"
    trace_path = tmp_path / "trace.jsonl"

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


def test_parse_task_cli_args_defaults_workspace_to_current_directory():
    parsed = parse_task_cli_args(["Summarize the repo"])

    assert parsed.request.objective == "Summarize the repo"
    assert parsed.request.workspace == Path.cwd()
    assert parsed.config_path is None
    assert parsed.model_name is None


def test_parse_task_cli_args_defaults_trace_path_to_runs_directory():
    parsed = parse_task_cli_args(["Summarize the repo"])

    assert parsed.options.trace_path is not None
    assert parsed.options.trace_path.parent == Path("runs")
    assert parsed.options.trace_path.name.startswith("loom-task-")
    assert parsed.options.trace_path.suffix == ".jsonl"
