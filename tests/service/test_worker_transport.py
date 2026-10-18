import asyncio
import threading

import pytest

from loom.llm.api import LlmToolCall
from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from loom.service.worker_transport import WorkerTransport
from loom.service.workers import WorkerBridge
from tests.service.test_controller import create


def state():
    return dict(epoch=1, run=dict(attempt_id="a"), input_cursor=0, messages=[])


def delta(text="x", kind="llm.reasoning.delta"):
    return dict(type=kind, delta=text, llm_call_id="m", at="2026-10-18T00:00:00.000Z")


class Connection:
    def __init__(self):
        self.sent = []
        self.gate = threading.Event()
        self.started = threading.Event()
        self.response = {"value": encode(None)}

    def send(self, record):
        self.sent.append(record)
        self.started.set()

    def recv(self):
        assert self.gate.wait(5), "test failed to release RPC"
        return self.response

    def close(self):
        self.gate.set()


def unpack(records):
    result = []
    for r in records:
        value = decode(r["payload"])
        result.extend(value if r["type"] == "event_batch" else [value])
    return result


@pytest.mark.asyncio
async def test_paced_stream_batches_chunks_without_delaying_completion():
    connection = Connection()
    connection.gate.set()
    bridge = WorkerBridge(connection, state())
    events = [delta(str(i)) for i in range(18)]
    try:
        for event in events:
            await bridge.emit(event)
            await asyncio.sleep(0.075)
        finish = dict(type="llm.stream.completed", llm_call_id="m")
        await asyncio.wait_for(bridge.emit(finish), 1)
        assert unpack(connection.sent) == [*events, finish]
        # A 25 ms window sent virtually every real-world chunk separately.
        assert len(connection.sent) <= 12
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_deltas_do_not_wait_for_ack_but_completion_flushes_in_order():
    connection = Connection()
    bridge = WorkerBridge(connection, state())
    events = [delta(str(i)) for i in range(200)]
    try:
        for event in events:
            await bridge.emit(event)
        assert bridge.transport.count == 200  # Includes unacknowledged in-flight data.
        finish = dict(type="llm.completed", llm_call_id="m")
        task = asyncio.create_task(bridge.emit(finish))
        await asyncio.sleep(0)
        assert not task.done()
        connection.gate.set()
        await asyncio.wait_for(task, 3)
        assert unpack(connection.sent) == [*events, finish]
        assert len(connection.sent) < 20
        assert connection.sent[-1]["type"] == "event"
    finally:
        connection.gate.set()
        bridge.close()


@pytest.mark.asyncio
async def test_full_queue_applies_backpressure_without_blocking_event_loop(monkeypatch):
    monkeypatch.setattr("loom.service.workers.WorkerTransport", lambda c, s: WorkerTransport(c, s, max_events=2))
    connection = Connection()
    bridge = WorkerBridge(connection, state())
    try:
        await bridge.emit(delta("a"))
        await bridge.emit(delta("b"))
        blocked = asyncio.create_task(bridge.emit(delta("c")))
        await asyncio.sleep(0)
        assert not blocked.done() and bridge.transport.count == 2
        # This coroutine can still run while an RPC is waiting and the queue is full.
        heartbeat = asyncio.create_task(asyncio.sleep(0, result=True))
        assert await heartbeat
        connection.gate.set()
        await asyncio.wait_for(blocked, 3)
        await bridge.emit(dict(type="llm.stream.completed", llm_call_id="m"))
        assert [e.get("delta") for e in unpack(connection.sent)[:3]] == ["a", "b", "c"]
    finally:
        connection.gate.set()
        bridge.close()


