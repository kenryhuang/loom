import json
import threading
import urllib.error
import urllib.request

import pytest

from loom.client.projection import SessionProjection
from loom.client.protocol import SessionClient
from loom.runtime.checkpoints import encode
from loom.service.api import ServiceHTTPServer, installation_token
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService


@pytest.fixture
def api(tmp_path):
    service = LoomService(tmp_path / "data")
    server = ServiceHTTPServer(("127.0.0.1", 0), service, "private-test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = SessionClient(url, "private-test-token")
    yield service, server, client, tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    service.close()


def new(client, path, cid="create"):
    return client.create({"objective": "Maintain", "workspace": str(path)}, command_id=cid)["session_id"]


def test_auth_validation_conflicts_and_private_snapshot(api):
    service, server, client, path = api
    with pytest.raises(ServiceError) as unauth:
        SessionClient(client.url, "bad").list_sessions()
    assert unauth.value.status == 401
    sid = new(client, path)
    assert client.create({"objective": "Maintain", "workspace": str(path)}, command_id="create")["session_id"] == sid
    with pytest.raises(ServiceError) as conflict:
        client.create({"objective": "Different", "workspace": str(path)}, command_id="create")
    assert conflict.value.status == 409
    with pytest.raises(ServiceError):
        client.create({"objective": "Maintain", "workspace": str(path), "api_key": "secret"})
    assert "context" not in client.snapshot(sid)
    assert len(client.list_sessions()) == 1
    token = installation_token(path / "credential")
    assert installation_token(path / "credential") == token
    assert (path / "credential").stat().st_mode & 0o777 == 0o600


def test_snapshot_subscribe_race_two_readers_and_resume_cursor(api):
    service, server, client, path = api
    sid = new(client, path)
    snapshot = client.snapshot(sid)
    client.command(sid, "submit_message", {"content": "New priority"}, command_id="m1")
    one = client.events(sid, snapshot["event_cursor"])
    two = client.events(sid, snapshot["event_cursor"])
    first = next(one)
    assert next(two) == first
    assert first["seq"] == snapshot["event_cursor"] + 1
    one.close()
    two.close()
    again = client.events(sid, 0, last_event_id=first["seq"])
    assert next(again)["seq"] == first["seq"] + 1
    again.close()
    history = client.history(sid, limit=2)
    assert len(history["events"]) == 2
    assert history["next_before"] == history["events"][0]["seq"]
    with pytest.raises(ServiceError) as future:
        next(client.events(sid, 100000))
    assert future.value.status == 409
    # Subscriber disconnect never changes task execution state.
    assert client.snapshot(sid)["task"]["state"] == "queued"


def test_artifact_scoping_and_body_errors(api):
    service, server, client, path = api
    one = new(client, path)
    two = new(client, path, "create-other")
    ref = service.store.save_artifact(one, {"report": "ok"}, "report")
    assert json.loads(client.artifact(one, ref["sha256"])) == {"report": "ok"}
    with pytest.raises(ServiceError) as denied:
        client.artifact(two, ref["sha256"])
    assert denied.value.status == 404
    for data in (b"{", b"x" * (1_048_576 + 1)):
        request = urllib.request.Request(client.url + "/v1/sessions", data=data, headers={"Authorization": "Bearer private-test-token"})
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request)
        assert failure.value.code in {400, 413}
        assert "error" in json.loads(failure.value.read())
    for payload in ([], {"command_id": "bad", "type": "bogus", "payload": {}}):
        with pytest.raises(ServiceError) as invalid:
            client._json(client._path(one, "commands"), payload)
        assert invalid.value.status == 400
    with pytest.raises(ServiceError):
        client.create({"objective": "Maintain", "workspace": str(path), "plan_mode": []})


def test_worker_stream_offsets_are_durable_and_large_detail_is_lazy(api):
    service, server, client, path = api
    sid = new(client, path)
    state = service.prepare_attempt(sid)
    for index, delta in enumerate(("abc", "de")):
        service.worker_record(
            sid,
            {
                "attempt_id": state["run"]["attempt_id"],
                "epoch": state["epoch"],
                "record_id": str(index),
                "type": "event",
                "payload": encode({"type": "llm.content.delta", "llm_call_id": "l", "delta": delta}),
            },
        )
    deltas = [e for e in service.events(sid) if e["type"] == "llm.content.delta"]
    assert [e["payload"]["offset"] for e in deltas] == [0, 3]
    snapshot = client.snapshot(sid)
    projection = SessionProjection(snapshot)
    assert projection.streams["l:content"] == "abcde"
    service.worker_record(
        sid,
        {
            "attempt_id": state["run"]["attempt_id"],
            "epoch": state["epoch"],
            "record_id": "next-delta",
            "type": "event",
            "payload": encode({"type": "llm.content.delta", "llm_call_id": "l", "delta": "f"}),
        },
    )
    assert projection.apply(service.events(sid, snapshot["event_cursor"])[0])
    assert projection.streams["l:content"] == "abcdef"
    service.worker_record(
        sid,
        {
            "attempt_id": state["run"]["attempt_id"],
            "epoch": state["epoch"],
            "record_id": "large",
            "type": "event",
            "payload": encode({"type": "tool.completed", "tool_call_id": "t", "output": "x" * 40000}),
        },
    )
    detail = service.events(sid)[-1]["payload"]
    assert len(json.dumps(detail)) < 2000
    assert json.loads(client.artifact(sid, detail["artifact"]["sha256"]))["output"] == "x" * 40000
