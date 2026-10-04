"""Assemble existing Loom primitives inside an isolated service worker."""

from dataclasses import replace

from loom.llm.managed_step import ManagedStep
from loom.runtime import create, create_runtime_registry, step
from loom.runtime.checkpoints import decode
from loom.tasks.assembly import TaskAssembly
from loom.tasks.config import load_task_config
from loom.tasks.request import TaskRequest
from loom.tasks.runner import _create_provider, make_task_context, make_task_loop


async def execute(state, bridge, config_path, provider_factory, plugin_registry_factory=None):
    provider = (
        provider_factory(state)
        if provider_factory
        else _create_provider(load_task_config(config_path).unwrap() if config_path else None, model_name=state["task"]["model"]).unwrap()
    )
    task = state["task"]
    request = TaskRequest(task["objective"], workspace=task["workspace"], task_spec=task.get("task_spec"))
    assembly = TaskAssembly(
        request,
        plan_mode=task["plan_mode"],
        execution=bridge,
        session_id=state["session_id"],
        run_id=state["run"]["id"],
        attempt_id=state["run"]["attempt_id"],
        registry=plugin_registry_factory() if plugin_registry_factory else None,
    )
    try:
        await _execute_assembled(state, bridge, provider, request, assembly)
    finally:
        await assembly.close()


async def _execute_assembled(state, bridge, provider, request, assembly):
    planning = assembly.workflow
    initial = make_task_context(request, planning=planning, assembly=assembly).unwrap()
    context = decode(state["context"]) if state.get("context") else initial
    same_run = context.run_id == state["run"]["id"]
    context = bridge.checkpoint["context"] if bridge.checkpoint else replace(context, run_id=state["run"]["id"])
    if bridge.checkpoint and bridge.checkpoint.get("plugin_states"):
        assembly.restore(bridge.checkpoint["plugin_states"])
        context = planning.project_state(context)
    elif bridge.checkpoint and bridge.checkpoint.get("planning"):
        planning.restore(bridge.checkpoint["planning"]).unwrap()
        context = planning.project_state(context)
    elif same_run and not state["run"].get("reset_planning"):
        snapshot = (context.state.scratch or {}).get("execution_plugins")
        if snapshot:
            assembly.restore(snapshot)
            context = planning.project_state(context)
    if state["run"].get("reset_planning") and not bridge.checkpoint and state["run"]["steps"] == 0:
        context = planning.project_state(context)
    managed = ManagedStep(provider, bridge, planning=planning, limits=state["task"]["limits"], stream=True, assembly=assembly)
    definition = assembly.wrap_loop(replace(make_task_loop(request, provider, planning=planning), step=managed))
    handle = create(definition, registry=create_runtime_registry(tools=assembly.handlers())).unwrap()
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
