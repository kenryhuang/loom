import asyncio
import json
import sys
from pathlib import Path

from loom.core.models import ok
from loom.examples.real_project_smoke import (
    DEFAULT_YAKDB_PATH,
    CommandResult,
    RealProjectSmokeConfig,
    inspect_project,
    make_real_project_smoke_context,
    make_real_project_smoke_llm_context,
    make_real_project_smoke_loop,
    parse_args,
    parse_run_options,
    run_command,
    run_real_project_smoke,
    run_smoke_test,
    run_yakdb_cli_smoke,
    synthesize_report,
)
from loom.llm import LlmResponse, LlmToolCall, TokenUsage, build_system_prompt


class FakeSmokeProvider:
    model = "fake-smoke-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            return ok(
                LlmResponse(
                    content=None,
                    tool_calls=(
                        LlmToolCall("read-call", "read_file", json.dumps({"path": "README.md"})),
                        LlmToolCall("smoke-call", "shell_execute", json.dumps({"command": [sys.executable, "-c", "print('smoke ok')"]})),
                    ),
                    finish_reason="tool_calls",
                )
            )
        if self.calls == 2:
            return ok(
                LlmResponse(
                    content=None,
                    tool_calls=(LlmToolCall("finish-call", "finish", json.dumps({"report": "# Fake LLM Smoke Report\n\nThe LLM made this judgment."})),),
                    finish_reason="tool_calls",
                )
            )
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "reasoning": "I judged the evidence from the tools.",
                        "action": {
                            "kind": "custom",
                            "description": "Write the smoke audit report",
                            "input": {"report": "# Fake LLM Smoke Report\n\nThe LLM made this judgment."},
                        },
                        "alternatives": [],
                        "confidence": 0.83,
                    }
                ),
                usage=TokenUsage(10, 10, 20),
            )
        )


