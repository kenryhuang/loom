"""Workspace tools for generic Loom task runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.core import Observation, Result, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.tasks.edit_file import apply_file_edits, parse_text_edits
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
            raw = await asyncio.to_thread(path.read_bytes)
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

    async def edit_file(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        unknown = set(data) - {"path", "edits"}
        if unknown:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "edit_file input contains unknown fields",
                    retryable=False,
                    metadata={"fields": sorted(unknown)},
                )
            )
        resolved = _resolve_workspace_path(root, data.get("path"))
        if not resolved.ok:
            return resolved
        display_path = _relative_to_root(root, resolved.value)
        edits = parse_text_edits(data.get("edits"), path=display_path)
        if not edits.ok:
            return edits
        edited = await asyncio.to_thread(apply_file_edits, resolved.value, edits.value, display_path=display_path)
        if not edited.ok:
            return edited
        value = edited.value
        return ok(
            Observation(
                new_trace_id(),
                "edit_file",
                {
                    "path": value.path,
                    "replacements": value.replacements,
                    "bytes_written": value.bytes_written,
                    "first_changed_line": value.first_changed_line,
                    "diff": value.diff,
                    "diff_truncated": value.diff_truncated,
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
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
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
        script = data.get("command")
        if not isinstance(script, str) or not script.strip():
            return _invalid_process_input(
                'shell_execute requires command as a shell script string, e.g. {"command":"git fetch && git status"}. '
                "Use process_execute with argv for argument arrays."
            )
        try:
            encoded = json.loads(script)
        except ValueError:
            encoded = None
        if isinstance(encoded, list):
            return _invalid_process_input('Do not JSON-encode argv into command. Use process_execute: {"argv":["git","status"]}.')
        shell = tuple((_options or {}).get("shell_argv", ("/bin/bash", "-o", "pipefail", "-c")))
        return await execute_process("shell_execute", data, (*shell, script), _options)

    async def process_execute(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv) or not argv[0].strip():
            return _invalid_process_input(
                'process_execute requires a non-empty string array argv, e.g. {"argv":["git","status"]}. '
                "Use shell_execute for pipes, &&, redirects or expansion."
            )
        return await execute_process("process_execute", data, tuple(argv), _options)

    async def execute_process(source, data, command, _options):
        cwd_result = _resolve_workspace_path(root, data.get("cwd") or ".")
        if not cwd_result.ok:
            return cwd_result
        timeout_seconds = _positive_int(data.get("timeout_seconds"), 120)
        started = time.monotonic()
        options = _options or {}
        max_output = _positive_int(options.get("max_output_bytes"), 20000)
        process = None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        timed_out = False
        readers = []
        gate_read = gate_write = None

        async def read_pipe(pipe, name):
            nonlocal truncated
            while chunk := await pipe.read(8192):
                remaining = max_output - len(buffers[name])
                buffers[name].extend(chunk[:remaining])
                truncated = truncated or len(chunk) > remaining

        async def terminate():
            if process is None:
                return
            # Reap the whole session even if the parent exited with live descendants.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 1)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

        async def wait_or_stop():
            check = options.get("cancel_check")
            while process.returncode is None:
                if check is not None and check():
                    await terminate()
                    return
                await asyncio.sleep(0.05)
            await process.wait()

        try:
            callback = options.get("process_started")
            argv = command
            extra = {}
            if callback is not None:
                gate_read, gate_write = os.pipe()
                launcher = "import os,sys; fd=int(sys.argv[1]); token=os.read(fd,1); os.close(fd); "
                launcher += (
                    "\nif token!=b'1': sys.exit(125)\ntry: os.execvp(sys.argv[2],sys.argv[2:])"
                    "\nexcept OSError as e: print('LOOM_PROCESS_LAUNCH_ERROR: '+str(e),file=sys.stderr); sys.exit(127)"
                )
                argv = (sys.executable, "-c", launcher, str(gate_read), *argv)
                extra["pass_fds"] = (gate_read,)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd_result.value,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                **extra,
            )
            if gate_read is not None:
                os.close(gate_read)
                gate_read = None
            if callback is not None:
                callback(process.pid)
                os.write(gate_write, b"1")
                os.close(gate_write)
                gate_write = None
            readers = [asyncio.create_task(read_pipe(process.stdout, "stdout")), asyncio.create_task(read_pipe(process.stderr, "stderr"))]
            try:
                await asyncio.wait_for(wait_or_stop(), timeout_seconds)
                # Descendants holding a pipe cannot keep a finished command open forever.
                await asyncio.wait_for(asyncio.gather(*readers), 1)
            except TimeoutError:
                timed_out = True
                await terminate()
            value = {
                "argv": command,
                **({"command": data["command"]} if source == "shell_execute" else {}),
                "cwd": _relative_to_root(root, cwd_result.value),
                "exit_code": 124 if timed_out else process.returncode,
                "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
                "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "timed_out": timed_out,
                "output_truncated": truncated,
            }
        except asyncio.CancelledError:
            await terminate()
            raise
        except OSError as exc:
            value = {
                "argv": command,
                **({"command": data["command"]} if source == "shell_execute" else {}),
                "cwd": _relative_to_root(root, cwd_result.value),
                "exit_code": 127,
                "stdout": "",
                "stderr": str(exc),
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "timed_out": False,
            }
        finally:
            for gate in (gate_read, gate_write):
                if gate is not None:
                    os.close(gate)
            if process is not None:
                # A successful parent can also leave detached descendants in its group.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
        launch_failed = value["exit_code"] == 127 and (process is None or value["stderr"].startswith("LOOM_PROCESS_LAUNCH_ERROR: "))
        no_match = source == "process_execute" and Path(command[0]).name in {"grep", "rg"} and value["exit_code"] == 1 and not timed_out
        status = (
            "launch_failed"
            if launch_failed
            else "timed_out"
            if timed_out
            else "no_match"
            if no_match
            else "succeeded"
            if value["exit_code"] == 0
            else "nonzero_exit"
        )
        value.update(ok=status in {"succeeded", "no_match"}, status=status)
        if no_match:
            value["matched"] = False
        return ok(Observation(new_trace_id(), source, value, now_iso()))

    async def finish(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
        data = _tool_input(input_value)
        report = str(data.get("report") or data.get("content") or "")
        return ok(
            Observation(
                new_trace_id(),
                "finish",
                {"report": report, "completed": bool(report.strip())},
                now_iso(),
                metadata={"controlFlow": {"stepBoundary": bool(report.strip()), "reason": "task_completed"}},
            )
        )

    return {
        "read_file": read_file,
        "edit_file": edit_file,
        "write_file": write_file,
        "shell_execute": shell_execute,
        "process_execute": process_execute,
        "finish": finish,
    }


def _tool_input(input_value: Any) -> dict[str, Any]:
    value = thaw_json(input_value)
    return dict(value) if isinstance(value, Mapping) else {}


def _resolve_workspace_path(root: Path, value: Any) -> Result:
    if not isinstance(value, str) or not value.strip():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Tool path must be a non-empty string",
                retryable=False,
            )
        )
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                f"Tool path is invalid: {exc}",
                retryable=False,
            )
        )
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


def _invalid_process_input(message: str) -> Result:
    return err(make_loom_error("VALIDATION_FAILED", message, retryable=False, metadata={"failureDomain": "tool", "error_kind": "invalid_input"}))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = ["make_task_tools"]
