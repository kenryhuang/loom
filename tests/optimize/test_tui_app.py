from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, Static

from loom.optimize.control import OptimizeRunControl
from loom.optimize.tui_app import ApprovalInputScreen, CancelConfirmationScreen, OptimizeTuiApp
from loom.optimize.tui_state import OptimizeTuiCollector


def _event(sequence: int, event_type: str, *, stage="search_running", status="event", scope=None, payload=None):
    return {
        "schema_version": "loom.optimization.event.v1",
        "type": event_type,
        "sequence": sequence,
        "at": "2026-07-19T00:00:00.000Z",
        "optimization_id": "opt_test",
        "campaign_id": "cmp_test",
        "stage": stage,
        "status": status,
        "scope": scope or {},
        "payload": payload or {},
    }


@pytest.mark.asyncio
async def test_operations_dashboard_has_stable_layout_and_renders_collector_state():
    collector = OptimizeTuiCollector()
    control = OptimizeRunControl()
    app = OptimizeTuiApp(collector, control)

    async with app.run_test() as pilot:
        for widget_id in (
            "#optimize-header",
            "#stage-pipeline",
            "#candidate-table",
            "#active-trial",
            "#event-feed",
            "#budget-panel",
            "#holdout-governance",
            "#optimize-status",
        ):
            assert len(list(app.query(widget_id))) == 1

        await collector.emit(_event(1, "optimization.stage.started", status="running"))
        await collector.emit(
            _event(
                2,
                "optimization.candidate.admitted",
                scope={"phase": "discovery", "candidate_id": "cand_1"},
                payload={"surface": "system_prompt"},
            )
        )
        await collector.emit(
            _event(
                3,
                "optimization.trial.started",
                status="running",
                scope={
                    "phase": "discovery",
                    "candidate_id": "cand_1",
                    "trial_id": "trial_1",
                    "task_id": "task_1",
                    "side": "candidate",
                    "repetition": 0,
                },
            )
        )
        await collector.emit(
            _event(
                4,
                "optimization.budget.updated",
                payload={"used": {"tokens": 50}, "limits": {"tokens": 100}},
            )
        )
        await pilot.pause(0.15)

        assert app.query_one("#candidate-table", DataTable).row_count == 1
        feed = app.query_one("#event-feed", DataTable)
        assert feed.row_count == 4
        feed.focus()
        feed.move_cursor(row=1)
        app.action_show_detail()
        assert "optimization.candidate.admitted" in str(app.query_one("#event-detail", Static).render())
        assert "task_1" in str(app.query_one("#active-trial", Static).render())
        assert "50 / 100" in str(app.query_one("#budget-panel", Static).render())
        assert "Search" in str(app.query_one("#stage-pipeline", Static).render())


@pytest.mark.asyncio
async def test_dashboard_poll_timer_tolerates_widget_teardown():
    app = OptimizeTuiApp(OptimizeTuiCollector(), OptimizeRunControl())

    async with app.run_test() as pilot:
        await app.query_one("#optimize-header", Static).remove()
        await pilot.pause(0.1)
        assert app.is_running


@pytest.mark.asyncio
async def test_pause_and_confirmed_cancel_use_control_without_auto_exiting():
    collector = OptimizeTuiCollector()
    control = OptimizeRunControl()
    app = OptimizeTuiApp(collector, control)

    async with app.run_test() as pilot:
        await pilot.press("p")
        assert control.pause_requested
        assert not control.cancel_requested

        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CancelConfirmationScreen)
        assert not control.cancel_requested
        await pilot.press("y")
        await pilot.pause()
        assert control.cancel_requested


@pytest.mark.asyncio
async def test_detach_exits_app_without_requesting_pause_or_cancel():
    collector = OptimizeTuiCollector()
    control = OptimizeRunControl()
    app = OptimizeTuiApp(collector, control)

    async with app.run_test() as pilot:
        await pilot.press("q")

    assert not control.pause_requested
    assert not control.cancel_requested


@pytest.mark.asyncio
async def test_terminal_event_updates_status_and_does_not_auto_exit():
    collector = OptimizeTuiCollector()
    app = OptimizeTuiApp(collector, OptimizeRunControl())

    async with app.run_test() as pilot:
        await collector.emit(
            _event(
                1,
                "optimization.completed",
                stage="report_complete",
                status="completed",
                payload={"disposition": "completed"},
            )
        )
        await pilot.pause(0.15)

        assert app.is_running
        assert "completed" in str(app.query_one("#optimize-status", Static).render()).lower()


@pytest.mark.asyncio
async def test_approval_binding_is_enabled_only_when_awaiting_approval():
    approvals = []

    async def approve(identity, reason):
        approvals.append((identity, reason))

    collector = OptimizeTuiCollector()
    app = OptimizeTuiApp(collector, OptimizeRunControl(), approval_handler=approve)

    async with app.run_test() as pilot:
        await pilot.press("a")
        assert approvals == []
        await collector.emit(
            _event(
                1,
                "optimization.governance.awaiting_approval",
                stage="governance_complete",
                status="awaiting_approval",
            )
        )
        await pilot.pause(0.15)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ApprovalInputScreen)
        app.screen.query_one("#approval-identity", Input).value = "reviewer@example.com"
        app.screen.query_one("#approval-reason", Input).value = "Evidence reviewed"
        await pilot.click("#submit-approval")
        await pilot.pause(0.1)
        assert approvals == [("reviewer@example.com", "Evidence reviewed")]
