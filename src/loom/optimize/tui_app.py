"""Textual Operations Dashboard for governed optimization campaigns."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Input, Static

from loom.core import Result
from loom.optimize.control import OptimizeRunControl
from loom.optimize.tui_state import PIPELINE_STAGES, OptimizeDashboardState, OptimizeTuiCollector

_STAGE_LABELS = {
    "preflight_complete": "Preflight",
    "seed_analysis_complete": "Seed",
    "campaign_initialized": "Campaign",
    "search_running": "Search",
    "search_sealed": "Seal",
    "validation_complete": "Validation",
    "holdout_complete": "Holdout",
    "governance_complete": "Governance",
    "report_complete": "Report",
}

_STATUS_GLYPHS = {
    "pending": "○",
    "running": "●",
    "completed": "✓",
    "replayed": "↻",
    "failed": "✗",
    "paused": "Ⅱ",
}


class CancelConfirmationScreen(ModalScreen[bool]):
    """Require an explicit confirmation before requesting durable cancellation."""

    BINDINGS = [("y", "confirm", "Confirm"), ("n", "decline", "Keep running"), ("escape", "decline", "Keep running")]

    CSS = """
    CancelConfirmationScreen { align: center middle; background: $background 70%; }
    #cancel-dialog { width: 64; height: 11; padding: 1 2; border: round $error; background: $panel; }
    #cancel-actions { height: 3; align-horizontal: center; }
    #cancel-actions Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="cancel-dialog"):
            yield Static("Cancel active work at the next durable checkpoint?\nThe optimization remains resumable.")
            with Horizontal(id="cancel-actions"):
                yield Button("Cancel work", id="confirm-cancel", variant="error")
                yield Button("Keep running", id="decline-cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-cancel")


