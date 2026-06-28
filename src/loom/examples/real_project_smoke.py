"""Real project smoke audit example for Loom."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RealProjectSmokeConfig:
    target_path: Path
    smoke_command: tuple[str, ...] = ("uv", "run", "pytest", "-q")
    cli_smoke_enabled: bool = True
    command_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_path", Path(self.target_path))
        object.__setattr__(self, "smoke_command", tuple(self.smoke_command))


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    path: str
    name: str
    purpose: str
    files: tuple[str, ...]
    git_status: str
    tech_stack: tuple[str, ...] = ()


def inspect_project(path: str | Path) -> ProjectInfo:
    target = Path(path)
    files = tuple(sorted(item.name for item in target.iterdir())) if target.exists() else ()
    pyproject = _read_text(target / "pyproject.toml")
    readme = _read_text(target / "README.md")
    name = _extract_pyproject_name(pyproject) or target.name
    purpose = _extract_readme_purpose(readme) or "Purpose unavailable."
    return ProjectInfo(
        path=str(target),
        name=name,
        purpose=purpose,
        files=files,
        git_status=_git_status(target),
        tech_stack=_infer_tech_stack(files),
    )


def synthesize_report(
    config: RealProjectSmokeConfig,
    project_info: ProjectInfo,
    *,
    smoke,
    cli_smoke,
) -> str:
    smoke_summary = _summarize_optional_result(smoke)
    cli_summary = _summarize_optional_result(cli_smoke)
    recommendations = _recommendations(project_info, smoke, cli_smoke)
    return "\n".join(
        [
            f"# Real Project Smoke Audit: {project_info.name}",
            "",
            "## Purpose",
            "",
            project_info.purpose,
            "",
            "## Repository State",
            "",
            project_info.git_status or "Git status unavailable or clean.",
            "",
            "## Smoke Test",
            "",
            smoke_summary,
            "",
            "## CLI Smoke",
            "",
            cli_summary,
            "",
            "## Improvement Directions",
            "",
            *[f"{index}. {item}" for index, item in enumerate(recommendations, start=1)],
            "",
            "## Target",
            "",
            str(config.target_path),
        ]
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _extract_pyproject_name(pyproject: str) -> str | None:
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith("name") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _extract_readme_purpose(readme: str) -> str | None:
    for line in readme.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<") or stripped.startswith("["):
            continue
        return stripped
    return None


def _git_status(target: Path) -> str:
    try:
        completed = subprocess.run(
            ("git", "status", "--short", "--branch"),
            cwd=target,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Git status unavailable."
    return (completed.stdout or completed.stderr).strip()


def _infer_tech_stack(files: tuple[str, ...]) -> tuple[str, ...]:
    stack = []
    if "pyproject.toml" in files:
        stack.append("python")
    if "package.json" in files:
        stack.append("node")
    if "Cargo.toml" in files:
        stack.append("rust")
    return tuple(stack)


def _summarize_optional_result(result) -> str:
    if result is None:
        return "Not run."
    if getattr(result, "skipped", False):
        return f"Skipped: {getattr(result, 'reason', 'no reason provided')}"
    exit_code = getattr(result, "exit_code", None)
    if exit_code is not None:
        status = "passed" if exit_code == 0 else "failed"
        return f"Command {status} with exit code {exit_code}."
    return str(result)


def _recommendations(project_info: ProjectInfo, smoke, cli_smoke) -> tuple[str, ...]:
    recommendations = []
    if project_info.name.lower() == "yakdb" or "yakdb" in project_info.path.lower():
        recommendations.extend(
            [
                "Exclude .yakdb/ internal storage from grep/search/index paths by default.",
                "Clean up watcher lifecycle warnings around _consume_queue.",
                "Keep README, CLI behavior, and package extras aligned around embedded mode.",
            ]
        )
    if not recommendations:
        recommendations.append("Keep smoke coverage close to primary user workflows.")
    return tuple(recommendations)
