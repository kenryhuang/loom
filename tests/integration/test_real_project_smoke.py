from pathlib import Path
import sys

from loom.examples.real_project_smoke import (
    RealProjectSmokeConfig,
    inspect_project,
    run_command,
    run_smoke_test,
    run_yakdb_cli_smoke,
    synthesize_report,
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
