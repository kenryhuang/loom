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
from loom.web.frontend import WebFrontend


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


def test_browser_assets_are_public_but_session_data_and_catalog_are_private(api):
    service, server, client, path = api
    for route, mime in (("/", "text/html"), ("/web/", "text/html"), ("/web/assets/app.mjs", "text/javascript"),
                        ("/web/assets/views/feed.mjs", "text/javascript"), ("/web/assets/styles.css", "text/css")):
        with urllib.request.urlopen(client.url + route) as response:
            body = response.read().decode()
            assert response.status == 200 and response.headers["Content-Type"].startswith(mime)
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert "private-test-token" not in body
    for route in ("/v1/sessions", "/v1/web/catalog"):
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(client.url + route)
        assert denied.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as query_token:
        urllib.request.urlopen(client.url + "/web/?token=private-test-token")
    assert query_token.value.code == 400
    catalog = client._json("/v1/web/catalog")
    assert [template["id"] for template in catalog["templates"]] == ["general", "research", "coding"]
    sid = client.create({"objective": "Explain architecture", "task_spec": catalog["templates"][0]["task_spec"]})["session_id"]
    assert client.snapshot(sid)["task"]["workspace"] is None
    for route in ("/web/assets/../frontend.py", "/web/assets/%2e%2e/frontend.py", "/web/assets/index.html", "/web/assets/views/missing.mjs"):
        with pytest.raises(ServiceError) as missing:
            client._json(route)
        assert missing.value.status == 404
    server.web_frontend = None
    with pytest.raises(ServiceError) as disabled:
        client._json("/web/")
    assert disabled.value.status == 404
    assert client.snapshot(sid)["session_id"] == sid


def test_web_catalog_is_replaceable_and_excludes_server_model_secrets(api):
    service, server, client, path = api
    config = path / "model.yaml"
    config.write_text("default_model: main\nmodels:\n  main:\n    provider: openai\n    model: browser-test\n"
                      "    api_key: never-share-this\n    base_url: https://private.example/v1\n"
                      "    request_options:\n      private_header: secret-header\n")
    service.config_path = str(config)
    templates = [{"id": "custom", "label": "Custom domain", "task_spec": {"tools": {"collections": ["task_control"]}}}]
    server.web_frontend = WebFrontend(templates=templates)
    catalog = client._json("/v1/web/catalog")
    assert catalog["templates"] == templates
    assert catalog["models"] == [{"id": "main", "label": "main · browser-test"}]
    assert catalog["default_model"] == "main"
    assert all(secret not in json.dumps(catalog) for secret in ("never-share-this", "private.example", "secret-header"))
    catalog["templates"][0]["label"] = "Changed locally"
    assert server.web_frontend.templates[0]["label"] == "Custom domain"


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
