import json
import os
import subprocess
import sys
import time
from pathlib import Path

from loom.client.protocol import SessionClient


def start_daemon(directory):
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.service.smoke", "--data-dir", str(directory)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src") + os.pathsep + str(root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    address = json.loads(process.stdout.readline())["url"]
    return process, SessionClient(address, (directory / "credential").read_text().strip())


def stop_daemon(process):
    process.terminate()
    _, errors = process.communicate(timeout=8)
    assert process.returncode == 0, errors


def wait(client, sid, state):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = client.snapshot(sid)
        if snapshot["task"]["state"] == state:
            return snapshot
        time.sleep(0.03)
    raise AssertionError(snapshot)


def test_real_daemon_disconnect_restart_and_resume(tmp_path):
    directory = tmp_path / "data"
    daemon, client = start_daemon(directory)
    try:
        sid = client.create({"objective": "question", "workspace": str(tmp_path), "plan_mode": "off"})["session_id"]
        pending = wait(client, sid, "awaiting_input")
        stop_daemon(daemon)
        daemon, client = start_daemon(directory)
        assert client.snapshot(sid)["run"]["id"] == pending["run"]["id"]
        client.command(sid, "answer_input", {"request_id": pending["input_request"]["id"], "answer": "Payments"}, command_id="answer")
        event_stream = client.events(sid, pending["event_cursor"])
        assert next(event_stream)["seq"] > pending["event_cursor"]
        event_stream.close()
        finished = wait(client, sid, "idle")
        assert finished["run"]["id"] == pending["run"]["id"]
        slow = client.create({"objective": "slow", "workspace": str(tmp_path), "plan_mode": "off"})["session_id"]
        stream = client.events(slow)
        next(stream)
        stream.close()
        assert wait(client, slow, "idle")["run"]["state"] == "completed"
    finally:
        if daemon.poll() is None:
            stop_daemon(daemon)
