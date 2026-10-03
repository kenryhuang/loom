import json

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