def test_inspect_project_extracts_readme_purpose_and_pyproject_name(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text("# SampleDB\n\nThe AI-native file database.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "sampledb"\n', encoding="utf-8")

    info = inspect_project(project)

    assert info.name == "sampledb"
    assert info.purpose == "The AI-native file database."
    assert "README.md" in info.files


def test_synthesize_report_includes_observed_sections(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)
    project_info = inspect_project(tmp_path)

    report = synthesize_report(config, project_info, smoke=None, cli_smoke=None)

    assert report.startswith("# Real Project Smoke Audit:")
    assert "## Purpose" in report
    assert "## Repository State" in report
    assert "## Smoke Test" in report
    assert "## Improvement Directions" in report


def test_run_command_captures_exit_code_stdout_and_stderr(tmp_path: Path):
    result = run_command(
        (sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)"),
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert result.exit_code == 2
    assert "out" in result.stdout
    assert "err" in result.stderr


def test_run_smoke_test_uses_configured_command(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path, smoke_command=(sys.executable, "-c", "print('smoke ok')"))

    result = run_smoke_test(config)

    assert result.exit_code == 0
    assert "smoke ok" in result.stdout


def test_yakdb_cli_smoke_skips_non_yakdb_projects(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)
    project_info = inspect_project(tmp_path)

    result = run_yakdb_cli_smoke(config, project_info)

    assert result.skipped is True
    assert "not detected" in result.reason


def test_real_project_smoke_loop_produces_report_and_trace(tmp_path: Path):
    project = tmp_path / "loop-project"
    project.mkdir()
    (project / "README.md").write_text("# Loop Project\n\nA project for loop smoke testing.\n", encoding="utf-8")
    config = RealProjectSmokeConfig(
        target_path=project,
        smoke_command=(sys.executable, "-c", "print('loop smoke ok')"),
        cli_smoke_enabled=False,
    )

    result = asyncio.run(run_real_project_smoke(config))

    assert result.ok
    assert result.value.metrics.steps == 1
    assert "loop smoke ok" in result.value.output
    assert len(result.value.traces) == 1
    assert len(result.value.traces[0].observations) == 4


def test_real_project_smoke_llm_prompt_requires_evidence_tool_calls(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()

    config = RealProjectSmokeConfig(target_path=project, cli_smoke_enabled=False)

    context = make_real_project_smoke_llm_context(config)

    assert context.ok
    tool_ids = tuple(tool.id for tool in context.value.affordances.tools)
    assert tool_ids == ("read_file", "write_file", "shell_execute", "finish")
    system_prompt = build_system_prompt(context.value)
    assert "Use read_file to inspect project files" in system_prompt
    assert "Use shell_execute to run the configured smoke command" in system_prompt
    assert "Do not enumerate the whole repository" in system_prompt
    assert "Call finish exactly once" in system_prompt
    assert "inspect-project" not in system_prompt


def test_real_project_smoke_llm_mode_uses_model_report(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text("# Sample\n\nA tiny sample project.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")

    config = RealProjectSmokeConfig(
        target_path=project,
        smoke_command=(sys.executable, "-c", "print('smoke ok')"),
        cli_smoke_enabled=False,
        command_timeout_seconds=10,
    )
    provider = FakeSmokeProvider()

    result = asyncio.run(run_real_project_smoke(config, provider=provider, llm=True))

    assert result.ok
    assert result.value.output == "# Fake LLM Smoke Report\n\nThe LLM made this judgment."
    assert provider.calls == 3
    assert "Exclude .yakdb" not in result.value.output


def test_real_project_smoke_persists_full_llm_loop_trace(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text("# Sample\n\nA tiny sample project.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")
    trace_path = tmp_path / "traces" / "real-project-smoke.jsonl"

    config = RealProjectSmokeConfig(
        target_path=project,
        smoke_command=(sys.executable, "-c", "print('smoke ok')"),
        cli_smoke_enabled=False,
        command_timeout_seconds=10,
        trace_path=trace_path,
    )

    result = asyncio.run(run_real_project_smoke(config, provider=FakeSmokeProvider(), llm=True))

    assert result.ok
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_records = [record for record in records if record["type"] == "event"]
    event_types = [record["eventType"] for record in event_records]
    for event_type in (
        "run.started",
        "step.started",
        "llm.requested",
        "llm.completed",
        "tool.started",
        "tool.completed",
        "step.completed",
        "run.completed",
    ):
        assert event_type in event_types
    assert any(record["type"] == "trace" for record in records)
    assert any(record["eventType"] == "tool.completed" and "input" in record["payload"] and "output" in record["payload"] for record in event_records)
    completed_tools = [record["payload"]["tool_id"] for record in event_records if record["eventType"] == "tool.completed"]
    assert completed_tools == ["read_file", "shell_execute", "finish"]
    assert any(record["eventType"] == "llm.requested" and "messages" in record["payload"] for record in event_records)


def test_real_project_smoke_context_rejects_missing_target(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path / "missing")

    result = make_real_project_smoke_context(config)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_loop_factory_returns_one_step_loop(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)

    loop = make_real_project_smoke_loop(config)

    assert loop.identity.role == "real project smoke auditor"
    assert loop.goal.objective.startswith("Audit")


def test_parse_args_defaults_to_yakdb_path():
    config = parse_args(())

    assert str(config.target_path) == DEFAULT_YAKDB_PATH
    assert config.smoke_command == ("uv", "run", "--no-sync", "pytest", "-q")


def test_parse_args_accepts_custom_path_and_smoke_command(tmp_path: Path):
    config = parse_args((str(tmp_path), "--smoke-command", "python -c pass", "--no-cli-smoke"))

    assert config.target_path == tmp_path
    assert config.smoke_command == ("python", "-c", "pass")
    assert config.cli_smoke_enabled is False


def test_parse_run_options_accepts_trace_path(tmp_path: Path):
    trace_path = tmp_path / "real-smoke.jsonl"

    options = parse_run_options((str(tmp_path), "--llm", "--trace-path", str(trace_path)))

    assert options.llm is True
    assert options.config.trace_path == trace_path


def test_yakdb_cli_smoke_uses_no_sync_to_avoid_lockfile_writes(tmp_path: Path, monkeypatch):
    project = tmp_path / "yakdb"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "yakdb"\n', encoding="utf-8")
    project_info = inspect_project(project)
    commands = []

    def fake_run_command(command, *, cwd, timeout_seconds):
        commands.append(command)
        return CommandResult(command, str(cwd), 0, "loom-real-case", "", 1)

    monkeypatch.setattr("loom.examples.real_project_smoke.run_command", fake_run_command)

    result = run_yakdb_cli_smoke(RealProjectSmokeConfig(target_path=project), project_info)

    assert not result.skipped
    assert commands
    assert all(command[:3] == ("uv", "run", "--no-sync") for command in commands)
