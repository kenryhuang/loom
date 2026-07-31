"""Command line entry point for generic Loom tasks."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loom.core import Result
from loom.runtime import PlanMode
from loom.tasks.config import RunDefaults, TaskDefaults, TaskRunnerConfig, load_task_config
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task


@dataclass(frozen=True, slots=True)
class TaskCliOptions:
    request: TaskRequest
    options: TaskRunOptions
    config_path: Path | None = None
    config: TaskRunnerConfig | None = None
    model_name: str | None = None


def parse_task_cli_args(argv: tuple[str, ...] | list[str] | None = None) -> TaskCliOptions:
    parser = _build_parser()
    args = parser.parse_args(None if argv is None else list(argv))
    config_path = args.config or _discover_default_config_path()
    config = _load_cli_config(config_path, parser)
    config_dir = config_path.expanduser().resolve().parent if config_path is not None else None
    task_defaults = config.task if config is not None else TaskDefaults()
    run_defaults = config.run if config is not None else RunDefaults()

    objective = " ".join(args.objective).strip() or task_defaults.objective or ""
    if not objective:
        parser.error("task objective is required; pass it on the command line or set task.objective in config.yaml/--config")

    workspace = _resolve_configurable_path(args.workspace if args.workspace is not None else task_defaults.workspace, config_dir) or Path.cwd()
    profile = args.profile if args.profile is not None else task_defaults.profile or "auto"
    expected_outputs = tuple(args.expected_outputs) if args.expected_outputs is not None else task_defaults.expected_outputs
    model_name = args.model if args.model is not None else (config.default_model if config is not None else None)
    tui = _coalesce(args.tui, run_defaults.tui, False)
    stream = _coalesce(args.stream, run_defaults.stream, False) or tui
    trace_path = _resolve_trace_path(
        cli_trace_path=args.trace_path,
        run_defaults=run_defaults,
        config_dir=config_dir,
        objective=objective,
        model_name=model_name,
        profile=profile,
        parser=parser,
    )
    return TaskCliOptions(
        request=TaskRequest(
            objective=objective,
            workspace=workspace,
            profile=profile,
            constraints=tuple(args.constraints or ()),
            expected_outputs=expected_outputs,
            risk_level=args.risk_level or "auto",
        ),
        options=TaskRunOptions(
            tui=tui,
            stream=stream,
            trace_path=trace_path,
            max_steps=_coalesce(args.max_steps, run_defaults.max_steps, None),
            timeout_ms=_coalesce(args.timeout_ms, run_defaults.timeout_ms, None),
            plan_mode=PlanMode(args.plan_mode or run_defaults.plan_mode or PlanMode.AUTO.value),
        ),
        config_path=config_path,
        config=config,
        model_name=model_name,
    )


async def run_task_cli(options: TaskCliOptions) -> Result:
    config = options.config
    if config is None and options.config_path is not None:
        loaded = load_task_config(options.config_path)
        if not loaded.ok:
            return loaded
        config = loaded.value
    return await run_generic_task(
        options.request,
        options=options.options,
        config=config,
        model_name=options.model_name,
    )


def main(argv: tuple[str, ...] | list[str] | None = None) -> int:
    parsed = parse_task_cli_args(argv)
    result = asyncio.run(run_task_cli(parsed))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Task run failed")
    print(result.value.output)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a generic Loom LLM task.")
    parser.add_argument("objective", nargs="*", help="Task objective. Quote it when passing a multi-word objective.")
    parser.add_argument("--workspace", type=Path, help="Workspace root for file and shell tools.")
    parser.add_argument("--profile", help="Task profile, such as auto, general, or project_audit.")
    parser.add_argument("--risk-level", help="Optional risk label stored in task context metadata.")
    parser.add_argument("--constraint", dest="constraints", action="append", help="Additional task constraint. Repeatable.")
    parser.add_argument("--expected-output", dest="expected_outputs", action="append", help="Expected final output. Repeatable.")
    parser.add_argument("--config", type=Path, help="Task config YAML or TOML file. Defaults to ./config.yaml or ./config.yml when present.")
    parser.add_argument("--model", help="Named model alias from --config, or LOOM_LLM_MODEL override without --config.")
    parser.add_argument("--tui", action=argparse.BooleanOptionalAction, default=None, help="Show live TUI events while the loop runs.")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None, help="Stream LLM deltas when provider supports SSE.")
    parser.add_argument("--trace-path", type=Path, help="Persist full loop trace events and traces to a JSONL file. Defaults to runs/loom-task-*.jsonl.")
    parser.add_argument("--max-steps", type=int, help="Optional runtime loop step budget. Tool calls inside a step are not capped.")
    parser.add_argument("--timeout-ms", type=int, help="Per-step timeout in milliseconds.")
    parser.add_argument(
        "--plan-mode",
        choices=tuple(mode.value for mode in PlanMode),
        help="Planning behavior: auto lets the model decide, force requires a plan, off uses the legacy ReAct loop.",
    )
    return parser


def _load_cli_config(config_path: Path | None, parser: argparse.ArgumentParser) -> TaskRunnerConfig | None:
    if config_path is None:
        return None
    loaded = load_task_config(config_path.expanduser())
    if not loaded.ok:
        parser.error(loaded.error.message if loaded.error is not None else "could not load task config")
    return loaded.value


def _discover_default_config_path() -> Path | None:
    for candidate in (Path("config.yaml"), Path("config.yml")):
        if candidate.exists():
            return candidate
    return None


def _coalesce(value, fallback, default):
    return value if value is not None else fallback if fallback is not None else default


def _resolve_configurable_path(value: str | Path | None, config_dir: Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if path.is_absolute() or config_dir is None:
        return path
    return config_dir / path


def _resolve_trace_path(
    *,
    cli_trace_path: Path | None,
    run_defaults: RunDefaults,
    config_dir: Path | None,
    objective: str,
    model_name: str | None,
    profile: str,
    parser: argparse.ArgumentParser,
) -> Path:
    if cli_trace_path is not None:
        return cli_trace_path.expanduser()
    if run_defaults.trace_path_template:
        try:
            rendered = _render_trace_path_template(run_defaults.trace_path_template, objective=objective, model_name=model_name, profile=profile)
        except ValueError as exc:
            parser.error(str(exc))
        resolved = _resolve_configurable_path(rendered, config_dir)
        return resolved if resolved is not None else _default_trace_path()
    if run_defaults.trace_path:
        resolved = _resolve_configurable_path(run_defaults.trace_path, config_dir)
        return resolved if resolved is not None else _default_trace_path()
    return _default_trace_path()


def _render_trace_path_template(template: str, *, objective: str, model_name: str | None, profile: str) -> str:
    values = {
        "timestamp": _trace_timestamp(),
        "task_slug": _slugify(objective),
        "model": model_name or "default",
        "profile": profile,
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise ValueError(f"Unknown trace_path_template variable: {exc.args[0]}") from exc
    except ValueError as exc:
        raise ValueError(f"Invalid trace_path_template: {exc}") from exc


def _slugify(value: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
            continue
        if not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-")[:64] or "task"


def _default_trace_path() -> Path:
    return Path("runs") / f"loom-task-{_trace_timestamp()}.jsonl"


def _trace_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


__all__ = ["TaskCliOptions", "main", "parse_task_cli_args", "run_task_cli"]