class ApprovalInputScreen(ModalScreen[tuple[str, str] | None]):
    """Collect the actor assertion and rationale required by governance."""

    BINDINGS = [("escape", "decline", "Cancel")]

    CSS = """
    ApprovalInputScreen { align: center middle; background: $background 70%; }
    #approval-dialog { width: 72; height: 16; padding: 1 2; border: round $warning; background: $panel; }
    #approval-dialog Input { margin-bottom: 1; }
    #approval-actions { height: 3; align-horizontal: center; }
    #approval-actions Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static("Approve the stored candidate using an authorized identity")
            yield Input(placeholder="Approver identity", id="approval-identity")
            yield Input(placeholder="Reason (optional)", id="approval-reason")
            with Horizontal(id="approval-actions"):
                yield Button("Submit approval", id="submit-approval", variant="warning")
                yield Button("Cancel", id="cancel-approval")

    def action_decline(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit-approval":
            self.dismiss(None)
            return
        identity = self.query_one("#approval-identity", Input).value.strip()
        if not identity:
            self.query_one("#approval-identity", Input).focus()
            return
        reason = self.query_one("#approval-reason", Input).value.strip()
        self.dismiss((identity, reason))


class OptimizeTuiApp(App[None]):
    """Read-only projection of optimization state plus cooperative controls."""

    CSS = """
    Screen { background: #11131c; color: #c0caf5; }
    #optimize-header { height: 3; padding: 0 1; border-bottom: solid #3b3d57; }
    #stage-pipeline { height: 3; padding: 1; background: #171925; }
    #dashboard-body { height: 1fr; }
    #dashboard-main { width: 3fr; height: 1fr; }
    #dashboard-side { width: 1fr; min-width: 26; height: 1fr; }
    #candidate-table { height: 2fr; border: round #3b3d57; }
    #active-trial { height: 7; padding: 0 1; border: round #7aa2f7; }
    #event-feed { height: 2fr; border: round #3b3d57; }
    #event-detail { height: 1fr; min-height: 5; border: round #565f89; padding: 0 1; overflow-y: auto; }
    #budget-panel { height: 1fr; min-height: 10; padding: 1; border: round #9ece6a; }
    #holdout-governance { height: 1fr; min-height: 10; padding: 1; border: round #e0af68; }
    #optimize-status { height: 1; padding: 0 1; background: #1e1f2e; }
    """

    BINDINGS = [
        ("q", "detach", "Detach"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("up", "cursor_up", "Up"),
        ("enter", "show_detail", "Detail"),
        ("space", "show_detail", "Detail"),
        ("y", "copy_detail", "Copy"),
        ("Y", "copy_all", "Copy all"),
        ("p", "pause", "Pause"),
        ("c", "request_cancel", "Cancel"),
        ("a", "approve", "Approve"),
    ]

    def __init__(
        self,
        collector: OptimizeTuiCollector,
        control: OptimizeRunControl,
        *,
        approval_handler: Callable[[str, str | None], Any] | None = None,
    ) -> None:
        super().__init__()
        self.collector = collector
        self.control = control
        self.approval_handler = approval_handler
        self._started = asyncio.Event()
        self._started_at = time.monotonic()
        self._rendered_sequence = -1
        self._poll_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static("LOOM OPTIMIZE", id="optimize-header")
        yield Static("", id="stage-pipeline")
        with Horizontal(id="dashboard-body"):
            with Vertical(id="dashboard-main"):
                yield DataTable(id="candidate-table", cursor_type="row", zebra_stripes=True)
                yield Static("No active trial", id="active-trial")
                yield DataTable(id="event-feed", cursor_type="row", zebra_stripes=True)
                yield Static("Select an event and press Enter for sanitized detail", id="event-detail", markup=False)
            with Vertical(id="dashboard-side"):
                yield Static("Budget data pending", id="budget-panel")
                yield Static("Holdout sealed\nGovernance pending", id="holdout-governance")
        yield Static("starting", id="optimize-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#candidate-table", DataTable)
        table.add_columns("Candidate", "Surface", "Pairs", "Score", "Delta", "State", "Frontier")
        self.query_one("#event-feed", DataTable).add_columns("#", "Event", "Status", "Scope")
        self._refresh_dashboard(force=True)
        self._started.set()
        self._poll_timer = self.set_interval(0.05, self._poll_updates)

    def on_unmount(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    async def wait_started(self) -> None:
        await self._started.wait()

    def _poll_updates(self) -> None:
        try:
            changed = False
            try:
                while True:
                    self.collector.updates.get_nowait()
                    changed = True
            except asyncio.QueueEmpty:
                pass
            if changed or self.collector.state.sequence != self._rendered_sequence:
                self._refresh_dashboard()
            else:
                self.query_one("#optimize-header", Static).update(_header_text(self.collector.state, time.monotonic() - self._started_at))
        except NoMatches:
            # A timer tick may already be queued while Textual tears down the
            # screen. Stop polling once the dashboard DOM no longer exists.
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None

    def _refresh_dashboard(self, *, force: bool = False) -> None:
        state = self.collector.state
        if not force and state.sequence == self._rendered_sequence:
            return
        self._rendered_sequence = state.sequence
        self.query_one("#optimize-header", Static).update(_header_text(state, time.monotonic() - self._started_at))
        self.query_one("#stage-pipeline", Static).update(_pipeline_text(state))
        self._refresh_candidates(state)
        self.query_one("#active-trial", Static).update(_trial_text(state))
        self.query_one("#budget-panel", Static).update(_budget_text(state))
        self.query_one("#holdout-governance", Static).update(_governance_text(state))
        self.query_one("#optimize-status", Static).update(_status_text(state))
        feed = self.query_one("#event-feed", DataTable)
        selected_row = feed.cursor_row
        feed.clear()
        for event in state.recent_events:
            scope = event.get("scope") if isinstance(event.get("scope"), dict) else {}
            labels = [scope.get(name) for name in ("candidate_id", "trial_id", "side") if scope.get(name)]
            feed.add_row(
                str(event.get("sequence", "?")),
                str(event.get("type", "unknown")),
                str(event.get("status", "event")),
                " / ".join(map(str, labels)) or "—",
                key=str(event.get("sequence", len(state.recent_events))),
            )
        if feed.row_count:
            feed.move_cursor(row=min(selected_row, feed.row_count - 1))

    def _refresh_candidates(self, state: OptimizeDashboardState) -> None:
        table = self.query_one("#candidate-table", DataTable)
        selected_row = table.cursor_row
        table.clear()
        for candidate in state.candidates.values():
            pairs = str(candidate.completed_pairs)
            if candidate.total_pairs is not None:
                pairs = f"{pairs}/{candidate.total_pairs}"
            table.add_row(
                candidate.candidate_id,
                candidate.surface or "—",
                pairs,
                "—" if candidate.score is None else f"{candidate.score:.3f}",
                "—" if candidate.regression is None else f"{candidate.regression:+.3f}",
                "finalist" if candidate.finalist else candidate.status,
                "yes" if candidate.frontier else "—",
                key=candidate.candidate_id,
            )
        if table.row_count:
            table.move_cursor(row=min(selected_row, table.row_count - 1))

    def action_detach(self) -> None:
        self.exit()

    def action_pause(self) -> None:
        self.control.request_pause()
        self.query_one("#optimize-status", Static).update("pause requested — waiting for durable checkpoint")

    def action_request_cancel(self) -> None:
        self.push_screen(CancelConfirmationScreen(), self._on_cancel_confirmation)

    def _on_cancel_confirmation(self, confirmed: bool | None) -> None:
        if confirmed:
            self.control.request_cancel()
            self.query_one("#optimize-status", Static).update("cancel requested — waiting for durable checkpoint")

    def action_approve(self) -> None:
        if self.collector.state.lifecycle != "awaiting_approval" or self.approval_handler is None:
            self.query_one("#optimize-status", Static).update("approval is not currently available")
            return
        self.push_screen(ApprovalInputScreen(), self._on_approval_input)

    def _on_approval_input(self, request: tuple[str, str] | None) -> None:
        if request is not None:
            self.run_worker(self._submit_approval(*request), exclusive=False)

    async def _submit_approval(self, identity: str, reason: str) -> None:
        if self.approval_handler is None:
            return
        result = self.approval_handler(identity, reason or None)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Result) and not result.ok:
            code = "INTERNAL" if result.error is None else result.error.code
            self.query_one("#optimize-status", Static).update(f"approval failed: {code}")
            return
        self.query_one("#optimize-status", Static).update("approval submitted")

    def action_cursor_down(self) -> None:
        self._focused_table().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._focused_table().action_cursor_up()

    def action_show_detail(self) -> None:
        event = self._selected_event()
        if event is None:
            self.query_one("#optimize-status", Static).update("no event selected")
            return
        self.query_one("#event-detail", Static).update(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True))

    def action_copy_detail(self) -> None:
        event = self._selected_event()
        self._copy(json.dumps(event, ensure_ascii=False, sort_keys=True) if event is not None else "")

    def action_copy_all(self) -> None:
        self._copy(json.dumps(self.collector.state.recent_events, ensure_ascii=False, sort_keys=True))

    def _copy(self, value: str) -> None:
        try:
            self.copy_to_clipboard(value)
            status = "copied to clipboard"
        except Exception:
            status = "clipboard unavailable"
        self.query_one("#optimize-status", Static).update(status)

    def _focused_table(self) -> DataTable:
        return self.focused if isinstance(self.focused, DataTable) else self.query_one("#event-feed", DataTable)

    def _selected_event(self) -> dict[str, Any] | None:
        events = self.collector.state.recent_events
        if not events:
            return None
        feed = self.query_one("#event-feed", DataTable)
        index = min(max(0, feed.cursor_row), len(events) - 1)
        return events[index]


def _header_text(state: OptimizeDashboardState, elapsed: float) -> str:
    return (
        f"LOOM OPTIMIZE  {state.optimization_id or 'preparing'}\n"
        f"lifecycle={state.lifecycle}  stage={_STAGE_LABELS.get(state.stage, state.stage)}  elapsed={int(elapsed)}s"
    )


def _pipeline_text(state: OptimizeDashboardState) -> str:
    labels = []
    for stage in PIPELINE_STAGES:
        label = _STAGE_LABELS[stage]
        if stage == "search_running" and state.search_iteration is not None:
            label = f"{label} {state.search_iteration}/{state.search_iterations or '?'}"
        labels.append(f"{_STATUS_GLYPHS.get(state.pipeline.get(stage, 'pending'), '○')} {label}")
    return "  →  ".join(labels)


def _trial_text(state: OptimizeDashboardState) -> str:
    trial = state.active_trial
    if trial is None:
        return "Active trial\nNo active paired trial"
    return (
        f"Active trial  {trial.trial_id}  [{trial.status}]\n"
        f"phase={trial.phase or '—'}  task={trial.task_id or '—'}  repetition={trial.repetition if trial.repetition is not None else '—'}\n"
        f"side={trial.side or '—'}  runtime={trial.runtime_event or 'starting'}  tokens={trial.solver_tokens}"
    )


def _budget_text(state: OptimizeDashboardState) -> str:
    names = ("candidates", "llm_calls", "tokens", "cost", "wall_time_seconds")
    lines = ["Budget"]
    for name in names:
        used = state.budget.used.get(name)
        limit = state.budget.limits.get(name)
        if used is None or limit is None:
            lines.append(f"{name}: unavailable")
            continue
        ratio = _ratio(used, limit)
        marker = "✗ " if ratio is not None and ratio >= 1 else "⚠ " if ratio is not None and ratio >= 0.8 else ""
        lines.append(f"{marker}{name}: {used} / {limit}")
    return "\n".join(lines)


def _governance_text(state: OptimizeDashboardState) -> str:
    gates = ", ".join(f"{gate.get('gate_id', 'gate')}={gate.get('result', 'unknown')}" for gate in state.governance_gates)
    return (
        f"Holdout\n{state.holdout_status}\n\nGovernance\n{state.governance_status}"
        f"\nrisk: {state.governance_risk or 'unavailable'}\ngates: {gates or 'unavailable'}"
    )


def _status_text(state: OptimizeDashboardState) -> str:
    if state.terminal_status:
        details = " • ".join(value for value in (state.report_path, state.next_action) if value)
        return f"{state.terminal_status} — {details or 'press q to detach'}"
    return "running — p pause • c cancel • q detach"


def _ratio(used: Any, limit: Any) -> float | None:
    try:
        denominator = float(limit)
        return None if denominator <= 0 else float(used) / denominator
    except (TypeError, ValueError):
        return None


__all__ = ["ApprovalInputScreen", "CancelConfirmationScreen", "OptimizeTuiApp"]
