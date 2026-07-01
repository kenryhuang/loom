"""Workspace tools for generic Loom task runs."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.core import Observation, Result, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.tasks.request import TaskRequest


def make_task_tools(request: TaskRequest) -> dict[str, Any]:
    root = (request.workspace or Path.cwd()).resolve()

    async def read_file(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        resolved = _resolve_workspace_path(root, data.get("path"))
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
                    "path": _relative_to_root(root, path),
                    "content": content,
                    "bytes_read": min(len(raw), max_bytes),
                    "truncated": len(raw) > max_bytes,
                },
                now_iso(),
            )
        )

    async def write_file(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        resolved = _resolve_workspace_path(root, data.get("path"))
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
                {"path": _relative_to_root(root, path), "bytes_written": len(content.encode("utf-8"))},
                now_iso(),
            )
        )

    async def shell_execute(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        command = _command_from_input(data.get("command"))
        if not command.ok:
            return command
        cwd_result = _resolve_workspace_path(root, data.get("cwd") or ".")
        if not cwd_result.ok:
            return cwd_result
        timeout_seconds = _positive_int(data.get("timeout_seconds"), 120)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command.value,
                cwd=cwd_result.value,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            value = {
                "command": command.value,
                "cwd": _relative_to_root(root, cwd_result.value),
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            value = {
                "command": command.value,
                "cwd": _relative_to_root(root, cwd_result.value),
                "exit_code": 124,
                "stdout": _text(exc.stdout),
                "stderr": _text(exc.stderr) or f"Command timed out after {timeout_seconds}s",
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "timed_out": True,
            }
        except OSError as exc:
            value = {
                "command": command.value,
                "cwd": _relative_to_root(root, cwd_result.value),
                "exit_code": 127,
                "stdout": "",
                "stderr": str(exc),
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "timed_out": False,
            }
        return ok(Observation(new_trace_id(), "shell_execute", value, now_iso()))

    async def finish(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        report = str(data.get("report") or data.get("content") or "")
        return ok(Observation(new_trace_id(), "finish", {"report": report, "completed": bool(report.strip())}, now_iso()))

    return {
        "read_file": read_file,
        "write_file": write_file,
        "shell_execute": shell_execute,
        "finish": finish,
    }


def _tool_input(input_value: Any) -> dict[str, Any]:
    value = thaw_json(input_value)
    return dict(value) if isinstance(value, Mapping) else {}


def _resolve_workspace_path(root: Path, value: Any) -> Result:
    if value is None or str(value).strip() == "":
        return err(make_loom_error("VALIDATION_FAILED", "Tool path is required", retryable=False))
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Tool path must stay inside workspace",
                retryable=False,
                metadata={"workspace": str(root), "path": str(resolved)},
            )
        )
    return ok(resolved)


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _command_from_input(value: Any) -> Result:
    raw = thaw_json(value)
    if isinstance(raw, str):
        command = tuple(shlex.split(raw))
    elif isinstance(raw, (list, tuple)):
        command = tuple(str(part) for part in raw)
    else:
        command = ()
    if not command:
        return err(make_loom_error("VALIDATION_FAILED", "Tool command is required", retryable=False))
    return ok(command)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = ["make_task_tools"]
