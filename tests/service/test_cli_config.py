"""The service, model picker and evaluator must use the same project config."""

from pathlib import Path

import pytest

from loom.service import cli
from loom.service.settings import Settings
from loom.web.frontend import WebFrontend


@pytest.mark.parametrize("filename", ["config.yaml", "config.yml", None])
def test_serve_discovers_models_without_explicit_config(tmp_path, monkeypatch, filename):
    monkeypatch.chdir(tmp_path)
    if filename:
        (tmp_path / filename).write_text("providers:\n  local:\n    models:\n      solver: {}\n      evaluator: {}\ndefault_model: local/solver\n")
    captured = {}

    class Server:
        server_port = 8765

        def __init__(self, address, service, token, **options):
            captured["path"] = service.config_path
            captured["models"] = [m["id"] for m in WebFrontend().catalog(service)["models"]]
            captured["evaluation_models"] = [m["id"] for m in service.background.semantic.options()["models"]]
            captured["settings_path"] = Settings(service).path

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "ServiceHTTPServer", Server)
    monkeypatch.setattr(cli.LoomService, "start", lambda self: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    assert cli.main(["--data-dir", str(tmp_path / "data")]) == 0
    expected = ["local/solver", "local/evaluator"] if filename else []
    assert captured["models"] == captured["evaluation_models"] == expected
    assert captured["path"] == (str(tmp_path / filename) if filename else None)
    assert captured["settings_path"] == tmp_path / (filename or "config.yaml")


def test_explicit_config_wins_over_project_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("models: {}\n")
    (tmp_path / "selected.yaml").write_text("models: {}\n")
    captured = {}

    class Service:
        def __init__(self, directory, **options):
            captured.update(options)

        def start(self):
            pass

        def close(self):
            pass

    class Server:
        server_port = 8765

        def __init__(self, *args, **kwargs):
            pass

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "LoomService", Service)
    monkeypatch.setattr(cli, "ServiceHTTPServer", Server)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    assert cli.main(["--data-dir", str(tmp_path / "data"), "--config", "selected.yaml"]) == 0
    assert captured["config_path"] == Path("selected.yaml")
