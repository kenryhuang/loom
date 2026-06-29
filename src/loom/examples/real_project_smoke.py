"""Real project smoke audit example for Loom."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
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
    thaw_json,
)
from loom.llm.api import create_env_openai_provider, create_llm_step_function
from loom.observability.traces import JsonlTraceStore
from loom.runtime.engine import create, create_runtime_registry, run
from loom.runtime.plugins import run_with_plugins
from loom.tui.plugin import TuiPlugin

DEFAULT_YAKDB_PATH = "/Users/huanggui/workspace/yakDB"


@dataclass(frozen=True, slots=True)
class RealProjectSmokeConfig:
    target_path: Path
    smoke_command: tuple[str, ...] = ("uv", "run", "--no-sync", "pytest", "-q")
    cli_smoke_enabled: bool = True
    command_timeout_seconds: int = 120
    trace_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_path", Path(self.target_path))
        object.__setattr__(self, "smoke_command", tuple(self.smoke_command))
        if self.trace_path is not None:
            object.__setattr__(self, "trace_path", Path(self.trace_path))


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


@dataclass(frozen=True, slots=True)
class RealProjectSmokeRunOptions:
    config: RealProjectSmokeConfig
    llm: bool = False
    tui: bool = False
    stream: bool = False


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
            ("uv", "run", "--no-sync", "yakdb", "init", str(workspace), "--no-ocr"),
            ("uv", "run", "--no-sync", "yakdb", "index", str(workspace), "--workspace", str(workspace)),
            ("uv", "run", "--no-sync", "yakdb", "grep", "loom-real-case", str(workspace), "--workspace", str(workspace)),
            ("uv", "run", "--no-sync", "yakdb", "read", "docs/notes.txt", "--workspace", str(workspace), "--numbered"),
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


def make_real_project_smoke_tools(config: RealProjectSmokeConfig) -> dict[str, Any]:
    async def read_file_tool(input_value, _options=None):
        data = _tool_input(input_value)
        resolved = _resolve_project_path(config.target_path, data.get("path"))
        if not resolved.ok:
            return resolved
        max_bytes = _positive_int(data.get("max_bytes"), 20000)
        path = resolved.value
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return err(make_loom_error("TOOL_FAILED", f"Failed to read file: {exc}", retryable=False, metadata={"path": str(path)}))
        content = raw[:max_bytes].decode("utf-8", errors="replace")
        return ok(
            Observation(
                new_trace_id(),
                "read_file",
                {
                    "path": _relative_to_project(config.target_path, path),
                    "content": content,
                    "bytes_read": min(len(raw), max_bytes),
                    "truncated": len(raw) > max_bytes,
                },
                now_iso(),
            )
        )

    async def write_file_tool(input_value, _options=None):
        data = _tool_input(input_value)
        resolved = _resolve_project_path(config.target_path, data.get("path"))
        if not resolved.ok:
            return resolved
        content = str(data.get("content", ""))
        path = resolved.value
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return err(make_loom_error("TOOL_FAILED", f"Failed to write file: {exc}", retryable=False, metadata={"path": str(path)}))
        return ok(
            Observation(
                new_trace_id(),
                "write_file",
                {
                    "path": _relative_to_project(config.target_path, path),
                    "bytes_written": len(content.encode("utf-8")),
                },
                now_iso(),
            )
        )

    async def shell_execute_tool(input_value, _options=None):
        data = _tool_input(input_value)
        command = _command_from_tool_input(data.get("command"), config.smoke_command)
        cwd = _resolve_project_path(config.target_path, data.get("cwd") or ".")
        if not cwd.ok:
            return cwd
        timeout_seconds = _positive_int(data.get("timeout_seconds"), config.command_timeout_seconds)
        result = run_command(command, cwd=cwd.value, timeout_seconds=timeout_seconds)
        return ok(Observation(new_trace_id(), "shell_execute", _command_result_value(result), now_iso()))

    async def finish_tool(input_value, _options=None):
        data = _tool_input(input_value)
        report = str(data.get("report") or data.get("content") or "")
        return ok(
            Observation(
                new_trace_id(),
                "finish",
                {
                    "report": report,
                    "completed": bool(report.strip()),
                },
                now_iso(),
            )
        )

    return {
        "read_file": read_file_tool,
        "write_file": write_file_tool,
        "shell_execute": shell_execute_tool,
        "finish": finish_tool,
    }


def make_real_project_smoke_llm_context(config: RealProjectSmokeConfig):
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
                identity=IdentityLayer(
                    role="real project smoke auditor",
                    constraints=(
                        {
                            "id": "inspect-files",
                            "description": (
                                "Use read_file to inspect project files such as README.md, pyproject.toml, package manifests, and focused source or test files."
                            ),
                            "severity": "must",
                        },
                        {
                            "id": "run-smoke-command",
                            "description": f"Use shell_execute to run the configured smoke command: {shlex.join(config.smoke_command)}.",
                            "severity": "must",
                        },
                        {
                            "id": "bounded-side-effects",
                            "description": "Do not modify project source files. Use write_file only for optional audit artifacts under .loom/.",
                            "severity": "must",
                        },
                        {
                            "id": "focused-audit",
                            "description": "Do not enumerate the whole repository. Prefer focused reads and commands that answer the audit question.",
                            "severity": "must",
                        },
                        {
                            "id": "finish-report",
                            "description": (
                                "Call finish exactly once with the final markdown audit report after gathering evidence. "
                                "After finish returns, return final JSON whose action.input.report contains the same report."
                            ),
                            "severity": "must",
                        },
                        {
                            "id": "llm-judgment-only",
                            "description": (
                                "Make purpose, risk, and improvement judgments yourself from observed evidence; "
                                "do not invent command results or repository facts."
                            ),
                            "severity": "must",
                        },
                    ),
                ),
                goal=GoalLayer(
                    objective=(
                        f"Audit the real project at {config.target_path}. Use the basic tools like a coding agent: read relevant files, run shell commands "
                        f"from the project root, run the configured smoke command `{shlex.join(config.smoke_command)}`, "
                        "inspect failures or warnings only when needed, avoid full repository inventory, "
                        "then call finish with a markdown report covering purpose, smoke result, risks, and improvement directions."
                    ),
                    budget={"max_steps": 1},
                ),
                state=StateLayer(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(
                    tools=(
                        ToolRef(
                            "read_file",
                            "Read a UTF-8 text file under the target project directory",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Project-relative path to read"},
                                    "max_bytes": {"type": "integer", "description": "Maximum bytes to return; defaults to 20000"},
                                },
                                "required": ["path"],
                                "additionalProperties": False,
                            },
                        ),
                        ToolRef(
                            "write_file",
                            "Write a UTF-8 text file under the target project directory; use only for optional .loom audit artifacts",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Project-relative path to write"},
                                    "content": {"type": "string", "description": "File content"},
                                },
                                "required": ["path", "content"],
                                "additionalProperties": False,
                            },
                        ),
                        ToolRef(
                            "shell_execute",
                            "Execute a shell command inside the target project directory and return stdout, stderr, exit code, and timing",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {"type": "array", "items": {"type": "string"}},
                                        ],
                                        "description": "Command to run. String commands are split with shell-like syntax.",
                                    },
                                    "cwd": {"type": "string", "description": "Optional project-relative working directory"},
                                    "timeout_seconds": {"type": "integer", "description": "Optional timeout; defaults to demo timeout"},
                                },
                                "required": ["command"],
                                "additionalProperties": False,
                            },
                        ),
                        ToolRef(
                            "finish",
                            "Finish the audit with the final markdown report",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "report": {"type": "string", "description": "Final markdown audit report"},
                                },
                                "required": ["report"],
                                "additionalProperties": False,
                            },
                        ),
                    )
                ),
            )
        )
    )


def make_real_project_smoke_llm_loop(config: RealProjectSmokeConfig, provider: Any, *, stream: bool = False) -> MinimalLoopDefinition:
    return MinimalLoopDefinition(
        id=new_loop_id(),
        version=new_loop_version(),
        identity=IdentityLayer(role="real project smoke auditor"),
        goal=GoalLayer(objective=f"LLM audit real project smoke path for {config.target_path}"),
        step=create_llm_step_function(provider, stream=stream, max_tool_calls_per_step=None),
        done=lambda context, _runtime: ok(bool(context.state.decisions)),
    )


async def run_real_project_smoke(
    config: RealProjectSmokeConfig,
    *,
    provider: Any | None = None,
    llm: bool = False,
    tui: bool = False,
    stream: bool = False,
):
    if llm:
        if provider is None:
            provider_result = create_env_openai_provider()
            if not provider_result.ok:
                return provider_result
            provider = provider_result.value
        context = make_real_project_smoke_llm_context(config)
        if not context.ok:
            return context
        handle = create(
            make_real_project_smoke_llm_loop(config, provider, stream=stream),
            registry=create_runtime_registry(tools=make_real_project_smoke_tools(config)),
            trace_store=_make_trace_store(config),
        )
        if not handle.ok:
            return handle
        if tui:
            result = await run_with_plugins(handle.value, context.value, max_steps=1, plugins=(TuiPlugin(),))
        else:
            result = await run(handle.value, context.value, max_steps=1)
        if result.ok:
            return ok(replace(result.value, output=_report_from_run_result(result.value)))
        return result

    context = make_real_project_smoke_context(config)
    if not context.ok:
        return context
    handle = create(
        make_real_project_smoke_loop(config),
        registry=create_runtime_registry(),
        trace_store=_make_trace_store(config),
    )
    if not handle.ok:
        return handle
    return await run(handle.value, context.value, max_steps=1)


def _make_trace_store(config: RealProjectSmokeConfig) -> JsonlTraceStore | None:
    if config.trace_path is None:
        return None
    return JsonlTraceStore(config.trace_path)


def _report_from_run_result(run_result) -> str:
    output = thaw_json(run_result.output)
    if isinstance(output, dict):
        action = output.get("action", {})
        if isinstance(action, dict):
            input_value = action.get("input", {})
            if isinstance(input_value, dict) and isinstance(input_value.get("report"), str):
                return input_value["report"]
    latest = run_result.context.state.decisions[-1] if run_result.context.state.decisions else None
    if latest is not None:
        action_input = thaw_json(latest.action.input)
        if isinstance(action_input, dict) and isinstance(action_input.get("report"), str):
            return action_input["report"]
    for observation in reversed(run_result.context.state.observations):
        if observation.source != "finish":
            continue
        value = thaw_json(observation.value)
        if isinstance(value, dict) and isinstance(value.get("report"), str):
            return value["report"]
    return "" if output is None else str(output)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Loom real project smoke audit.")
    parser.add_argument("path", nargs="?", default=DEFAULT_YAKDB_PATH, help="Target project path")
    parser.add_argument(
        "--smoke-command",
        default="uv run --no-sync pytest -q",
        help="Smoke command to run in the target project",
    )
    parser.add_argument("--no-cli-smoke", action="store_true", help="Disable project-specific CLI smoke")
    parser.add_argument("--timeout", type=int, default=120, help="Command timeout in seconds")
    parser.add_argument("--llm", action="store_true", help="Use an LLM to judge evidence and write the report")
    parser.add_argument("--tui", action="store_true", help="Show live TUI events while the loop runs")
    parser.add_argument("--stream", action="store_true", help="Stream LLM deltas when provider supports it")
    parser.add_argument("--trace-path", type=Path, help="Persist full loop trace events and traces to a JSONL file")
    return parser


def parse_args(argv: tuple[str, ...] | list[str] | None = None) -> RealProjectSmokeConfig:
    args = _build_parser().parse_args(None if argv is None else list(argv))
    return RealProjectSmokeConfig(
        target_path=Path(args.path),
        smoke_command=tuple(shlex.split(args.smoke_command)),
        cli_smoke_enabled=not args.no_cli_smoke,
        command_timeout_seconds=args.timeout,
        trace_path=args.trace_path,
    )


def parse_run_options(argv: tuple[str, ...] | list[str] | None = None) -> RealProjectSmokeRunOptions:
    args = _build_parser().parse_args(None if argv is None else list(argv))
    config = RealProjectSmokeConfig(
        target_path=Path(args.path),
        smoke_command=tuple(shlex.split(args.smoke_command)),
        cli_smoke_enabled=not args.no_cli_smoke,
        command_timeout_seconds=args.timeout,
        trace_path=args.trace_path,
    )
    return RealProjectSmokeRunOptions(
        config=config,
        llm=args.llm,
        tui=args.tui,
        stream=args.stream,
    )


def main(argv: tuple[str, ...] | list[str] | None = None) -> None:
    options = parse_run_options(argv)
    result = asyncio.run(
        run_real_project_smoke(
            options.config,
            llm=options.llm,
            tui=options.tui,
            stream=options.stream or options.tui,
        )
    )
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


def _tool_input(input_value: Any) -> dict[str, Any]:
    value = thaw_json(input_value)
    return value if isinstance(value, dict) else {}


def _resolve_project_path(project_root: Path, raw_path: Any):
    if not isinstance(raw_path, str) or not raw_path.strip():
        return err(make_loom_error("VALIDATION_FAILED", "Tool path is required", retryable=False))
    root = project_root.resolve()
    candidate = Path(raw_path)
    path = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Tool path must stay inside the target project",
                retryable=False,
                metadata={"path": raw_path, "target_path": str(project_root)},
            )
        )
    return ok(resolved)


def _relative_to_project(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except (OSError, ValueError):
        return str(path)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _command_from_tool_input(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    value = thaw_json(value)
    if isinstance(value, str) and value.strip():
        return tuple(shlex.split(value))
    if isinstance(value, tuple | list) and value:
        return tuple(str(item) for item in value)
    return default


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
