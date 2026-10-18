"""Ordered, bounded worker outbox; one thread exclusively owns the RPC pipe."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass

from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import canonical

DELTA_TYPES = frozenset({"llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta", "llm.tool_call.arguments.delta"})
BATCH_EVENTS = 64
BATCH_BYTES = 65536


@dataclass
class Request:
    kind: str
    payload: object
    size: int
    delta: bool
    future: Future


class WorkerTransport:
    def __init__(self, connection, state, *, max_events=256, max_bytes=524288, batch_delay=0.15):
        self.connection = connection
        self.epoch = state["epoch"]
        self.attempt_id = state["run"]["attempt_id"]
        self.max_events, self.max_bytes, self.batch_delay = max_events, max_bytes, batch_delay
        self.condition = threading.Condition()
        self.pending = deque()
        self.count = self.bytes = self.serial = 0
        self.error = None
        self.closing = False
        self.thread = threading.Thread(target=self._run, name="loom-worker-outbox", daemon=True)
        self.thread.start()

    def request(self, kind, payload):
        value = encode(payload)
        size = len(canonical(value).encode())
        delta = kind == "event" and payload.get("type") in DELTA_TYPES and isinstance(payload.get("delta"), str) and size <= min(BATCH_BYTES, self.max_bytes)
        return Request(kind, value, size, delta, Future())

    def enqueue(self, request, *, block=True):
        with self.condition:
            while True:
                if self.error:
                    raise RuntimeError("Worker transport failed") from self.error
                if self.closing:
                    raise RuntimeError("Worker transport closed")
                # Count in-flight data too. A single oversized critical request
                # is admitted only when otherwise empty; deltas never bypass limits.
                if self.count < self.max_events and (self.bytes + request.size <= self.max_bytes or not self.count):
                    self.pending.append(request)
                    self.count += 1
                    self.bytes += request.size
                    self.condition.notify_all()
                    return True
                if not block:
                    return False
                self.condition.wait()

    def _take(self):
        with self.condition:
            while not self.pending and not self.closing:
                self.condition.wait()
            if not self.pending:
                return []
            batch = [self.pending.popleft()]
            size = batch[0].size
            deadline = time.monotonic() + self.batch_delay
            while batch[0].delta and len(batch) < BATCH_EVENTS:
                if self.pending:
                    item = self.pending[0]
                    if not item.delta or size + item.size > BATCH_BYTES:
                        break
                    batch.append(self.pending.popleft())
                    size += item.size
                else:
                    remaining = deadline - time.monotonic()
                    if self.closing or remaining <= 0:
                        break
                    self.condition.wait(remaining)
            return batch

    def _run(self):
        batch = []
        try:
            while batch := self._take():
                self.serial += 1
                grouped = len(batch) > 1
                self.connection.send(
                    dict(
                        epoch=self.epoch,
                        attempt_id=self.attempt_id,
                        record_id=str(self.serial),
                        type="event_batch" if grouped else batch[0].kind,
                        payload=[r.payload for r in batch] if grouped else batch[0].payload,
                    )
                )
                response = self.connection.recv()
                if "error" in response:
                    raise RuntimeError(response["error"])
                value = decode(response["value"])
                with self.condition:
                    self.count -= len(batch)
                    self.bytes -= sum(r.size for r in batch)
                    self.condition.notify_all()
                for item in batch:
                    item.future.set_result(value)
        except Exception as exc:
            with self.condition:
                self.error = exc
                failed = batch + list(self.pending)
                self.pending.clear()
                self.condition.notify_all()
            for item in failed:
                if not item.future.done():
                    item.future.set_exception(exc)

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.connection.close()
            raise RuntimeError("Worker transport did not drain before shutdown")
        if self.error:
            raise RuntimeError("Worker transport failed") from self.error
