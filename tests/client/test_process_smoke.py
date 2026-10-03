import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_new_session_accepts_first_task_through_real_http_tui(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from loom.client.tui import SessionTuiApp

    daemon, client = start_daemon(tmp_path / "data")
    try:
        sid = client.create({"workspace": str(tmp_path), "plan_mode": "off"})["session_id"]
        app = SessionTuiApp(client, sid)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert client.snapshot(sid)["run"] is None
            app.query_one("#message", Input).value = "Maintain parser"
            await pilot.press("enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                await pilot.pause(0.05)
                state = app.projection.snapshot
                if state["task"]["state"] == "idle" and any(m["role"] == "assistant" for m in state["messages"]):
                    break
            assert state["task"]["objective"] == "Maintain parser"
            assert state["task"]["state"] == "idle"
            assert sum(m["role"] == "user" for m in state["messages"]) == 1
            assert any(m["role"] == "assistant" for m in state["messages"])
    finally:
        if daemon.poll() is None:
            stop_daemon(daemon)