def test_byte_limit_failure_and_tool_barrier():
    connection = Connection()
    transport = WorkerTransport(connection, state(), max_bytes=300)
    try:
        first = transport.request("event", delta("x" * 200))
        transport.enqueue(first)
        second = transport.request("event", delta("y" * 200))
        assert not transport.enqueue(second, block=False)
        assert transport.count == 1
        connection.response = {"error": "commit failed"}
        connection.gate.set()
        with pytest.raises(RuntimeError, match="commit failed"):
            first.future.result(timeout=3)
        with pytest.raises(RuntimeError, match="transport failed"):
            transport.enqueue(transport.request("operation_start", {"id": "edit"}))
        assert len(connection.sent) == 1  # Never execute a tool after losing prior trace writes.
    finally:
        connection.gate.set()
        with pytest.raises(RuntimeError, match="transport failed"):
            transport.close()


def test_sync_tool_rpc_waits_for_prior_delta_commit():
    connection = Connection()
    bridge = WorkerBridge(connection, state())
    try:
        asyncio.run(bridge.emit(delta()))
        done = threading.Event()
        thread = threading.Thread(target=lambda: (bridge.operation_start(LlmToolCall("edit", "edit_file", "{}")), done.set()))
        thread.start()
        assert connection.started.wait(2)
        assert not done.is_set()
        connection.gate.set()
        thread.join(timeout=3)
        assert done.is_set()
        assert [r["type"] for r in connection.sent] == ["event", "operation_start"]
    finally:
        connection.gate.set()
        bridge.close()


def test_delta_batch_is_atomic_idempotent_and_preserves_offsets(tmp_path):
    service = LoomService(tmp_path / "data")
    try:
        sid = create(service, tmp_path / "workspace")
        descriptor = service.prepare_attempt(sid)
        events = [delta("a"), delta("bc"), delta("body", "llm.content.delta")]
        record = dict(epoch=descriptor["epoch"], attempt_id=descriptor["run"]["attempt_id"], record_id="batch", type="event_batch", payload=encode(events))
        service.worker_record(sid, record)
        service.worker_record(sid, record)
        saved = [e for e in service.events(sid, limit=1000) if e["type"].endswith(".delta")]
        assert [e["payload"]["offset"] for e in saved] == [0, 1, 0]
        assert [e["payload"]["delta"] for e in saved] == ["a", "bc", "body"]
        snapshot = service.store.snapshot(sid)
        assert snapshot["run"]["streams"]["m:reasoning"] == "abc"
        assert snapshot["run"]["streams"]["m:content"] == "body"
        bad = {**record, "record_id": "bad", "payload": encode([delta("lost"), dict(type="llm.completed")])}
        with pytest.raises(ServiceError, match="Invalid worker delta batch"):
            service.worker_record(sid, bad)
        assert service.store.snapshot(sid)["event_cursor"] == snapshot["event_cursor"]
        with pytest.raises(ServiceError, match="Stale executor epoch"):
            service.worker_record(sid, {**record, "epoch": -1})
    finally:
        service.close()


def test_close_drains_pending_preview_events():
    connection = Connection()
    connection.gate.set()
    bridge = WorkerBridge(connection, state())
    asyncio.run(bridge.emit(delta("last")))
    bridge.close()
    assert unpack(connection.sent) == [delta("last")]
    assert not bridge.transport.thread.is_alive()


def test_batch_write_failure_reaches_queued_boundary_without_sending_it():
    connection = Connection()
    transport = WorkerTransport(connection, state())
    first = transport.request("event", delta())
    boundary = transport.request("boundary", {})
    transport.enqueue(first)
    transport.enqueue(boundary)
    connection.response = {"error": "database unavailable"}
    connection.gate.set()
    try:
        for item in (first, boundary):
            with pytest.raises(RuntimeError, match="database unavailable"):
                item.future.result(timeout=3)
        assert [r["type"] for r in connection.sent] == ["event"]
    finally:
        with pytest.raises(RuntimeError, match="transport failed"):
            transport.close()


