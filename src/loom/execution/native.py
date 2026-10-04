"""Native execution with explicit entrypoints, operation identity and cancellation."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import asdict

from loom.core import err, make_loom_error, ok
from loom.runtime.checkpoints import decode, encode
from loom.runtime.execution_contracts import ExecutionResult
from loom.runtime.plugin_contracts import PluginManifest, config_digest, json_value


class NativeToolExecutionRuntime:
    manifest = PluginManifest("native_os", "execution_runtime", capabilities=("python_tools", "processes"))

    def __init__(self, config, *, entrypoints, execution=None, cancellable=()):
        if set(config) - {"workspace_resource"}:
            raise ValueError("Unknown native runtime configuration")
        self.config = json_value(config)
        self.entrypoints = dict(entrypoints)
        self.execution = execution
        self.cancellable = set(cancellable)
        self.runtime_id = uuid.uuid4().hex
        self.operations = {}
        self.tasks = {}

    def prepare(self, resources):
        workspace = self.config.get("workspace_resource")
        if workspace and workspace not in {r.id for r in resources}:
            raise ValueError("Native runtime workspace resource is not bound")
        return self.runtime_id

    async def execute(self, invocation, options=None):
        oid = invocation.operation_id
        fingerprint = config_digest({k: v for k, v in asdict(invocation).items() if k not in {"attempt_id", "runtime_id", "deadline"}})
        existing = self.operations.get(oid)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("Operation ID reused with different invocation")
            if "result" in existing:
                stored = decode(existing["result"])
                result = ok(stored["value"]) if stored["ok"] else err(stored["error"])
                return ExecutionResult(existing["execution_id"], existing["status"], result, existing.get("error_kind"))
            if oid in self.tasks:
                return await asyncio.shield(self.tasks[oid])
            return ExecutionResult(
                existing["execution_id"], "unknown", err(make_loom_error("EXECUTION_UNKNOWN", "Execution requires reconciliation", retryable=False))
            )
        handler = self.entrypoints.get(invocation.entrypoint_id)
        if handler is None:
            raise ValueError(f"Tool entrypoint is not deployed: {invocation.entrypoint_id}")
        record = {"fingerprint": fingerprint, "execution_id": uuid.uuid4().hex, "status": "running"}
        self.operations[oid] = record

        async def invoke():
            opts = dict(options or {})
            if self.execution is not None:
                opts.update(
                    process_started=lambda pid: self.execution.rpc("process_started", {"pid": pid}),
                    cancel_check=lambda: self.execution.rpc("check_control", {}),
                )
            try:

                async def run_handler():
                    value = handler(invocation.input, opts)
                    value = await value if inspect.isawaitable(value) else value
                    return value if hasattr(value, "ok") else ok(value)

                if invocation.deadline is not None:
                    async with asyncio.timeout(max(0, invocation.deadline - time.time())):
                        result = await run_handler()
                else:
                    result = await run_handler()
                status, error_kind = ("succeeded", None) if result.ok else ("failed", "tool_error")
            except TimeoutError:
                confirmed = invocation.entrypoint_id in self.cancellable
                code = "TIMEOUT" if confirmed else "EXECUTION_UNKNOWN"
                result, status, error_kind = (
                    err(make_loom_error(code, "Tool execution deadline exceeded", retryable=False)),
                    "failed" if confirmed else "unknown",
                    "timeout",
                )
            except asyncio.CancelledError:
                record["status"] = "cancelled" if invocation.entrypoint_id in self.cancellable else "unknown"
                raise
            except Exception as exc:
                result, status, error_kind = err(make_loom_error("TOOL_FAILED", str(exc), retryable=False)), "failed", "tool_error"
            record.update(status=status, result=encode({"ok": result.ok, "value": result.value, "error": result.error}), error_kind=error_kind)
            return ExecutionResult(record["execution_id"], status, result, error_kind)

        task = self.tasks[oid] = asyncio.create_task(invoke())
        try:
            return await task
        except asyncio.CancelledError:
            if record["status"] == "unknown":
                return ExecutionResult(
                    record["execution_id"], "unknown", err(make_loom_error("EXECUTION_UNKNOWN", "Tool termination is unconfirmed", retryable=False))
                )
            raise
        finally:
            self.tasks.pop(oid, None)

    def inspect(self, operation_id):
        # Missing records cannot prove an execution never started in another process.
        return self.operations.get(operation_id, {}).get("status", "unknown")

    async def cancel(self, operation_id):
        task = self.tasks.get(operation_id)
        if task is None:
            return self.inspect(operation_id) in {"succeeded", "failed", "cancelled"}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self.inspect(operation_id) == "cancelled"

    def snapshot(self):
        return self.manifest.state(self.config, {"runtime_id": self.runtime_id, "operations": self.operations})

    def restore(self, state):
        value = self.manifest.restore(self.config, state)
        self.runtime_id = value["runtime_id"]
        self.operations = value["operations"]
        for operation in self.operations.values():
            if operation["status"] in {"running", "cancelled"} and "result" not in operation:
                operation["status"] = "unknown"

    def reconnect(self):
        return True

    async def close(self):
        for oid in list(self.tasks):
            await self.cancel(oid)
