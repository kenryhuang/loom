from pathlib import Path

from loom.examples.real_project_smoke import (
    RealProjectSmokeConfig,
    inspect_project,
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