# Module-level factory is imported by the real spawned service worker.
def streaming_provider_factory(state):
    from loom.llm.api import LlmStreamEvent
    from tests.service.fakes import FakeProvider

    class StreamingProvider(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            response = (await self.chat(messages, **kwargs)).unwrap()
            for _ in range(150):
                yield LlmStreamEvent(kind="reasoning.delta", reasoning_delta="thinking ")
            if response.content:
                yield LlmStreamEvent(kind="content.delta", content_delta=response.content)
            yield LlmStreamEvent(kind="completed", response=response)

    return StreamingProvider(state)


def test_spawned_streaming_task_saves_all_deltas_before_model_completion(tmp_path):
    from tests.service.test_controller import wait_state

    class CountingService(LoomService):
        batches = 0

        def worker_record(self, sid, record):
            self.batches += record["type"] == "event_batch"
            return super().worker_record(sid, record)

    service = CountingService(tmp_path / "data", provider_factory=streaming_provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace")
        wait_state(service, sid, "idle", timeout=20)
        events = service.events(sid, limit=1000)
        deltas = [e for e in events if e["type"] == "llm.reasoning.delta"]
        assert deltas and service.batches > 0
        for call_id in {e["payload"]["llm_call_id"] for e in deltas}:
            chunks = [e for e in deltas if e["payload"]["llm_call_id"] == call_id]
            completed = next(e for e in events if e["type"] == "llm.completed" and e["payload"]["llm_call_id"] == call_id)
            assert len(chunks) == 150
            assert [e["payload"]["offset"] for e in chunks] == list(range(0, 150 * 9, 9))
            assert chunks[-1]["seq"] < completed["seq"]
        assert not service.store.snapshot(sid)["run"]["streams"]
    finally:
        service.close()


def test_sqlite_batch_error_rejects_worker_without_killing_supervisor(tmp_path):
    import sqlite3

    from tests.service.test_controller import wait_state

    class FailingStoreService(LoomService):
        def _worker_event(self, state, emit, db, value):
            if value["type"] == "llm.reasoning.delta":
                raise sqlite3.OperationalError("simulated commit failure")
            return super()._worker_event(state, emit, db, value)

    service = FailingStoreService(tmp_path / "data", provider_factory=streaming_provider_factory).start()
    try:
        sid = create(service, tmp_path / "workspace")
        wait_state(service, sid, "failed", timeout=20)
        assert service.thread.is_alive()
        events = service.events(sid, limit=1000)
        assert not any(e["type"] == "llm.reasoning.delta" for e in events)
        assert not any(e["type"] == "operation.started" for e in events)
    finally:
        service.close()


def test_identical_internal_evidence_reuses_only_committed_reference():
    connection = Connection()
    connection.response = {"value": encode({"sha256": "saved", "kind": "acceptance_tool_evidence"})}
    connection.gate.set()
    bridge = WorkerBridge(connection, state())
    payload = {"kind": "acceptance_tool_evidence", "value": {"input": {"path": "a"}, "output": {"content": "original"}}}
    try:
        first = bridge.rpc("publish_artifact", payload)
        repeated = bridge.rpc("publish_artifact", payload)
        assert first == repeated and len(connection.sent) == 1
        repeated["sha256"] = "changed by caller"
        assert bridge.rpc("publish_artifact", payload)["sha256"] == "saved"
        payload["value"]["output"]["content"] = "file changed"
        bridge.rpc("publish_artifact", payload)
        assert len(connection.sent) == 2
        # User-facing outputs still publish each time, even with identical content.
        bridge.rpc("publish_artifact", {"kind": "report", "value": "report"})
        bridge.rpc("publish_artifact", {"kind": "report", "value": "report"})
        assert len(connection.sent) == 4
        for i in range(130):
            bridge.rpc("publish_artifact", {"kind": "acceptance_tool_evidence", "value": i})
        assert len(bridge._evidence_artifacts) == 128
    finally:
        bridge.close()


def test_failed_evidence_publication_is_never_cached():
    connection = Connection()
    connection.response = {"error": "disk failure"}
    connection.gate.set()
    bridge = WorkerBridge(connection, state())
    try:
        with pytest.raises(RuntimeError, match="disk failure"):
            bridge.rpc("publish_artifact", {"kind": "acceptance_tool_evidence", "value": {"input": {}, "output": "x"}})
        assert not bridge._evidence_artifacts
    finally:
        with pytest.raises(RuntimeError, match="transport failed"):
            bridge.close()
