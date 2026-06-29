from __future__ import annotations

import asyncio

import pytest

from loom.examples.factories import make_initial_counter_context, make_minimal_counter_loop
from loom.runtime.engine import create, create_runtime_registry
from loom.runtime.plugins import RunPluginContext, run_with_plugins
from loom.tui.plugin import TuiPlugin
from loom.tui.tui_collector import TuiEventCollector


class ImmediateApp:
    def __init__(self, collector: TuiEventCollector) -> None:
        self.collector = collector
        self.loop_info: tuple[str, str] | None = None
        self.exited = False
        self.ran = False

    def set_loop_info(self, *, role: str, goal: str) -> None:
        self.loop_info = (role, goal)

    async def run_async(self) -> None:
        self.ran = True

    def exit(self) -> None:
        self.exited = True


class UserQuitApp:
    def __init__(self, collector: TuiEventCollector) -> None:
        self.collector = collector
        self.loop_info: tuple[str, str] | None = None
        self.exited = False
        self.ran = False
        self._quit = asyncio.Event()

    def set_loop_info(self, *, role: str, goal: str) -> None:
        self.loop_info = (role, goal)

    async def run_async(self) -> None:
        self.ran = True
        await self._quit.wait()

    def exit(self) -> None:
        self.exited = True
        self._quit.set()

    def user_quit(self) -> None:
        self._quit.set()


@pytest.mark.asyncio
async def test_tui_plugin_exposes_collector_sink_and_sends_done_sentinel() -> None:
    app_holder: list[ImmediateApp] = []

    def app_factory(collector: TuiEventCollector) -> ImmediateApp:
        app = ImmediateApp(collector)
        app_holder.append(app)
        return app

    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    plugin = TuiPlugin(app_factory=app_factory, auto_exit_timeout_seconds=0.01)
    result = await run_with_plugins(
        handle_result.value,
        make_initial_counter_context(max_steps=1),
        plugins=(plugin,),
        max_steps=1,
    )

    assert result.ok
    assert plugin.collector is not None
    assert plugin.collector.event_count >= 1
    assert app_holder[0].loop_info == (loop_def.identity.role, loop_def.goal.objective)
    assert app_holder[0].ran is True
    queued = list(plugin.collector.queue._queue)
    assert queued[-1].event_type == "_tui_done"


@pytest.mark.asyncio
async def test_tui_plugin_keeps_tui_open_until_user_quits_after_run_done() -> None:
    app_holder: list[UserQuitApp] = []

    def app_factory(collector: TuiEventCollector) -> UserQuitApp:
        app = UserQuitApp(collector)
        app_holder.append(app)
        return app

    loop_def = make_minimal_counter_loop()
    handle_result = create(loop_def, registry=create_runtime_registry())
    assert handle_result.ok

    plugin = TuiPlugin(app_factory=app_factory, auto_exit_timeout_seconds=0.01)
    started = await plugin.start(
        RunPluginContext(
            loop=handle_result.value,
            initial_context=make_initial_counter_context(max_steps=1),
            metadata={},
        )
    )
    assert started.ok

    stop_task = asyncio.create_task(plugin.stop(None))
    await asyncio.sleep(0.05)

    assert stop_task.done() is False
    assert app_holder[0].exited is False
    assert plugin.collector is not None
    queued = list(plugin.collector.queue._queue)
    assert queued[-1].event_type == "_tui_done"

    app_holder[0].user_quit()
    stopped = await asyncio.wait_for(stop_task, timeout=0.5)
    assert stopped.ok
