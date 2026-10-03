import json

import pytest

from loom.cli import main
from tests.service.test_api import api as api


def test_cli_create_message_snapshot_and_history(api, capsys):
    service, server, client, path = api
    credential = path / "token"
    credential.write_text(client.token)
    prefix = ["session", "--url", client.url, "--token-file", str(credential)]
    assert main([*prefix, "create", "Maintain", "--workspace", str(path), "--plan-mode", "off"]) == 0
    sid = json.loads(capsys.readouterr().out)["session_id"]
    assert main([*prefix, "message", sid, "Fix regression"]) == 0
    capsys.readouterr()
    assert main([*prefix, "snapshot", sid]) == 0
    assert len(json.loads(capsys.readouterr().out)["messages"]) == 2
    assert main([*prefix, "history", sid, "--limit", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)["events"]) == 2


def stub_tui(monkeypatch):
    pytest.importorskip("textual")
    apps = []

    class App:
        def __init__(self, client, session_id=None):
            self.client, self.session_id = client, session_id
            apps.append(self)

        def run(self):
            pass

    monkeypatch.setattr("loom.client.tui.SessionTuiApp", App)
    return apps


def test_default_cli_opens_new_persisted_session_without_starting_task(api, monkeypatch):
    service, server, client, path = api
    apps = stub_tui(monkeypatch)
    credential = path / "token"
    credential.write_text(client.token)
    monkeypatch.setenv("LOOM_SERVICE_URL", client.url)
    monkeypatch.setenv("LOOM_SERVICE_TOKEN_FILE", str(credential))
    monkeypatch.chdir(path)
    assert main([]) == 0
    assert len(apps) == 1
    state = service.snapshot(apps[0].session_id)
    assert state["task"]["objective"] == ""
    assert state["task"]["state"] == "idle"
    assert state["task"]["workspace"] == str(path.resolve())
    assert state["run"] is None
    assert state["messages"] == []


@pytest.mark.parametrize("state", ["paused", "failed", "running", "queued", "idle", "awaiting_input", "recovering", "completed"])
def test_resume_cli_connects_existing_session_and_respects_execution_state(api, monkeypatch, state):
    service, server, client, path = api
    apps = stub_tui(monkeypatch)
    sid = client.create({"objective": "Maintain", "workspace": str(path)})["session_id"]

    def setup(snapshot, emit, db):
        snapshot["task"]["state"] = state
        if state in {"awaiting_input", "recovering"}:
            snapshot["input_request"] = {
                "id": "question",
                "kind": "recovery" if state == "recovering" else "clarification",
                "state": "pending",
                "question": "Verify?",
            }

    service.store.update(sid, setup)
    credential = path / "token"
    credential.write_text(client.token)
    assert main(["--resume", sid, "--url", client.url, "--token-file", str(credential)]) == 0
    assert [app.session_id for app in apps] == [sid]
    assert len(client.list_sessions()) == 1
    expected = "queued" if state in {"paused", "failed"} else "idle" if state == "completed" else state
    assert service.snapshot(sid)["task"]["state"] == expected


def test_resume_missing_session_does_not_create_replacement(api, monkeypatch, capsys):
    service, server, client, path = api
    apps = stub_tui(monkeypatch)
    credential = path / "token"
    credential.write_text(client.token)
    assert main(["--resume", "missing", "--url", client.url, "--token-file", str(credential)]) == 1
    assert apps == []
    assert client.list_sessions() == []
    assert "not found" in capsys.readouterr().out.lower()


