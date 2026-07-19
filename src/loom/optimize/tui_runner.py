"""Lifecycle owner for the full-screen Optimize Operations Dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO

from loom.campaigns.serialization import utc_now
from loom.core import Result, err, make_loom_error
from loom.optimize.cli import OptimizeCliOptions, run_approve, run_optimize
from loom.optimize.control import OptimizeRunControl
from loom.optimize.tui_state import OptimizeTuiCollector
from loom.optimize.ui import OptimizeTuiObserver, TextObserver


@dataclass(slots=True)
class _SwitchingObserver:
    tui_observer: OptimizeTuiObserver
    fallback: TextObserver
    detached: bool = False

    async def emit(self, event) -> Any:
        target = self.fallback if self.detached else self.tui_observer
        value = target.emit(event)
        return await value if inspect.isawaitable(value) else value

    def detach(self) -> None:
        self.detached = True


async def run_optimize_with_tui(
    options: OptimizeCliOptions,
    *,
    app_factory: Callable[..., Any] | None = None,
    fallback_stream: TextIO = sys.stderr,
    startup_timeout_seconds: float = 10.0,
) -> Result:
    """Start Textual first, then run optimization without tying job life to UI life."""

    collector = OptimizeTuiCollector()
    control = OptimizeRunControl()
    observer = _SwitchingObserver(OptimizeTuiObserver(collector), TextObserver(fallback_stream))
    result_holder: dict[str, Result] = {}

    async def approve(identity: str, reason: str | None) -> Result:
        current = result_holder.get("result")
        value = None if current is None or not current.ok else current.value
        optimization_id = _field(value, "optimization_id")
        candidate_id = _field(value, "selected_candidate_id") or _field(value, "candidate_id")
        if not optimization_id or not candidate_id:
            return _tui_error("APPROVAL_CONTEXT_MISSING", "The active TUI result is not awaiting candidate approval")
        approval_options = OptimizeCliOptions(
            "approve",
            output_dir=options.output_dir,
            optimization_id=str(optimization_id),
            candidate_id=str(candidate_id),
            identity=identity,
            reason=reason,
        )
        approved = await run_approve(approval_options)
        if approved.ok:
            await _emit_terminal(observer, collector, approved)
        return approved

    if app_factory is None:
        try:
            from loom.optimize.tui_app import OptimizeTuiApp
        except Exception as exc:
            return _tui_error("TUI_UNAVAILABLE", "Optimize TUI dependencies could not be imported", cause=exc)
        app_factory = OptimizeTuiApp
    try:
        app = app_factory(collector, control, approval_handler=approve)
        app_task = asyncio.create_task(app.run_async(), name="loom-optimize-tui")
        started_task = asyncio.create_task(app.wait_started(), name="loom-optimize-tui-startup")
    except Exception as exc:
        return _tui_error("TUI_UNAVAILABLE", "Optimize TUI could not be constructed", cause=exc)

    done, _pending = await asyncio.wait(
        {app_task, started_task},
        timeout=max(0.001, startup_timeout_seconds),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if not done:
        app_task.cancel()
        started_task.cancel()
        await asyncio.gather(app_task, started_task, return_exceptions=True)
        return _tui_error("TUI_UNAVAILABLE", "Optimize TUI did not complete startup before the timeout")
    if started_task not in done and not started_task.done():
        app_error = _task_exception(app_task)
        started_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await started_task
        return _tui_error("TUI_UNAVAILABLE", "Optimize TUI failed before startup completed", cause=app_error)
    with contextlib.suppress(asyncio.CancelledError):
        await started_task

    if app_task.done():
        _task_exception(app_task)
        observer.detach()
    job_task = asyncio.create_task(run_optimize(options, observer=observer, control=control), name="loom-optimize-job")

    if app_task.done():
        result = await job_task
        result_holder["result"] = result
        return result

    done, _pending = await asyncio.wait({app_task, job_task}, return_when=asyncio.FIRST_COMPLETED)
    if app_task in done and job_task not in done:
        _task_exception(app_task)
        observer.detach()
        result = await job_task
        result_holder["result"] = result
        return result

    result = await job_task
    result_holder["result"] = result
    if (collector.state.terminal_status is None and collector.state.lifecycle != "awaiting_approval") or not result.ok:
        await _emit_terminal(observer, collector, result)

    if not app_task.done():
        try:
            await app_task
        except Exception:
            observer.detach()
    return result


async def _emit_terminal(observer: _SwitchingObserver, collector: OptimizeTuiCollector, result: Result) -> None:
    sequence = collector.state.sequence + 1
    if not result.ok:
        event_type = "optimization.failed"
        status = "failed"
        payload = {"error_code": "INTERNAL" if result.error is None else result.error.code}
    else:
        disposition = str(_field(result.value, "disposition") or "completed")
        payload = {"disposition": disposition}
        if disposition == "awaiting_approval":
            event_type = "optimization.governance.awaiting_approval"
            status = disposition
        elif disposition == "paused":
            event_type = "optimization.paused"
            status = disposition
        else:
            event_type = "optimization.completed"
            status = "completed"
    await observer.emit(
        {
            "schema_version": "loom.optimization.event.v1",
            "type": event_type,
            "sequence": sequence,
            "at": utc_now(),
            "optimization_id": str(_field(result.value, "optimization_id") or collector.state.optimization_id or "unknown")
            if result.ok
            else collector.state.optimization_id or "unknown",
            "campaign_id": _field(result.value, "campaign_id") if result.ok else collector.state.campaign_id,
            "stage": collector.state.stage,
            "status": status,
            "scope": {},
            "payload": payload,
        }
    )


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _task_exception(task: asyncio.Task) -> BaseException:
    try:
        exception = task.exception()
    except asyncio.CancelledError as exc:
        return exc
    return exception or RuntimeError("Optimize TUI exited before it mounted")


def _tui_error(code: str, message: str, *, cause: BaseException | None = None) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
        )
    )


__all__ = ["run_optimize_with_tui"]
