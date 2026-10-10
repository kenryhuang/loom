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
    # Consume the initial input before assembling capabilities and workflow.
    # A completed run's follow-up is a new goal, not more text on its old goal.
    fresh_turn = not bridge.checkpoint and not state["run"]["steps"] and (not state.get("context") or state["run"].get("reset_planning", False))
    inputs = [m for m in state["messages"] if m["role"] == "user" and m["seq"] > state["input_cursor"]] if fresh_turn else []
    if inputs:
        state = {**state, "task": {**state["task"], "objective": "\n\n".join(m["content"] for m in inputs)}}
        bridge.input_cursor = inputs[-1]["seq"]
    bridge.fresh_turn = fresh_turn
    bridge.turn_messages = [m for m in state["messages"] if not inputs or m["seq"] < inputs[0]["seq"]]
    provider = (
        provider_factory(state)
        if provider_factory
        else _create_provider(load_task_config(config_path).unwrap() if config_path else None, model_name=state["task"]["model"]).unwrap()
    )
    task = state["task"]
    request = TaskRequest(
        task["objective"],
        workspace=task["workspace"],
        task_spec=task.get("task_spec"),
        metadata={"knowledge_base_ids": task.get("knowledge_base_ids", []), "knowledge_directory": state.get("knowledge_directory"),
                  "knowledge_config_path": config_path},
    )
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
    verification_provider = None
    path = request.metadata.get("knowledge_config_path")
    if path:
        config = load_task_config(path).unwrap()
        if config.verification_model:
            verification_provider = _create_provider(config, model_name=config.verification_model).unwrap()
    assembly.configure_acceptance(provider, limits=state["task"]["limits"], verification_provider=verification_provider)
    planning = assembly.workflow
    initial = make_task_context(request, planning=planning, assembly=assembly).unwrap()
    previous = decode(state["context"]) if state.get("context") else None
    if bridge.fresh_turn:
        context = replace(initial, knowledge=previous.knowledge) if previous else initial
        archive = assembly.publish_artifact({"messages": bridge.turn_messages}, "session_history") if bridge.turn_messages else None
        context = replace(
            context,
            metadata={
                **(context.metadata or {}),
                "session_history": _history(bridge.turn_messages, state["task"]["limits"]["max_window_chars"]),
                "session_history_artifact": archive,
            },
        )
    else:
        context = previous or initial
    same_run = context.run_id == state["run"]["id"]
    if not bridge.checkpoint and not same_run:
        context = replace(
            context,
            metadata={
                **(context.metadata or {}),
                "session_turn": True,
                "result_scope": {
                    "run_id": state["run"]["id"],
                    "observation_start": len(context.state.observations),
                    "decision_start": len(context.state.decisions),
                },
            },
        )
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
    assembly.update_goal(context.goal)
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


def _history(messages, window_chars):
    """Bound transcript background separately from the current execution state."""
    remaining = min(8000, max(256, window_chars // 3))
    history = []
    for message in reversed(messages[-8:]):
        if message["role"] not in {"user", "assistant"} or remaining <= 0:
            continue
        content = message["content"]
        limit = min(2000, remaining)
        if len(content) > limit:
            marker = "\n[Earlier message shortened]\n"[:limit]
            head = (limit - len(marker)) // 2
            tail = limit - len(marker) - head
            content = content[:head] + marker + (content[-tail:] if tail else "")
        history.append({"role": message["role"], "content": content})
        remaining -= len(content)
    return list(reversed(history))
