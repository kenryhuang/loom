"""Evolution and mutation support for Loom."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from loom.core.models import Context, ContextPatch, MinimalLoopDefinition, Trace, err, make_loom_error, ok
from loom.runtime.engine import run


@dataclass(frozen=True, slots=True)
class MutationPolicy:
    require_evidence_trace: bool = True
    allow_code_replacement: bool = False
    allow_structure_change: bool = False
    max_risk: str = "low"


@dataclass(frozen=True, slots=True)
class ContextMutation:
    patch: ContextPatch
    expected_impact: dict[str, Any] | None = None
    level: int = 1
    kind: str = "context"


@dataclass(frozen=True, slots=True)
class LoopMutation:
    step_ref: str | None = None
    done_ref: str | None = None
    identity: Any = None
    goal: Any = None
    level: int = 2
    kind: str = "loop"


@dataclass(frozen=True, slots=True)
class StructureMutation:
    operation: str
    loop_ref: str | None = None
    insert_after: str | None = None
    level: int = 3
    kind: str = "structure"


@dataclass(frozen=True, slots=True)
class MutationBundle:
    base_version: str
    mutations: tuple[Any, ...]
    created_from_trace_ids: tuple[str, ...]
    policy: MutationPolicy = MutationPolicy()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mutations", tuple(self.mutations))
        object.__setattr__(self, "created_from_trace_ids", tuple(self.created_from_trace_ids))


def validate_mutation_bundle_shape(bundle: MutationBundle):
    if not bundle.base_version:
        return err(make_loom_error("VALIDATION_FAILED", "base_version is required", retryable=False))
    if not bundle.mutations:
        return err(make_loom_error("VALIDATION_FAILED", "mutations are required", retryable=False))
    if bundle.policy.require_evidence_trace and not bundle.created_from_trace_ids:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "created_from_trace_ids are required",
                retryable=False,
            )
        )
    return ok(None)


@dataclass(frozen=True, slots=True)
class EvolutionDecision:
    level: int
    proposed_mutation_kinds: tuple[str, ...]
    reason: str


def decide_evolution(traces: tuple[Trace, ...]):
    gaps = [trace.metadata.get("gap") for trace in traces if trace.metadata and trace.metadata.get("gap")]
    surprises = [trace.metadata.get("surprise") for trace in traces if trace.metadata and trace.metadata.get("surprise")]
    if len(gaps) >= 2:
        return EvolutionDecision(1, ("context",), f"Repeated gap: {gaps[0]}")
    if len(surprises) >= 2:
        return EvolutionDecision(2, ("loop",), f"Repeated surprise: {surprises[0]}")
    if any(trace.outcome == "timeout" for trace in traces):
        return EvolutionDecision(3, ("structure",), "Timeout signals")
    return EvolutionDecision(1, ("context",), "Weak signal context refinement")


class InMemoryLoopRegistry:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._active: dict[str, str] = {}

    def register(self, handle) -> None:
        self._records.setdefault(handle.id, {})[handle.version] = handle
        self._active.setdefault(handle.id, handle.version)

    def active(self, loop_id: str):
        version = self._active.get(loop_id)
        if version is None:
            return err(make_loom_error("VALIDATION_FAILED", "Loop not found", retryable=False))
        return ok(self._records[loop_id][version])

    def begin_mutation(self, loop_id: str, base_version: str):
        active = self._active.get(loop_id)
        if active != base_version:
            return err(make_loom_error("MUTATION_REJECTED", "Version mismatch", retryable=False))
        return ok(MutationTransaction(self, loop_id, base_version))


class MutationTransaction:
    def __init__(self, registry: InMemoryLoopRegistry, loop_id: str, base_version: str):
        self.registry = registry
        self.loop_id = loop_id
        self.base_version = base_version
        self.candidate = None

    def apply(self, handle):
        version = _next_version(handle.version)
        definition = replace(handle.definition, version=version)
        candidate = replace(handle, version=version, definition=definition)
        self.registry._records.setdefault(handle.id, {})[version] = candidate
        self.candidate = candidate
        return ok(candidate)

    def commit(self) -> None:
        if self.candidate is not None:
            self.registry._active[self.loop_id] = self.candidate.version

    def rollback(self) -> None:
        if self.candidate is not None:
            self.registry._records.get(self.loop_id, {}).pop(self.candidate.version, None)
            self.candidate = None


@dataclass(frozen=True, slots=True)
class ScoreSummary:
    score: float
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EvolutionEvaluation:
    accepted: bool
    reason: str


class DefaultEvolutionEvaluator:
    def __init__(self, min_delta: float = 0.01):
        self.min_delta = min_delta

    def score_run(self, traces: tuple[Trace, ...]) -> ScoreSummary:
        passes = sum(1 for trace in traces if trace.outcome == "pass")
        failures = sum(1 for trace in traces if trace.outcome == "fail")
        timeouts = sum(1 for trace in traces if trace.outcome == "timeout")
        gaps = sum(1 for trace in traces if trace.metadata and trace.metadata.get("gap"))
        score = passes - failures * 2 - timeouts * 2 - gaps * 0.5
        return ScoreSummary(score, {"runs": len(traces), "passes": passes, "failures": failures})

    def compare(self, before: ScoreSummary, after: ScoreSummary, _bundle: MutationBundle):
        delta = after.score - before.score
        return EvolutionEvaluation(delta >= self.min_delta, f"delta={delta}")


class InMemoryImplementationRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def register(self, ref: str, implementation: Any) -> None:
        self._items[ref] = implementation

    def get(self, ref: str):
        value = self._items.get(ref)
        if value is None:
            return err(make_loom_error("MUTATION_REJECTED", "Implementation not found", retryable=False))
        return ok(value)


def apply_loop_mutation(
    definition: MinimalLoopDefinition,
    mutation: LoopMutation,
    implementations: InMemoryImplementationRegistry,
):
    step = definition.step
    done = definition.done
    if mutation.step_ref is not None:
        result = implementations.get(mutation.step_ref)
        if not result.ok:
            return result
        step = result.value
    if mutation.done_ref is not None:
        result = implementations.get(mutation.done_ref)
        if not result.ok:
            return result
        done = result.value
    return ok(
        replace(
            definition,
            version=_next_version(definition.version),
            step=step,
            done=done,
            identity=mutation.identity or definition.identity,
            goal=mutation.goal or definition.goal,
        )
    )


@dataclass(frozen=True, slots=True)
class CompositionNode:
    id: str
    loop_id: str


@dataclass(frozen=True, slots=True)
class CompositionEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True, slots=True)
class CompositionGraph:
    version: str
    nodes: tuple[CompositionNode, ...]
    edges: tuple[CompositionEdge, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))


def validate_composition_graph(graph: CompositionGraph):
    node_ids = {node.id for node in graph.nodes}
    for edge in graph.edges:
        if edge.source not in node_ids or edge.target not in node_ids:
            return err(make_loom_error("VALIDATION_FAILED", "Dangling graph edge", retryable=False))
    if _has_cycle(graph):
        return err(make_loom_error("VALIDATION_FAILED", "Composition graph has a cycle", retryable=False))
    return ok(None)


def apply_structure_mutation(graph: CompositionGraph, mutation: StructureMutation):
    if mutation.operation != "insert-loop" or not mutation.loop_ref or not mutation.insert_after:
        return err(make_loom_error("MUTATION_REJECTED", "Unsupported structure mutation", retryable=False))
    if mutation.insert_after not in {node.id for node in graph.nodes}:
        return err(make_loom_error("MUTATION_REJECTED", "insert_after node not found", retryable=False))
    node = CompositionNode(mutation.loop_ref, mutation.loop_ref)
    return ok(
        CompositionGraph(
            version=_next_version(graph.version),
            nodes=(*graph.nodes, node),
            edges=(*graph.edges, CompositionEdge(mutation.insert_after, node.id, "chain")),
        )
    )


@dataclass(frozen=True, slots=True)
class ShadowSummary:
    traces: tuple[Trace, ...]
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ShadowEvaluationResult:
    before: ShadowSummary
    after: ShadowSummary
    evaluation: EvolutionEvaluation


async def run_shadow_evaluation(before_handle, after_handle, contexts: tuple[Context, ...]):
    before_traces = []
    after_traces = []
    for context in contexts:
        before_result = await run(before_handle, context)
        after_result = await run(after_handle, context)
        if before_result.ok:
            before_traces.extend(before_result.value.traces)
        if after_result.ok:
            after_traces.extend(after_result.value.traces)
    evaluator = DefaultEvolutionEvaluator(min_delta=0)
    before = evaluator.score_run(tuple(before_traces))
    after = evaluator.score_run(tuple(after_traces))
    return ShadowEvaluationResult(
        ShadowSummary(tuple(before_traces), {"runs": len(contexts), **before.metrics}),
        ShadowSummary(tuple(after_traces), {"runs": len(contexts), **after.metrics}),
        evaluator.compare(before, after, MutationBundle("v1", (ContextMutation(ContextPatch("ctx", (), "shadow")),), ("shadow",))),
    )


def _next_version(version: str) -> str:
    if version.startswith("v") and version[1:].isdigit():
        return f"v{int(version[1:]) + 1}"
    return f"{version}.1"


def _has_cycle(graph: CompositionGraph) -> bool:
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.kind == "chain":
            adjacency.setdefault(edge.source, []).append(edge.target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for child in adjacency.get(node, []):
            if visit(child):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node.id) for node in graph.nodes)


__all__ = [
    "CompositionEdge",
    "CompositionGraph",
    "CompositionNode",
    "ContextMutation",
    "DefaultEvolutionEvaluator",
    "EvolutionDecision",
    "EvolutionEvaluation",
    "InMemoryImplementationRegistry",
    "InMemoryLoopRegistry",
    "LoopMutation",
    "MutationBundle",
    "MutationPolicy",
    "MutationTransaction",
    "ScoreSummary",
    "ShadowEvaluationResult",
    "ShadowSummary",
    "StructureMutation",
    "apply_loop_mutation",
    "apply_structure_mutation",
    "decide_evolution",
    "run_shadow_evaluation",
    "validate_composition_graph",
    "validate_mutation_bundle_shape",
]
