"""Real project smoke audit example for Loom."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core.models import (
    Action,
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    Observation,
    StateLayer,
    StepResult,
    ToolRef,
    Trace,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    err,
    freeze_context,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    now_iso,
    ok,
)
from loom.runtime.engine import create, create_runtime_registry, run

DEFAULT_YAKDB_PATH = "/Users/huanggui/workspace/yakDB"


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


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))


@dataclass(frozen=True, slots=True)
class CliSmokeResult:
    skipped: bool
    reason: str
    commands: tuple[CommandResult, ...] = ()
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "findings", tuple(self.findings))


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


def run_command(command: tuple[str, ...], *, cwd: str | Path, timeout_seconds: int) -> CommandResult:
    import time

    started = time.monotonic()
    try:
        completed = subprocess.run(
            tuple(command),
            cwd=Path(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            command=tuple(command),
            cwd=str(Path(cwd)),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=tuple(command),
            cwd=str(Path(cwd)),
            exit_code=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"Command timed out after {timeout_seconds}s",
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(
            command=tuple(command),
            cwd=str(Path(cwd)),
            exit_code=127,
            stdout="",
            stderr=str(exc),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )


def run_smoke_test(config: RealProjectSmokeConfig) -> CommandResult:
    return run_command(config.smoke_command, cwd=config.target_path, timeout_seconds=config.command_timeout_seconds)


def run_yakdb_cli_smoke(config: RealProjectSmokeConfig, project_info: ProjectInfo) -> CliSmokeResult:
    if not config.cli_smoke_enabled:
        return CliSmokeResult(True, "CLI smoke disabled.")
    if not _is_yakdb_project(project_info):
        return CliSmokeResult(True, "yakDB project not detected.")

    with tempfile.TemporaryDirectory(prefix="loom-yakdb-smoke-") as tmpdir:
        workspace = Path(tmpdir)
        notes = workspace / "docs" / "notes.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(
            "YakDB turns files into searchable text for agent workflows.\nSmoke test marker: loom-real-case.\n",
            encoding="utf-8",
        )
        commands = (
            ("uv", "run", "yakdb", "init", str(workspace), "--no-ocr"),
            ("uv", "run", "yakdb", "index", str(workspace), "--workspace", str(workspace)),
            ("uv", "run", "yakdb", "grep", "loom-real-case", str(workspace), "--workspace", str(workspace)),
            ("uv", "run", "yakdb", "read", "docs/notes.txt", "--workspace", str(workspace), "--numbered"),
        )
        results = []
        for command in commands:
            result = run_command(command, cwd=config.target_path, timeout_seconds=config.command_timeout_seconds)
            results.append(result)
            if result.exit_code != 0:
                return CliSmokeResult(False, "CLI smoke failed.", tuple(results), _cli_findings(tuple(results)))
        return CliSmokeResult(False, "CLI smoke completed.", tuple(results), _cli_findings(tuple(results)))


def make_real_project_smoke_context(config: RealProjectSmokeConfig):
    if not config.target_path.exists():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Real project smoke target does not exist",
                retryable=False,
                metadata={"target_path": str(config.target_path)},
            )
        )
    return ok(
        freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="real project smoke auditor"),
                goal=GoalLayer(objective=f"Audit real project smoke path for {config.target_path}", budget={"max_steps": 1}),
                state=StateLayer(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(
                    tools=(
                        ToolRef("inspect-project", "Inspect project metadata and repository state"),
                        ToolRef("run-smoke-test", "Run configured smoke command"),
                        ToolRef("run-cli-smoke", "Run project-specific CLI smoke path"),
                        ToolRef("synthesize-report", "Create deterministic markdown audit report"),
                    )
                ),
            )
        )
    )


def make_real_project_smoke_loop(config: RealProjectSmokeConfig) -> MinimalLoopDefinition:
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        started_at = now_iso()
        trace_id = new_trace_id()
        project_info = inspect_project(config.target_path)
        smoke = run_smoke_test(config)
        cli_smoke = run_yakdb_cli_smoke(config, project_info)
        report = synthesize_report(config, project_info, smoke=smoke, cli_smoke=cli_smoke)
        at = now_iso()
        actions = (
            Action(f"{trace_id}-inspect-action", "tool", "Inspect project", target="inspect-project", input={"target_path": str(config.target_path)}),
            Action(f"{trace_id}-smoke-action", "tool", "Run smoke test", target="run-smoke-test", input={"command": config.smoke_command}),
            Action(f"{trace_id}-cli-action", "tool", "Run CLI smoke", target="run-cli-smoke", input={"enabled": config.cli_smoke_enabled}),
            Action(f"{trace_id}-report-action", "tool", "Synthesize report", target="synthesize-report"),
        )
        observations = (
            Observation(f"{trace_id}-inspect-observation", "inspect-project", _project_info_value(project_info), at),
            Observation(f"{trace_id}-smoke-observation", "run-smoke-test", _command_result_value(smoke), at),
            Observation(f"{trace_id}-cli-observation", "run-cli-smoke", _cli_smoke_value(cli_smoke), at),
            Observation(f"{trace_id}-report-observation", "synthesize-report", {"report": report}, at),
        )
        decision = Decision(
            f"{trace_id}-decision",
            actions[-1],
            "Ran real project smoke audit and synthesized report.",
            actions[:-1],
            1.0,
            at,
        )
        next_context = freeze_context(
            Context(
                id=new_context_id(),
                run_id=context.run_id,
                created_at=context.created_at,
                identity=context.identity,
                goal=context.goal,
                state=StateLayer(observations=(*context.state.observations, *observations), decisions=(*context.state.decisions, decision)),
                knowledge=context.knowledge,
                affordances=context.affordances,
                parent_context_id=context.parent_context_id,
                metadata=context.metadata,
            )
        )
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=started_at,
            ended_at=now_iso(),
            duration_ms=0,
            input_context_id=context.id,
            output_context_id=next_context.id,
            outcome="pass" if smoke.exit_code == 0 else "fail",
            observations=observations,
            decisions=(decision,),
            actions=actions,
            tags=("example", "real-project-smoke"),
            metadata={"targetPath": str(config.target_path), "projectName": project_info.name},
        )
        return ok(StepResult(next_context, trace, observations[-1], report))

    def done_fn(context: Context, _runtime: Any):
        return ok(bool(context.state.decisions))

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="real project smoke auditor"),
        goal=GoalLayer(objective=f"Audit real project smoke path for {config.target_path}"),
        step=step_fn,
        done=done_fn,
    )


async def run_real_project_smoke(config: RealProjectSmokeConfig):
    context = make_real_project_smoke_context(config)
    if not context.ok:
        return context
    handle = create(make_real_project_smoke_loop(config), registry=create_runtime_registry())
    if not handle.ok:
        return handle
    return await run(handle.value, context.value, max_steps=1)


def parse_args(argv: tuple[str, ...] | list[str] | None = None) -> RealProjectSmokeConfig:
    parser = argparse.ArgumentParser(description="Run a Loom real project smoke audit.")
    parser.add_argument("path", nargs="?", default=DEFAULT_YAKDB_PATH, help="Target project path")
    parser.add_argument(
        "--smoke-command",
        default="uv run pytest -q",
        help="Smoke command to run in the target project",
    )
    parser.add_argument("--no-cli-smoke", action="store_true", help="Disable project-specific CLI smoke")
    parser.add_argument("--timeout", type=int, default=120, help="Command timeout in seconds")
    args = parser.parse_args(None if argv is None else list(argv))
    return RealProjectSmokeConfig(
        target_path=Path(args.path),
        smoke_command=tuple(shlex.split(args.smoke_command)),
        cli_smoke_enabled=not args.no_cli_smoke,
        command_timeout_seconds=args.timeout,
    )


def main(argv: tuple[str, ...] | list[str] | None = None) -> None:
    config = parse_args(argv)
    result = asyncio.run(run_real_project_smoke(config))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Real project smoke failed")
    print(result.value.output)


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
    commands = getattr(result, "commands", None)
    if commands is not None:
        failed = tuple(command for command in commands if command.exit_code != 0)
        status = "failed" if failed else "passed"
        lines = [f"CLI smoke {status}: {getattr(result, 'reason', '')}".strip()]
        findings = getattr(result, "findings", ())
        lines.extend(f"- {finding}" for finding in findings)
        return "\n".join(lines)
    exit_code = getattr(result, "exit_code", None)
    if exit_code is not None:
        status = "passed" if exit_code == 0 else "failed"
        output = _first_non_empty_line(getattr(result, "stdout", "")) or _first_non_empty_line(getattr(result, "stderr", ""))
        if output:
            return f"Command {status} with exit code {exit_code}. Output: {output}"
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


def _is_yakdb_project(project_info: ProjectInfo) -> bool:
    return project_info.name.lower() == "yakdb" or "yakdb_core" in project_info.files


def _cli_findings(commands: tuple[CommandResult, ...]) -> tuple[str, ...]:
    findings = []
    combined = "\n".join((*[command.stdout for command in commands], *[command.stderr for command in commands]))
    if ".yakdb/blobs" in combined:
        findings.append("CLI grep output included .yakdb/blobs internal storage.")
    if "loom-real-case" in combined:
        findings.append("CLI smoke marker was searchable and readable.")
    return tuple(findings)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def _project_info_value(project_info: ProjectInfo) -> dict[str, Any]:
    return {
        "path": project_info.path,
        "name": project_info.name,
        "purpose": project_info.purpose,
        "files": project_info.files,
        "git_status": project_info.git_status,
        "tech_stack": project_info.tech_stack,
    }


def _command_result_value(result: CommandResult) -> dict[str, Any]:
    return {
        "command": result.command,
        "cwd": result.cwd,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


def _cli_smoke_value(result: CliSmokeResult) -> dict[str, Any]:
    return {
        "skipped": result.skipped,
        "reason": result.reason,
        "commands": tuple(_command_result_value(command) for command in result.commands),
        "findings": result.findings,
    }


if __name__ == "__main__":
    main(sys.argv[1:])
