from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from loom.core import err, make_loom_error, ok
from loom.optimize.cli import OptimizeCliOptions
from loom.optimize.tui_runner import run_optimize_with_tui


def _options(tmp_path: Path) -> OptimizeCliOptions:
    return OptimizeCliOptions(
        "run",
        trace=tmp_path / "trace.jsonl",
        tasks=tmp_path / "tasks.jsonl",
        config=tmp_path / "config.yaml",
        tui=True,
        output_dir=tmp_path / "runs",
    )


def _event(sequence: int, event_type: str, *, status="running"):
    return {
        "schema_version": "loom.optimization.event.v1",
        "type": event_type,
        "sequence": sequence,
        "at": "2026-07-19T00:00:00.000Z",
        "optimization_id": "opt_test",
        "campaign_id": "cmp_test",
        "stage": "preflight_complete",
        "status": status,
        "scope": {},
        "payload": {},
    }


class AutoDetachApp:
    def __init__(self, collector, control, *, approval_handler=None):
        self.collector = collector
        self.control = control
        self.approval_handler = approval_handler
        self.started = asyncio.Event()
        self.exited = asyncio.Event()

    async def wait_started(self):
        await self.started.wait()

    async def run_async(self):
        self.started.set()
        while self.collector.state.terminal_status is None:
            await asyncio.sleep(0)
        self.exited.set()


@pytest.mark.asyncio
async def test_runner_starts_app_before_job_and_forwards_snapshot_and_stage(monkeypatch, tmp_path: Path):
    holder = {}

    def factory(*args, **kwargs):
        holder["app"] = AutoDetachApp(*args, **kwargs)
        return holder["app"]

    async def fake_job(options, *, observer=None, control=None):
        assert options.tui and holder["app"].started.is_set()
        assert control is holder["app"].control
        await observer.emit(_event(1, "optimization.snapshot.loaded", status="running"))
        await observer.emit(_event(2, "optimization.stage.started"))
        await observer.emit(_event(3, "optimization.completed", status="completed"))
        return ok({"optimization_id": "opt_test", "disposition": "completed"})

    monkeypatch.setattr("loom.optimize.tui_runner.run_optimize", fake_job)

    result = await run_optimize_with_tui(_options(tmp_path), app_factory=factory)

    assert result.ok
    assert [event["type"] for event in holder["app"].collector.state.recent_events] == [
        "optimization.snapshot.loaded",
        "optimization.stage.started",
        "optimization.completed",
    ]


class StartupFailureApp(AutoDetachApp):
    async def run_async(self):
        raise RuntimeError("terminal unavailable")


@pytest.mark.asyncio
async def test_startup_failure_prevents_optimization_job(monkeypatch, tmp_path: Path):
    called = False

    async def fake_job(options, *, observer=None, control=None):
        nonlocal called
        called = True
        return ok(None)

    monkeypatch.setattr("loom.optimize.tui_runner.run_optimize", fake_job)

    result = await run_optimize_with_tui(_options(tmp_path), app_factory=StartupFailureApp)

    assert not result.ok
    assert result.error.code == "TUI_UNAVAILABLE"
    assert not called


class ImmediateDetachApp(AutoDetachApp):
    async def run_async(self):
        self.started.set()


@pytest.mark.asyncio
async def test_detachment_does_not_cancel_job_and_switches_to_text_fallback(monkeypatch, tmp_path: Path):
    stream = io.StringIO()

    async def fake_job(options, *, observer=None, control=None):
        await asyncio.sleep(0.01)
        await observer.emit(_event(1, "optimization.stage.started"))
        return ok({"disposition": "completed"})

    monkeypatch.setattr("loom.optimize.tui_runner.run_optimize", fake_job)

    result = await run_optimize_with_tui(_options(tmp_path), app_factory=ImmediateDetachApp, fallback_stream=stream)

    assert result.ok
    assert "optimization.stage.started" in stream.getvalue()


@pytest.mark.asyncio
async def test_job_failure_is_rendered_as_terminal_event_before_detach(monkeypatch, tmp_path: Path):
    holder = {}

    def factory(*args, **kwargs):
        holder["app"] = AutoDetachApp(*args, **kwargs)
        return holder["app"]

    async def fake_job(options, *, observer=None, control=None):
        return err(make_loom_error("PROPOSAL_FAILED", "proposal failed", retryable=False))

    monkeypatch.setattr("loom.optimize.tui_runner.run_optimize", fake_job)

    result = await run_optimize_with_tui(_options(tmp_path), app_factory=factory)

    assert not result.ok
    assert holder["app"].collector.state.terminal_status == "failed"
