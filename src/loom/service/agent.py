"""Assemble existing Loom primitives inside an isolated service worker."""

from dataclasses import replace

from loom.llm.managed_step import ManagedStep
from loom.runtime import PlanningRuntime, create, create_runtime_registry, step
from loom.runtime.checkpoints import decode
from loom.tasks.config import load_task_config
from loom.tasks.request import TaskRequest
from loom.tasks.runner import _create_provider, make_task_context, make_task_loop
from loom.tasks.tools import make_task_tools


async def execute(state, bridge, config_path, provider_factory):
    provider = (
        provider_factory(state)
        if provider_factory
        else _create_provider(load_task_config(config_path).unwrap() if config_path else None, model_name=state["task"]["model"]).unwrap()
    )
    request = TaskRequest(state["task"]["objective"], workspace=state["task"]["workspace"])
    planning = PlanningRuntime(state["task"]["plan_mode"])
    initial = make_task_context(request, planning=planning).unwrap()
    context = decode(state["context"]) if state.get("context") else initial
    context = bridge.checkpoint["context"] if bridge.checkpoint else replace(context, run_id=state["run"]["id"])
    managed = ManagedStep(provider, bridge, planning=planning, limits=state["task"]["limits"], stream=True)
    definition = planning.wrap_loop(replace(make_task_loop(request, provider, planning=planning), step=managed))
    tools = make_task_tools(request)
    shell = tools["shell_execute"]

    async def managed_shell(value, options):
        return await shell(
            value,
            {
                **(options or {}),
                "process_started": lambda pid: bridge.rpc("process_started", {"pid": pid}),
                "cancel_check": lambda: bridge.rpc("check_control", {}),
            },
        )

    tools["shell_execute"] = managed_shell
    handle = create(definition, registry=create_runtime_registry(tools=planning.wrap_tools(tools))).unwrap()
    while True:
        result = await step(handle, context, trace_sink=bridge, trace_id=(bridge.checkpoint or {}).get("trace_id"))
        if not result.ok:
            bridge.rpc("failure", {"message": result.error.message, "code": result.error.code})
            return
        control = result.value.control
        directive = bridge.rpc("result", result.value)
        if control.kind != "continue" or not directive["continue"]:
            return
        context = result.value.context
        bridge.counters = {"llm_calls": bridge.checkpoint["llm_calls"], "usage": bridge.checkpoint["usage"]} if bridge.checkpoint else bridge.counters
        bridge.checkpoint = None
        bridge.input_cursor = bridge.rpc("status", {})["input_cursor"]
