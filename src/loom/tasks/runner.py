"""Generic task runner built on Loom context and runtime primitives."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.core import (
    Budget,
    Capability,
    Constraint,
    Context,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    ResourceRef,
    Result,
    StateLayer,
    SuccessCriterion,
    ToolRef,
    empty_affordances,
    empty_knowledge,
    err,
    freeze_context,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    now_iso,
    ok,
    thaw_json,
)
from loom.llm import create_env_openai_provider, create_llm_step_function
from loom.observability import JsonlTraceStore
from loom.runtime import create, create_runtime_registry, run, run_with_plugins
from loom.tasks.config import TaskRunnerConfig, create_provider_from_task_config
from loom.tasks.profiles import TaskProfile, get_task_profile, select_task_profile
from loom.tasks.request import TaskRequest, TaskRunOptions, TaskRunResult
from loom.tasks.tools import make_task_tools


def make_task_context(request: TaskRequest) -> Result:
    validation = _validate_request(request)
    if not validation.ok:
        return validation

    profile_result = _resolve_profile(request)
    if not profile_result.ok:
        return profile_result
    profile = profile_result.value

    workspace = request.workspace.resolve() if request.workspace is not None else None
    constraints = _constraints_for_request(profile, request, workspace)
    criteria = _criteria_for_request(profile, request)
    tools = _task_tool_refs()
    resources = () if workspace is None else (ResourceRef("workspace", "directory", str(workspace), "read-write"),)

    return ok(
        freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(
                    role=profile.role,
                    capabilities=(
                        Capability("workspace_inspection", "Inspect files and command output through registered tools."),
                        Capability("evidence_synthesis", "Synthesize observations into a final task report."),
                    ),
                    constraints=constraints,
                    metadata={"profile": profile.id},
                ),
                goal=GoalLayer(
                    objective=request.objective,
                    criteria=criteria,
                    budget=Budget(),
                    metadata={"blueprint": profile.blueprint, "risk_level": request.risk_level},
                ),
                state=StateLayer(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(tools=tools, resources=resources),
                metadata={
                    "task_kind": "generic_task",
                    "profile": profile.id,
                    "workspace": "" if workspace is None else str(workspace),
                    "blueprint": profile.blueprint,
                    "request": _request_metadata(request, workspace),
                },
            )
        )
    )


def make_task_loop(request: TaskRequest, provider: Any, *, stream: bool = False) -> MinimalLoopDefinition:
    profile = select_task_profile(request)
    return MinimalLoopDefinition(
        id=new_loop_id(),
        version=new_loop_version(),
        identity=IdentityLayer(role=profile.role),
        goal=GoalLayer(objective=request.objective),
        step=create_llm_step_function(provider, stream=stream, max_tool_calls_per_step=None),
        done=lambda context, _runtime: ok(bool(context.state.decisions)),
        metadata={"task_kind": "generic_task", "profile": profile.id, "blueprint": profile.blueprint},
    )


async def run_generic_task(
    request: TaskRequest,
    *,
    provider: Any | None = None,
    options: TaskRunOptions | None = None,
    config: TaskRunnerConfig | None = None,
    model_name: str | None = None,
) -> Result:
    run_options = options or TaskRunOptions()
    if provider is None:
        provider_result = _create_provider(config, model_name=model_name)
        if not provider_result.ok:
            return provider_result
        provider = provider_result.value

    context = make_task_context(request)
    if not context.ok:
        return context

    handle = create(
        make_task_loop(request, provider, stream=run_options.stream),
        registry=create_runtime_registry(tools=make_task_tools(request)),
        trace_store=JsonlTraceStore(run_options.trace_path) if run_options.trace_path is not None else None,
    )
    if not handle.ok:
        return handle

    if run_options.tui:
        from loom.tui import TuiPlugin

        run_result = await run_with_plugins(
            handle.value,
            context.value,
            max_steps=run_options.max_steps,
            timeout_ms=run_options.timeout_ms,
            plugins=(TuiPlugin(),),
        )
    else:
        run_result = await run(
            handle.value,
            context.value,
            max_steps=run_options.max_steps,
            timeout_ms=run_options.timeout_ms,
        )
    if not run_result.ok:
        return run_result
    return ok(TaskRunResult(run_result=run_result.value, output=_report_from_run_result(run_result.value)))


def _validate_request(request: TaskRequest) -> Result:
    if not request.objective.strip():
        return err(make_loom_error("VALIDATION_FAILED", "Task objective is required", retryable=False))
    if request.workspace is not None and not request.workspace.exists():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task workspace does not exist",
                retryable=False,
                metadata={"workspace": str(request.workspace)},
            )
        )
    return ok(None)


def _resolve_profile(request: TaskRequest) -> Result:
    if request.profile == "auto":
        return ok(select_task_profile(request))
    return get_task_profile(request.profile)


def _constraints_for_request(profile: TaskProfile, request: TaskRequest, workspace: Path | None) -> tuple[Constraint, ...]:
    descriptions = [*profile.constraints, *request.constraints]
    if workspace is not None:
        descriptions.insert(0, f"Workspace root is {workspace}. Treat tool paths as relative to this root unless absolute paths are necessary.")
    return tuple(Constraint(f"constraint-{index + 1}", description) for index, description in enumerate(descriptions))


def _criteria_for_request(profile: TaskProfile, request: TaskRequest) -> tuple[SuccessCriterion, ...]:
    outputs = request.expected_outputs or profile.expected_outputs or ("Complete the task and provide a final answer.",)
    return tuple(SuccessCriterion(f"criterion-{index + 1}", description) for index, description in enumerate(outputs))


def _task_tool_refs() -> tuple[ToolRef, ...]:
    return (
        ToolRef(
            "read_file",
            "Read a UTF-8 text file from the workspace. Use this before making claims about source or documentation.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to read before truncating."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        ToolRef(
            "write_file",
            "Write a UTF-8 text file inside the workspace. Use only when the user requested file changes.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                    "content": {"type": "string", "description": "Complete file content to write."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        ToolRef(
            "shell_execute",
            "Execute a command in the workspace without shell expansion and return stdout, stderr, and exit code.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Command string or argv list.",
                    },
                    "cwd": {"type": "string", "description": "Workspace-relative working directory."},
                    "timeout_seconds": {"type": "integer", "description": "Positive command timeout in seconds."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        ToolRef(
            "finish",
            "Finish the task with the final report. For audits, provide markdown with evidence and recommendations.",
            input_schema={
                "type": "object",
                "properties": {
                    "report": {"type": "string", "description": "Final answer or markdown report."},
                    "content": {"type": "string", "description": "Alias for report."},
                },
                "additionalProperties": False,
            },
        ),
    )


def _create_provider(config: TaskRunnerConfig | None, *, model_name: str | None) -> Result:
    if config is not None:
        return create_provider_from_task_config(config, model_name=model_name)
    return create_env_openai_provider(model=model_name)


def _request_metadata(request: TaskRequest, workspace: Path | None) -> Mapping[str, Any]:
    metadata = _json_safe_mapping(request.metadata)
    metadata.update(
        {
            "objective": request.objective,
            "workspace": "" if workspace is None else str(workspace),
            "profile": request.profile,
            "constraints": request.constraints,
            "expected_outputs": request.expected_outputs,
            "risk_level": request.risk_level,
        }
    )
    return metadata


def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_json_safe(item) for item in value)
    return str(value)


def _report_from_run_result(run_result: Any) -> str:
    output = thaw_json(run_result.output)
    report = _report_from_decision_output(output)
    if report:
        return report

    latest = run_result.context.state.decisions[-1] if run_result.context.state.decisions else None
    if latest is not None:
        report = _report_from_decision_output({"action": {"input": thaw_json(latest.action.input)}})
        if report:
            return report

    for observation in reversed(run_result.context.state.observations):
        if observation.source != "finish":
            continue
        value = thaw_json(observation.value)
        if isinstance(value, Mapping) and isinstance(value.get("report"), str):
            return value["report"]
    return "" if output is None else str(output)


def _report_from_decision_output(output: Any) -> str:
    if not isinstance(output, Mapping):
        return ""
    action = output.get("action", {})
    if not isinstance(action, Mapping):
        return ""
    input_value = action.get("input", {})
    if not isinstance(input_value, Mapping):
        return ""
    report = input_value.get("report") or input_value.get("content")
    return report if isinstance(report, str) else ""


__all__ = ["make_task_context", "make_task_loop", "run_generic_task"]
