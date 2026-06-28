from __future__ import annotations

import pytest

from loom.examples.factories import make_initial_counter_context, make_minimal_counter_loop
from loom.runtime.engine import create, create_runtime_registry
from loom.runtime.plugins import run_with_plugins
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
