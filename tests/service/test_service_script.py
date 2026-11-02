"""Exercise start/restart argument forwarding without touching a real service."""

import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/loom-service.sh"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts/loom-service.sh", script)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "launch.json"
    fake = bindir / "fake-loom"
    fake.write_text(
        f"#!{sys.executable}\nimport os,json,signal,time\nfrom pathlib import Path\n"
        'Path(os.environ["TEST_LAUNCH"]).write_text(json.dumps({"pid":os.getpid(),"args":__import__("sys").argv[1:]}))\n'
        "signal.signal(signal.SIGTERM,lambda *args:exit(0))\nwhile True: time.sleep(.1)\n"
    )
    curl = bindir / "curl"
    curl.write_text(
        f"#!{sys.executable}\nimport json,os\nfrom pathlib import Path\n"
        'try:\n data=json.loads(Path(os.environ["TEST_LAUNCH"]).read_text()); os.kill(data["pid"],0)\n'
        'except (OSError,ValueError): print("000",end="")\nelse: print("200",end="")\n'
    )
    fake.chmod(0o755)
    curl.chmod(0o755)
    lsof = bindir / "lsof"
    lsof.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        'try: pid=json.loads(Path(os.environ["TEST_LAUNCH"]).read_text())["pid"]; os.kill(pid,0)\n'
        'except (OSError,ValueError): sys.exit(1)\n'
        'if sys.argv[-1].endswith("supervisor.lock") and os.environ.get("TEST_WRONG_OWNER"): print(pid+100000)\n'
        'else: print(pid)\n'
    )
    lsof.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    env.update(PATH=str(bindir) + os.pathsep + env["PATH"], LOOM_BIN=str(fake), TEST_LAUNCH=str(record), LOOM_STOP_TIMEOUT="3")

    def run(*args, **overrides):
        return subprocess.run(["bash", str(script), *args], cwd=tmp_path, env={**env, **overrides}, capture_output=True, text=True, timeout=15)

    yield root, record, run
    if record.exists():
        with suppress(ProcessLookupError):
            os.kill(json.loads(record.read_text())["pid"], signal.SIGTERM)


@pytest.mark.parametrize("filename", ["config.yaml", "config.yml"])
def test_start_and_restart_explicitly_pass_project_config(launcher, filename):
    root, record, run = launcher
    config = root / filename
    config.write_text("models: {}\n")
    result = run("start", "--", "--no-auth")
    assert result.returncode == 0, result.stderr
    first = json.loads(record.read_text())
    assert first["args"][-3:] == ["--config", str(config), "--no-auth"]
    assert "--" not in first["args"]
    result = run("restart", "--", "--no-auth")
    assert result.returncode == 0, result.stderr
    second = json.loads(record.read_text())
    assert second["pid"] != first["pid"]
    assert second["args"] == first["args"]
    result = run("restart", "--config", str(root / "missing.yaml"))
    assert result.returncode != 0
    assert json.loads(record.read_text()) == second
    assert run("status").returncode == 0


@pytest.mark.parametrize("explicit", [False, True])
def test_config_override_precedence(launcher, explicit):
    root, record, run = launcher
    for name in ["config.yaml", "environment.yaml", "chosen config.yaml"]:
        (root / name).write_text("models: {}\n")
    args = ["start", "--", f"--config={root / 'chosen config.yaml'}"] if explicit else ["start"]
    result = run(*args, LOOM_CONFIG=str(root / "environment.yaml"))
    assert result.returncode == 0, result.stderr
    forwarded = json.loads(record.read_text())["args"]
    if explicit:
        assert forwarded[-1] == f"--config={root / 'chosen config.yaml'}"
        assert "--config" not in forwarded
    else:
        assert forwarded[-2:] == ["--config", str(root / "environment.yaml")]


@pytest.mark.parametrize("stale", [False, True])
def test_restart_recovers_missing_or_stale_pid_only_for_same_listener_and_data_directory(launcher, stale):
    root, record, run = launcher
    assert run("start").returncode == 0
    before = json.loads(record.read_text())
    data = root / "runs/service"
    (data / "supervisor.lock").touch()
    if stale:
        (data / "loom.pid").write_text("99999999")
    else:
        (data / "loom.pid").unlink()
    refused = run("restart", TEST_WRONG_OWNER="1")
    assert refused.returncode != 0
    assert json.loads(record.read_text()) == before
    os.kill(before["pid"], 0)
    result = run("restart")
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text())["pid"] != before["pid"]
