"""Top-level ``loom optimize`` command and supporting operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok, thaw_json
from loom.optimize.store import SQLiteOptimizationStore


@dataclass(frozen=True, slots=True)
class OptimizeCliOptions:
    command: str
    trace: Path | None = None
    tasks: Path | None = None
    discovery_tasks: Path | None = None
    validation_tasks: Path | None = None
    holdout_tasks: Path | None = None
    config: Path | None = None
    tui: bool = False
    json: bool = False
    dry_run: bool = False
    new_run: bool = False
    output_dir: Path = Path(".loom/optimize")
    optimization_id: str | None = None
    candidate_id: str | None = None
    identity: str | None = None
    reason: str | None = None


def parse_args(argv: Sequence[str] | None = None) -> OptimizeCliOptions:
    values = list(sys.argv[1:] if argv is None else argv)
    command = values[0] if values and values[0] in {"status", "approve", "pause"} else "run"
    if command == "run":
        parser = _run_parser()
        args = parser.parse_args(values)
        automatic = args.tasks is not None
        explicit = any(value is not None for value in (args.discovery_tasks, args.validation_tasks, args.holdout_tasks))
        if automatic == explicit:
            parser.error("provide either --tasks or all three explicit task-set options")
        if explicit and not all(value is not None for value in (args.discovery_tasks, args.validation_tasks, args.holdout_tasks)):
            parser.error("explicit task sets require --discovery-tasks, --validation-tasks, and --holdout-tasks")
        return OptimizeCliOptions(
            "run",
            trace=args.trace,
            tasks=args.tasks,
            discovery_tasks=args.discovery_tasks,
            validation_tasks=args.validation_tasks,
            holdout_tasks=args.holdout_tasks,
            config=args.config,
            tui=args.tui,
            json=args.json,
            dry_run=args.dry_run,
            new_run=args.new_run,
            output_dir=args.output_dir,
        )
    parser = _support_parser(command)
    args = parser.parse_args(values[1:])
    return OptimizeCliOptions(
        command,
        json=getattr(args, "json", False),
        output_dir=args.output_dir,
        optimization_id=args.optimization_id,
        candidate_id=getattr(args, "candidate_id", None),
        identity=getattr(args, "identity", None),
        reason=getattr(args, "reason", None),
    )


async def run_optimize(options: OptimizeCliOptions) -> Result:
    built = build_orchestrator(options)
    if inspect_is_awaitable(built):
        built = await built
    if not isinstance(built, Result):
        return _cli_error("OPTIMIZATION_COMPOSITION_INVALID", "Optimize orchestrator builder returned an invalid result")
    if not built.ok:
        return built
    if options.dry_run:
        dry_run = getattr(built.value, "dry_run", None)
        if callable(dry_run):
            value = dry_run()
            return await value if inspect_is_awaitable(value) else value
        return ok({"disposition": "dry_run", "optimization_id": getattr(built.value, "spec", None).optimization_id})
    return await built.value.run()


def build_orchestrator(options: OptimizeCliOptions) -> Result:
    del options
    return _cli_error(
        "OPTIMIZATION_COMPOSITION_UNAVAILABLE",
        "Optimize runtime composition is unavailable",
    )


async def run_status(options: OptimizeCliOptions) -> Result:
    store = _open_store(options)
    if not store.ok:
        return store
    return await store.value.load(options.optimization_id or "")


async def run_pause(options: OptimizeCliOptions) -> Result:
    store = _open_store(options)
    if not store.ok:
        return store
    return await store.value.pause(options.optimization_id or "", options.reason or "user requested pause")


async def run_approve(options: OptimizeCliOptions) -> Result:
    root = _optimization_root(options)
    request = root / "approval-request.json"
    if not request.is_file():
        return _cli_error(
            "APPROVAL_CONTEXT_MISSING",
            "Approval context is unavailable; resume the original optimize command after the request is recorded",
        )
    return _cli_error("APPROVAL_SERVICE_UNAVAILABLE", "Approval service is not configured for this optimization")


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    if options.command == "run":
        result = asyncio.run(run_optimize(options))
    elif options.command == "status":
        result = asyncio.run(run_status(options))
    elif options.command == "approve":
        result = asyncio.run(run_approve(options))
    else:
        result = asyncio.run(run_pause(options))
    if not result.ok:
        _print_error(result, json_output=options.json)
        return 1
    value = result.value
    _print_value(value, json_output=options.json)
    if options.command == "pause":
        return 3
    disposition = _field(value, "disposition")
    return 2 if disposition == "awaiting_approval" else 0


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom optimize", description="Run a governed Meta-Harness optimization from one command.")
    parser.add_argument("--trace", required=True, type=Path, help="Seed Loom task trace JSONL.")
    parser.add_argument("--tasks", type=Path, help="Task JSONL to split deterministically.")
    parser.add_argument("--discovery-tasks", type=Path)
    parser.add_argument("--validation-tasks", type=Path)
    parser.add_argument("--holdout-tasks", type=Path)
    parser.add_argument("--config", required=True, type=Path, help="Loom config containing meta_harness.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--tui", action="store_true", help="Observe committed stages in a TUI.")
    output.add_argument("--json", action="store_true", help="Emit JSON events and final result.")
    parser.add_argument("--dry-run", action="store_true", help="Validate all inputs without model or campaign calls.")
    parser.add_argument("--new-run", action="store_true", help="Create a new optimization identity instead of resuming identical inputs.")
    parser.add_argument("--output-dir", type=Path, default=Path(".loom/optimize"))
    return parser


def _support_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"loom optimize {command}")
    parser.add_argument("optimization_id")
    parser.add_argument("--output-dir", type=Path, default=Path(".loom/optimize"))
    if command == "status":
        parser.add_argument("--json", action="store_true")
    elif command == "approve":
        parser.add_argument("--candidate", dest="candidate_id", required=True)
        parser.add_argument("--identity", required=True)
        parser.add_argument("--reason")
    else:
        parser.add_argument("--reason")
    return parser


def _optimization_root(options: OptimizeCliOptions) -> Path:
    direct = options.output_dir
    if (direct / "optimization.sqlite").is_file():
        return direct
    return direct / (options.optimization_id or "")


def _open_store(options: OptimizeCliOptions) -> Result:
    root = _optimization_root(options)
    if not (root / "optimization.sqlite").is_file():
        return _cli_error("OPTIMIZATION_NOT_FOUND", "Optimization store was not found", root=str(root))
    return ok(SQLiteOptimizationStore(root))


def _print_value(value: Any, *, json_output: bool) -> None:
    plain = _plain(value)
    if json_output:
        print(json.dumps(plain, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    if isinstance(plain, Mapping):
        for key in ("optimization_id", "disposition", "report_path", "next_action"):
            if plain.get(key) is not None:
                print(f"{key}: {plain[key]}")
    else:
        print(plain)


def _print_error(result: Result, *, json_output: bool) -> None:
    error = result.error
    payload = {
        "ok": False,
        "error": {
            "code": "INTERNAL" if error is None else error.code,
            "message": "Optimize command failed" if error is None else error.message,
            "retryable": False if error is None else error.retryable,
            "metadata": {} if error is None else _plain(error.metadata),
        },
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(f"{payload['error']['code']}: {payload['error']['message']}", file=sys.stderr)


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in thaw_json(value).items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def inspect_is_awaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


def _cli_error(code: str, message: str, **metadata: Any) -> Result:
    return err(make_loom_error(code, message, retryable=False, metadata=metadata))


__all__ = [
    "OptimizeCliOptions",
    "build_orchestrator",
    "main",
    "parse_args",
    "run_approve",
    "run_optimize",
    "run_pause",
    "run_status",
]
