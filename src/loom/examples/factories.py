"""Example loop factories for Loom."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from loom.composition.operators import chain, fork, nest
from loom.core.models import (
    Action,
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    KnowledgeItem,
    MinimalLoopDefinition,
    Observation,
    ResourceRef,
    StateLayer,
    ToolRef,
    Trace,
    as_step_number,
    emit_child_output,
    empty_affordances,
    empty_knowledge,
    empty_state,
    freeze_context,
    merge_child_output,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    now_iso,
    ok,
    project,
)
from loom.evolution.mutations import (
    CompositionEdge,
    CompositionGraph,
    CompositionNode,
    StructureMutation,
    apply_structure_mutation,
)
from loom.llm.api import create_env_openai_provider, create_llm_step_function, create_openai_provider
from loom.observability.traces import JsonlTraceStore, archive_run
from loom.runtime.engine import create, create_runtime_registry, run
from loom.runtime.engine import step as runtime_step


def make_initial_counter_context(max_steps: int = 1) -> Context:
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(
                role="minimal counter",
                capabilities=(ToolRef("count", "Append counter observations"),),
            ),
            goal=GoalLayer(
                objective="Count until the step budget is reached",
                budget={"max_steps": max_steps},
            ),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(),
        )
    )


def make_minimal_counter_loop() -> MinimalLoopDefinition:
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        counter = len(context.state.observations) + 1
        at = now_iso()
        action = Action(
            f"record-counter-{counter}",
            "custom",
            "Record counter observation",
            input={"counter": counter},
        )
        observation = Observation(
            f"counter-{counter}",
            "minimal-counter",
            {"counter": counter},
            at,
        )
        decision = Decision(
            f"decision-{counter}",
            action,
            f"Counter advanced to {counter}",
            (Action("no-op", "none", "Stop without recording"),),
            1,
            at,
        )
        next_context = freeze_context(
            replace(
                context,
                id=new_context_id(),
                state=StateLayer(
                    observations=(*context.state.observations, observation),
                    decisions=(*context.state.decisions, decision),
                    pending=context.state.pending,
                ),
            )
        )
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=at,
            ended_at=now_iso(),
            duration_ms=0,
            input_context_id=context.id,
            output_context_id=next_context.id,
            outcome="pass",
            observations=(observation,),
            decisions=(decision,),
            actions=(action,),
            tags=("example", "minimal-counter"),
        )
        from loom.core.models import StepResult

        return ok(StepResult(next_context, trace, observation, {"counter": counter}))

    def done_fn(context: Context, _runtime: Any):
        max_steps = context.goal.budget.max_steps
        return ok(max_steps is not None and len(context.state.observations) >= max_steps)

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="minimal counter loop"),
        goal=GoalLayer(objective="Append counter observations"),
        step=step_fn,
        done=done_fn,
    )


def make_initial_llm_context() -> Context:
    search_notes_tool = ToolRef(
        "search-notes",
        "Search local project notes for relevant Loom context",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="Loom planning agent"),
            goal=GoalLayer(
                objective="Decide the next useful action for the Loom project",
                budget={"max_steps": 1, "max_tokens": 4000},
            ),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(tools=(search_notes_tool,)),
        )
    )


def make_llm_loop_definition(options: dict[str, Any], *, provider: Any | None = None) -> MinimalLoopDefinition:
    if provider is None:
        provider_result = _create_llm_provider(options)
        if not provider_result.ok:
            raise ValueError(provider_result.error.message)
        provider = provider_result.value
    return MinimalLoopDefinition(
        id=new_loop_id(),
        version=new_loop_version(),
        identity=IdentityLayer(role="LLM loop"),
        goal=GoalLayer(objective="Run an LLM-backed Loom step"),
        step=create_llm_step_function(provider, enable_tool_calling=True, max_tool_calls_per_step=3),
        done=lambda context, _runtime: ok(len(context.state.decisions) > 0),
    )


async def run_llm_loop(options: dict[str, Any]):
    provider = _create_llm_provider(options)
    if not provider.ok:
        return provider

    async def search_notes(input_value, _options=None):
        return ok(
            Observation(
                "search-notes-observation",
                "search-notes",
                {
                    "input": input_value,
                    "matches": [
                        {
                            "title": "Loom architecture",
                            "summary": "Use context layers, Result values, and append-only state.",
                        }
                    ],
                },
                now_iso(),
            )
        )

    handle = create(
        make_llm_loop_definition(options, provider=provider.value),
        registry=create_runtime_registry(tools={"search-notes": search_notes}),
    )
    if not handle.ok:
        return handle
    return await run(handle.value, make_initial_llm_context(), max_steps=1)


def _create_llm_provider(options: dict[str, Any]):
    if options.get("llm_provider") is not None:
        return ok(options["llm_provider"])
    if options.get("api_key"):
        return ok(
            create_openai_provider(
                api_key=options["api_key"],
                model=options.get("model", "gpt-4o-mini"),
                base_url=options.get("base_url", "https://api.openai.com/v1"),
                temperature=options.get("temperature"),
                max_tokens=options.get("max_tokens"),
                http_client=options.get("http_client"),
            )
        )
    return create_env_openai_provider(
        env_path=options.get("env_path"),
        env=options.get("env"),
        model=options.get("model"),
        base_url=options.get("base_url"),
        temperature=options.get("temperature"),
        max_tokens=options.get("max_tokens"),
        http_client=options.get("http_client"),
    )


def read_openai_api_key_from_env() -> str:
    return os.environ.get("OPENAI_API_KEY", "")


@dataclass(frozen=True, slots=True)
class ContextBoundaryExampleResult:
    parent: IdentityLayer
    parent_context: Context
    child: Context
    merged_context: Context


def run_context_boundary_example():
    fact = KnowledgeItem("fact-1", "fact", "Parent fact for child.", 0.9, now_iso())
    parent = freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="parent"),
            goal=GoalLayer(objective="Parent goal"),
            state=StateLayer(observations=(Observation("parent-obs", "parent", {"ready": True}, now_iso()),)),
            knowledge=empty_knowledge(facts=(fact,)),
            affordances=empty_affordances(
                tools=(ToolRef("search", "Search notes"), ToolRef("write", "Write notes")),
                resources=(ResourceRef("notes", "file", "notes.md", "read"),),
            ),
        )
    )
    child = project(
        parent,
        GoalLayer(objective="Child goal"),
        identity=IdentityLayer(role="child"),
        tool_ids=("search",),
        resource_ids=("notes",),
    ).unwrap()
    child_fact = KnowledgeItem("child-fact", "fact", "Child learned this.", 0.7, now_iso())
    child_context = freeze_context(
        replace(
            child,
            state=StateLayer(observations=(Observation("child-obs", "child", {"found": True}, now_iso()),)),
            knowledge=replace(child.knowledge, facts=(*child.knowledge.facts, child_fact)),
        )
    )
    output = emit_child_output(child_context, status="completed")
    merged = merge_child_output(parent, output, accept_knowledge_ids=("child-fact",))
    if not merged.ok:
        return merged
    return ok(ContextBoundaryExampleResult(parent.identity, parent, child, merged.value))


def _example_append_loop(source: str) -> MinimalLoopDefinition:
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        observation = Observation(
            f"{source}-{len(context.state.observations) + 1}",
            source,
            {"source": source},
            now_iso(),
        )
        next_context = replace(
            context,
            id=new_context_id(),
            state=StateLayer(observations=(*context.state.observations, observation)),
        )
        trace_id = new_trace_id()
        from loom.core.models import StepResult

        return ok(
            StepResult(
                next_context,
                Trace(
                    id=trace_id,
                    run_id=context.run_id,
                    loop_id=loop_id,
                    loop_version=version,
                    step_number=as_step_number(len(context.state.observations)),
                    root_trace_id=trace_id,
                    started_at=now_iso(),
                    ended_at=now_iso(),
                    duration_ms=1,
                    input_context_id=context.id,
                    output_context_id=next_context.id,
                    outcome="pass",
                    observations=(observation,),
                ),
            )
        )

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role=f"{source} loop"),
        goal=GoalLayer(objective=f"Append {source}"),
        step=step_fn,
        done=lambda _context, _runtime: ok(False),
    )


def _composition_context() -> Context:
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="composition example"),
            goal=GoalLayer(objective="Run composition example"),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(),
        )
    )


async def make_chain_pipeline_example():
    first = create(_example_append_loop("retrieve")).unwrap()
    second = create(_example_append_loop("summarize")).unwrap()
    handle = chain((first, second)).unwrap()
    return await runtime_step(handle, _composition_context())


async def make_nested_tool_example():
    parent = create(_example_append_loop("parent")).unwrap()
    child = create(_example_append_loop("child")).unwrap()
    handle = nest(parent, child).unwrap()
    return await runtime_step(handle, _composition_context())


async def make_fork_reviewers_example():
    worker = create(_example_append_loop("reviewer")).unwrap()

    async def split(_context):
        return ("a.py", "b.py", "c.py")

    handle = fork(worker, split=split, concurrency=2).unwrap()
    return await runtime_step(handle, _composition_context())


async def make_level1_evolution_example():
    context = make_initial_counter_context(1)
    heuristic = KnowledgeItem(
        "heuristic.permission-ownership-first",
        "heuristic",
        "Check permission ownership before changing files.",
        0.9,
        now_iso(),
    )
    evolved = replace(
        context,
        id=new_context_id(),
        knowledge=replace(context.knowledge, heuristics=(*context.knowledge.heuristics, heuristic)),
    )
    return ok(evolved)


async def make_structure_evolution_example():
    loop_id = new_loop_id()
    graph = CompositionGraph(
        version="v1",
        nodes=(CompositionNode("plan", loop_id), CompositionNode("execute", loop_id)),
        edges=(CompositionEdge("plan", "execute", "chain"),),
    )
    return apply_structure_mutation(
        graph,
        StructureMutation(operation="insert-loop", loop_ref="validate", insert_after="execute"),
    )


async def run_trace_query_example(base_path):
    store = JsonlTraceStore(base_path / "trace-query.jsonl")
    context = _composition_context()
    trace_id = new_trace_id()
    trace = Trace(
        id=trace_id,
        run_id=context.run_id,
        loop_id=new_loop_id(),
        loop_version=new_loop_version(),
        step_number=as_step_number(0),
        root_trace_id=trace_id,
        started_at=now_iso(),
        ended_at=now_iso(),
        duration_ms=1,
        input_context_id=context.id,
        output_context_id=context.id,
        outcome="pass",
        tags=("example", "trace-query"),
    )
    await store.append(trace)
    from loom.observability.traces import DefaultTraceReader

    reader = DefaultTraceReader(store)
    summary = await reader.summarize({"run_id": context.run_id})
    manifest = await archive_run(context.run_id, store, base_path / "archive")
    return ok({"summary": summary, "manifest": manifest})


__all__ = [
    "make_initial_counter_context",
    "make_initial_llm_context",
    "make_chain_pipeline_example",
    "make_fork_reviewers_example",
    "make_level1_evolution_example",
    "make_llm_loop_definition",
    "make_minimal_counter_loop",
    "make_nested_tool_example",
    "make_structure_evolution_example",
    "read_openai_api_key_from_env",
    "run_context_boundary_example",
    "run_llm_loop",
    "run_trace_query_example",
]
