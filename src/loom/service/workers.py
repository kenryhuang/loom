"""Spawn-only worker transport and process-group cleanup."""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import subprocess
from dataclasses import dataclass

from loom.runtime.checkpoints import decode, encode


def process_identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def reap_group(pid, identity):
    if not pid or not identity or process_identity(pid) != identity:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


class WorkerBridge:
    def __init__(self, connection, state):
        self.connection = connection
        self.state = state
        self.serial = 0
        self.checkpoint = decode(state.get("checkpoint_value"))
        self.counters = decode(state["run"].get("counters", encode({})))
        self.input_cursor = state["input_cursor"]

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
        return self.rpc("boundary", checkpoint)

    def operation_start(self, call):
        return self.rpc("operation_start", call)

    def operation_finish(self, call, result):
        return self.rpc("operation_finish", {"call": call, "result": result})

    def request_input(self, call, question):
        return self.rpc("request_input", {"call": call, "question": question})

    async def emit(self, event):
        from loom.core import ok

        self.rpc("event", event)
        return ok(None)


def worker_main(connection, state, config_path, provider_factory):
    import asyncio

    from loom.service.agent import execute

    os.setsid()
    bridge = WorkerBridge(connection, state)
    try:
        asyncio.run(execute(state, bridge, config_path, provider_factory))
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


def spawn_attempt(state, config_path, provider_factory):
    import time

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=worker_main, args=(child, state, config_path, provider_factory), daemon=False)
    process.start()
    child.close()
    return Attempt(process, parent, state["task"]["workspace"], state["epoch"], time.monotonic())
