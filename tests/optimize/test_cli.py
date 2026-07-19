from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.core import ok
from loom.optimize.cli import OptimizeCliOptions, main, parse_args
from loom.optimize.contracts import OptimizationResult
from loom.optimize.ui import JsonObserver, TextObserver


def test_parse_primary_optimize_command():
    args = parse_args(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--tui"])

    assert args.command == "run"
    assert args.tui is True
    assert args.json is False
    assert args.tasks == Path("tasks.jsonl")


def test_tui_and_json_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--tui", "--json"])


def test_automatic_and_explicit_task_sets_are_mutually_exclusive_and_complete():
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--trace",
                "run.jsonl",
                "--tasks",
                "tasks.jsonl",
                "--discovery-tasks",
                "d.jsonl",
                "--validation-tasks",
                "v.jsonl",
                "--holdout-tasks",
                "h.jsonl",
                "--config",
                "config.yaml",
            ]
        )
    with pytest.raises(SystemExit):
        parse_args(["--trace", "run.jsonl", "--discovery-tasks", "d.jsonl", "--config", "config.yaml"])


def test_status_approve_and_pause_supporting_commands_parse():
    status = parse_args(["status", "opt_123", "--json"])
    approve = parse_args(["approve", "opt_123", "--candidate", "cand_123", "--identity", "approver"])
    pause = parse_args(["pause", "opt_123"])

    assert status.command == "status" and status.optimization_id == "opt_123"
    assert approve.command == "approve" and approve.candidate_id == "cand_123"
    assert pause.command == "pause"


def test_observers_render_the_same_committed_event(tmp_path: Path):
    event = {"schema_version": "loom.optimization.event.v1", "stage": "search_running", "status": "completed"}
    text_path = tmp_path / "text.log"
    json_path = tmp_path / "json.log"
    with text_path.open("w", encoding="utf-8") as text_stream, json_path.open("w", encoding="utf-8") as json_stream:
        TextObserver(text_stream).emit(event)
        JsonObserver(json_stream).emit(event)

    assert "search_running" in text_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8")) == event


def test_run_main_maps_terminal_dispositions_to_exit_codes(tmp_path: Path, monkeypatch, capsys):
    report = tmp_path / "report.md"
    report.write_text("report", encoding="utf-8")

    async def fake_run(options: OptimizeCliOptions):
        assert options.command == "run"
        return ok(
            OptimizationResult(
                "loom.optimization.result.v1",
                "opt_test",
                "cmp_test",
                "promoted",
                "cand_test",
                None,
                None,
                report,
            )
        )

    monkeypatch.setattr("loom.optimize.cli.run_optimize", fake_run)

    promoted = main(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--json"])

    assert promoted == 0
    assert json.loads(capsys.readouterr().out)["disposition"] == "promoted"


def test_run_main_routes_tui_and_maps_paused_to_exit_three(tmp_path: Path, monkeypatch, capsys):
    report = tmp_path / "paused.md"
    report.write_text("paused", encoding="utf-8")
    calls = []

    async def fake_tui(options: OptimizeCliOptions):
        calls.append(options.tui)
        return ok(
            OptimizationResult(
                "loom.optimization.result.v1",
                "opt_test",
                "cmp_test",
                "paused",
                None,
                None,
                None,
                report,
            )
        )

    monkeypatch.setattr("loom.optimize.tui_runner.run_optimize_with_tui", fake_tui)

    code = main(["--trace", "run.jsonl", "--tasks", "tasks.jsonl", "--config", "config.yaml", "--tui"])

    assert code == 3
    assert calls == [True]
    assert "disposition: paused" in capsys.readouterr().out


def test_top_level_cli_dispatches_optimize(monkeypatch):
    monkeypatch.setattr("loom.optimize.cli.main", lambda argv: 23)
    from loom.cli import main as loom_main

    assert loom_main(["optimize", "status", "opt_123"]) == 23
