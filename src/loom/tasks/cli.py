"""Command line entry point for generic Loom tasks."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loom.core import Result
from loom.tasks.config import load_task_config
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task


@dataclass(frozen=True, slots=True)
class TaskCliOptions:
    request: TaskRequest
    options: TaskRunOptions
    config_path: Path | None = None
    model_name: str | None = None


def parse_task_cli_args(argv: tuple[str, ...] | list[str] | None = None) -> TaskCliOptions:
    args = _build_parser().parse_args(None if argv is None else list(argv))
    objective = " ".join(args.objective).strip()
    workspace = Path(args.workspace).expanduser()
    return TaskCliOptions(
        request=TaskRequest(
            objective=objective,
            workspace=workspace,
            profile=args.profile,
            constraints=tuple(args.constraints),
            expected_outputs=tuple(args.expected_outputs),
            risk_level=args.risk_level,
        ),
        options=TaskRunOptions(
            tui=args.tui,
            stream=args.stream or args.tui,
            trace_path=args.trace_path or _default_trace_path(),
            max_steps=args.max_steps,
            timeout_ms=args.timeout_ms,
        ),
        config_path=args.config,
        model_name=args.model,
    )


async def run_task_cli(options: TaskCliOptions) -> Result:
    config = None
    if options.config_path is not None:
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


def main(argv: tuple[str, ...] | list[str] | None = None) -> None:
    parsed = parse_task_cli_args(argv)
    result = asyncio.run(run_task_cli(parsed))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Task run failed")
    print(result.value.output)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a generic Loom LLM task.")
    parser.add_argument("objective", nargs="+", help="Task objective. Quote it when passing a multi-word objective.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace root for file and shell tools.")
    parser.add_argument("--profile", default="auto", help="Task profile, such as auto, general, or project_audit.")
    parser.add_argument("--risk-level", default="auto", help="Optional risk label stored in task context metadata.")
    parser.add_argument("--constraint", dest="constraints", action="append", default=[], help="Additional task constraint. Repeatable.")
    parser.add_argument("--expected-output", dest="expected_outputs", action="append", default=[], help="Expected final output. Repeatable.")
    parser.add_argument("--config", type=Path, help="Task model config YAML or TOML file.")
    parser.add_argument("--model", help="Named model alias from --config, or LOOM_LLM_MODEL override without --config.")
    parser.add_argument("--tui", action="store_true", help="Show live TUI events while the loop runs.")
    parser.add_argument("--stream", action="store_true", help="Stream LLM deltas when provider supports SSE.")
    parser.add_argument("--trace-path", type=Path, help="Persist full loop trace events and traces to a JSONL file. Defaults to runs/loom-task-*.jsonl.")
    parser.add_argument("--max-steps", type=int, help="Optional runtime loop step budget. Tool calls inside a step are not capped.")
    parser.add_argument("--timeout-ms", type=int, help="Per-step timeout in milliseconds.")
    return parser


def _default_trace_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path("runs") / f"loom-task-{timestamp}.jsonl"


__all__ = ["TaskCliOptions", "main", "parse_task_cli_args", "run_task_cli"]