def test_question_arriving_during_resume_opens_session_for_answer(api, monkeypatch):
    from loom.client.protocol import SessionClient

    service, server, client, path = api
    apps = stub_tui(monkeypatch)
    sid = client.create({"objective": "Maintain", "workspace": str(path)})["session_id"]
    service.store.update(sid, lambda state, emit, db: state["task"].update(state="paused"))
    original = SessionClient.command

    def concurrent_question(self, sid, kind, *args, **kwargs):
        if kind == "resume":
            service.store.update(
                sid, lambda state, emit, db: state.update(input_request={"id": "q", "kind": "clarification", "state": "pending", "question": "Which module?"})
            )
        return original(self, sid, kind, *args, **kwargs)

    monkeypatch.setattr(SessionClient, "command", concurrent_question)
    credential = path / "token"
    credential.write_text(client.token)
    assert main(["--resume", sid, "--url", client.url, "--token-file", str(credential)]) == 0
    assert apps[0].session_id == sid
    assert service.snapshot(sid)["task"]["state"] == "paused"


def test_token_budget_options_create_query_update_and_resume(api, monkeypatch, capsys):
    service, server, client, path = api
    apps = stub_tui(monkeypatch)
    credential = path / "token"
    credential.write_text(client.token)
    prefix = ["--url", client.url, "--token-file", str(credential)]
    assert main([*prefix, "--workspace", str(path), "--token-budget", "2.5M"]) == 0
    sid = apps[-1].session_id
    assert service.snapshot(sid)["task"]["limits"]["max_tokens"] == 2_500_000
    session = ["session", *prefix]
    assert main([*session, "budget", sid, "20M", "--command-id", "update"]) == 0
    capsys.readouterr()
    assert main([*session, "budget", sid]) == 0
    assert json.loads(capsys.readouterr().out)["limit"] == 20_000_000
    assert main([*session, "create", "Work", "--workspace", str(path), "--token-budget", "500K"]) == 0
    existing = json.loads(capsys.readouterr().out)["session_id"]
    assert service.snapshot(existing)["task"]["limits"]["max_tokens"] == 500_000
    service.store.update(existing, lambda state, emit, db: state["task"].update(state="paused"))
    assert main([*prefix, "--resume", existing, "--token-budget", "10M"]) == 0
    assert service.snapshot(existing)["task"]["limits"]["max_tokens"] == 10_000_000
    assert service.snapshot(existing)["task"]["state"] == "queued"


@pytest.mark.parametrize("value", ["0", "10MB", "1.5"])
def test_invalid_token_budget_does_not_create_session(api, value):
    service, server, client, path = api
    with pytest.raises(SystemExit) as exc:
        main(["--url", client.url, "--token-budget", value])
    assert exc.value.code == 2
    assert client.list_sessions() == []


def test_budget_query_handles_backend_without_usage_field(api, monkeypatch, capsys):
    service, server, client, path = api
    sid = client.create({"workspace": str(path), "limits": {"max_tokens": 100000}})["session_id"]
    original = service.snapshot

    def legacy_snapshot(sid):
        state = original(sid)
        state.pop("token_budget")
        return state

    monkeypatch.setattr(service, "snapshot", legacy_snapshot)
    credential = path / "token"
    credential.write_text(client.token)
    assert main(["session", "--url", client.url, "--token-file", str(credential), "budget", sid]) == 0
    budget = json.loads(capsys.readouterr().out)
    assert budget["limit"] == 100000
    assert budget["used"] is None
    assert budget["remaining"] is None
    assert "Restart loom serve" in budget["notice"]


def test_unsupported_budget_update_prompts_backend_restart(api, monkeypatch, capsys):
    from loom.service.contracts import ServiceError

    service, server, client, path = api
    sid = client.create({"workspace": str(path), "limits": {"max_tokens": 100000}})["session_id"]

    def legacy_submit(sid, command):
        raise ServiceError("Unknown command type")

    monkeypatch.setattr(service, "submit", legacy_submit)
    credential = path / "token"
    credential.write_text(client.token)
    assert main(["session", "--url", client.url, "--token-file", str(credential), "budget", sid, "10M"]) == 1
    assert "Restart loom serve" in capsys.readouterr().out
    assert service.store.snapshot(sid)["task"]["limits"]["max_tokens"] == 100000
