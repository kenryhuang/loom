"""Spawn-only worker transport and process-group cleanup."""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import subprocess
import time
from dataclasses import dataclass

from loom.runtime.checkpoints import decode, encode


def process_identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def reap_group(pid, identity):
    """Confirm a recorded group has no live members, including orphaned children."""
    if not pid:
        return True

    def members():
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,uid=,stat="], capture_output=True, text=True, check=False)
        if result.returncode:
            return None
        return [fields for line in result.stdout.splitlines() if len(fields := line.split()) == 4 and fields[1] == str(pid) and not fields[3].startswith("Z")]

    current = process_identity(pid)
    # A reused leader belongs to a new group. Never signal that process.
    if current and identity and current != identity:
        return True
    live = members()
    if live == []:
        return True
    if live is None or (current and not identity) or any(int(row[2]) != os.getuid() for row in live):
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        live = members()
        if live == []:
            return True
        if live is None:
            return False
        time.sleep(0.02)
    return False


class WorkerBridge:
    def __init__(self, connection, state):
        self.connection = connection
        self.state = state
        self.serial = 0
        self.checkpoint = decode(state.get("checkpoint_value"))
        self.counters = decode(state["run"].get("counters", encode({})))
        self.input_cursor = state["input_cursor"]
        if state.get("context") is None and state["messages"]:
            self.input_cursor = state["messages"][0]["seq"]

    def rpc(self, kind, payload):
        self.serial += 1
        self.connection.send(
            {
                "epoch": self.state["epoch"],
                "attempt_id": self.state["run"]["attempt_id"],
                "record_id": str(self.serial),
                "type": kind,
                "payload": encode(payload),
            }
        )
        response = self.connection.recv()
        if "error" in response:
            raise RuntimeError(response["error"])
        return decode(response["value"])

    def boundary(self, checkpoint):
        self.checkpoint = checkpoint
        self.counters = {"llm_calls": checkpoint["llm_calls"], "usage": checkpoint["usage"]}
        result = self.rpc("boundary", checkpoint)
        self.goal_revision = result.get("goal_revision", 0)
        return result

    def operation_start(self, call):
        effects = getattr(self, "binding_effects", {})
        return self.rpc(
            "operation_start", {"call": call, "effect_kind": effects.get(call.name, "read_only" if call.name == "read_artifact" else "side_effecting")}
        )

    def operation_finish(self, call, result):
        return self.rpc("operation_finish", {"call": call, "result": result})

    def request_input(self, call, question):
        return self.rpc("request_input", {"call": call, "question": question})

    def poll_control(self):
        return self.rpc("poll_control", {})

    async def emit(self, event):
        import asyncio

        from loom.core import ok

        self.rpc("event", event)
        await asyncio.sleep(0)
        return ok(None)


def worker_main(connection, state, config_path, provider_factory, plugin_registry_factory=None):
    import asyncio

    from loom.service.agent import execute

    os.setsid()
    bridge = WorkerBridge(connection, state)
    try:
        asyncio.run(execute(state, bridge, config_path, provider_factory, plugin_registry_factory))
    except Exception as exc:
        with contextlib.suppress(EOFError, BrokenPipeError, OSError):
            bridge.rpc("failure", {"message": str(exc), "type": type(exc).__name__})
    finally:
        connection.close()


@dataclass
class Attempt:
    process: object
    connection: object
    workspace: str
    epoch: int
    started: float
    accounted: float
    terminal_since: float | None = None
    resource_claims: tuple = ()


def spawn_attempt(state, config_path, provider_factory, plugin_registry_factory=None):
    import time

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=worker_main, args=(child, state, config_path, provider_factory, plugin_registry_factory), daemon=False)
    process.start()
    child.close()
    started = time.monotonic()
    return Attempt(
        process, parent, state["task"]["workspace"], state["epoch"], started, started, resource_claims=tuple(state["task"].get("resource_claims", ()))
    )
