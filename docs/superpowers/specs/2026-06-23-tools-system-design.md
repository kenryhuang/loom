# Loom Tools System Design

## Purpose

This document designs a bounded tools system for Loom.

The goal is not to expose more tools to the model. The goal is to expose a
small, stable, traceable, and evolvable set of tools for each loop step.

The design uses dynamic tool composition as the primary capability. Dynamic
tool generation is preserved as a controlled extension, but generated tools do
not enter the long-lived tool catalog without validation and evidence.

## Core Principles

### Layered, Not Piled

Tools are governed by layers, not by a single ever-growing list.

| Layer | Meaning | Target Size | Lifecycle | Composition |
| --- | --- | ---: | --- | --- |
| Atomic | Base actions such as search, read_file, write_file, terminal | 20-30 | Permanent | Can be composed |
| Composed | One-level fixed orchestration of atomic tools | 10-15 | TTL plus decay | Cannot be composed again |
| Ephemeral | Run-local generated micro-tools or temporary plans | 3-5 | Destroyed after one run | Cannot be promoted directly |

The maximum durable tool graph depth is one:

```text
atomic + atomic -> composed
atomic + atomic -> ephemeral
composed + anything -> forbidden
ephemeral + anything -> forbidden as a durable tool
```

Composed tools cannot compose other composed tools. Complex workflows should be
represented as loops or runtime plans, not as nested tools.

Ephemeral tools may exist as run-local plans, but they must expand directly to
atomic tool calls at execution time. They cannot become dependencies of other
tools.

### Tools Must Prove Their Existence

A tool is not just a piece of knowledge. It is a verifiable action boundary.

A tool is worth keeping only when it has:

- a stable input schema
- a stable output schema
- clear failure semantics
- traceable execution
- a specific applicability boundary
- evidence that it improves reliability, cost, context usage, or auditability

Temporary compositions are not automatically promoted. They start as ephemeral
runtime artifacts. Only recurring, stable, useful compositions can become
candidate composed tools.

### Context Budget Is A Hard Constraint

The model should never see the entire tool catalog.

Loom should maintain a global catalog, but each context should only receive a
budgeted affordance view. The system first decides which tools are eligible to
appear in the current context, then the LLM selects the minimal tools for the
current step.

```text
ToolCatalog + ToolEvolutionState
  -> ToolResolver applies budget and policy
  -> AffordanceLayer.tools
  -> ToolSelector chooses step tools
  -> StepRuntime.call_tool executes selected calls
```

### Merge Before Growth

When a tool composition proves useful, the system should not immediately add a
new tool.

It should first ask:

1. Can this behavior become a mode or parameter of an existing atomic tool?
2. Can this replace a low-value composed tool?
3. Does this represent a genuinely new stable action boundary?

Only the third answer justifies adding a new composed tool. The catalog should
converge over time, not grow without bound.

### Trace Data Drives Evolution

Tool composition, promotion, merging, splitting, and pruning should be driven by
trace evidence instead of manual preference.

The existing Trace, Observation, Decision, and evolution infrastructure should
provide the evidence loop:

```text
trace data
  -> pattern detection
  -> candidate composition / merge / prune proposal
  -> shadow evaluation
  -> accept or reject
```

## Architecture

### Existing Loom Concepts

The design should preserve the current Loom boundary model:

- `ToolRef` is a serializable declaration of a tool.
- `AffordanceLayer.tools` is the current context's visible tool view.
- `RuntimeRegistry.tools` binds tool ids to executable implementations.
- `StepRuntime.call_tool` is the execution gateway.
- `Trace`, `Observation`, and `Decision` record evidence.
- `ToolSelectionConfig` handles step-level LLM tool selection.
- `evolution` can be extended to evaluate tool lifecycle changes.

### New Conceptual Components

The long-lived tool catalog should not be stored directly inside
`AffordanceLayer`.

`AffordanceLayer` remains a context snapshot. Catalog governance belongs to
runtime/evolution-level components.

```text
loom.tools.catalog
  stores atomic tools, composed tools, and candidate generated tools

loom.tools.resolver
  resolves a budgeted affordance view for a context

loom.tools.composer
  creates one-level composed or ephemeral tools from atomic tools

loom.tools.evaluation
  scores usage, decay, context savings, success rate, and promotion evidence

loom.tools.sandbox
  validates generated tools before they can run beyond ephemeral scope
```

### Boundary Summary

```text
ToolCatalog
  Long-lived governance state.

ToolEvolutionState
  Mutable scoring, usage, TTL, decay, and promotion evidence.

AffordanceLayer
  Immutable current context view of visible ToolRefs.

RuntimeRegistry
  Executable implementation registry.

Trace Store
  Evidence source for tool evolution.
```

## Tool Metadata

Atomic and composed tools share the existing `ToolRef` shape for LLM exposure.
Lifecycle data lives outside the `ToolRef`.

Conceptual lifecycle state:

```python
@dataclass(frozen=True, slots=True)
class ToolLifecycle:
    layer: Literal["atomic", "composed", "ephemeral", "candidate"]
    created_from_trace_ids: tuple[str, ...]
    ttl_steps: int | None
    usage_count: int
    distinct_context_shapes: int
    success_rate: float
    confidence_delta: float
    token_savings_estimate: int
    decay_score: float
```

This keeps `ToolRef` portable and serializable while allowing the catalog to
track richer governance state.

## Affordance Budget

The resolver should apply a hard budget before tools enter the current context.

Conceptual budget:

```python
@dataclass(frozen=True, slots=True)
class AffordanceBudget:
    max_tool_schema_tokens: int = 4000
    max_tools: int = 15
    max_composed_tools: int = 5
    max_ephemeral_tools: int = 3
```

Resolution order:

1. Keep required atomic essentials.
2. Add high-scoring composed tools relevant to the current goal and context.
3. Add a small number of run-local ephemeral tools.
4. If over budget:
   - remove ephemeral tools first
   - remove composed tools by lowest score or highest decay
   - preserve required atomic essentials

This is separate from step-level LLM tool selection.

```text
Catalog pruning:
  full catalog -> current AffordanceLayer.tools

Step tool selection:
  current AffordanceLayer.tools -> effective tools for this step
```

Catalog pruning is governance. Step tool selection is attention allocation.

## Promotion Rules

A candidate composed tool can be promoted only when it satisfies at least two
evidence categories.

### Reuse Evidence

The same atomic pattern appears in at least three distinct context shapes or
goal shapes.

Reuse is not raw call count. A long run that repeats the same pattern many times
does not prove general utility.

### Compression Evidence

The composed tool's serialized schema and description use fewer tokens than
exposing all underlying atomic tools separately for the same behavior.

### Quality Evidence

Trace data shows improvement in one or more metrics:

- higher successful outcome rate
- higher decision confidence
- fewer retries
- lower failure rate
- fewer tool calls for the same result
- lower total prompt/tool schema cost

### Stability Evidence

The candidate's input and output schemas stop changing across repeated use.

### Auditability Evidence

The candidate can always be expanded into its underlying atomic tool calls in
trace replay.

## Decay And Pruning

Composed tools are not permanent by default.

Each composed tool has TTL and decay. A composed tool becomes a prune candidate
when:

- it is unused for multiple runs
- it is selected but rarely called
- it is called but produces worse outcomes than atomic fallback
- its internal atomic steps are often skipped
- its schema drifts repeatedly
- it exceeds budget pressure compared with alternatives

Pruning removes the composed tool from the governed catalog. It does not remove
the underlying atomic tools or trace evidence.

## Dynamic Generated Tools

LLM-written tools are supported, but they do not bypass governance.

Generated tool lifecycle:

```text
generated code
  -> sandbox validation
  -> schema inference and validation
  -> smoke test
  -> ephemeral tool
  -> candidate tool
  -> shadow evaluation
  -> promotion or rejection
```

Generated tools have strict limits:

- They can call atomic tools only.
- They cannot call composed tools.
- They cannot write directly into the stable catalog.
- They start with minimum permissions.
- They must declare timeout, input schema, output schema, and failure behavior.
- Their execution must be traceable.

Promotion does not automatically expand permissions. Permission changes require
separate policy approval.

## Evolution Signals

Tool evolution extends the existing evolution pattern.

Candidate compose signals:

- the same group of atomic tools appears together across distinct goals
- LLM repeatedly recreates the same ephemeral plan
- several traces show the same action pattern after similar observations

Candidate merge signals:

- a composed tool overlaps heavily with an atomic tool plus one common option
- two composed tools differ only by a small parameter
- a low-frequency composed tool is subsumed by a stronger one

Candidate split signals:

- a composed tool's internal atomic step is frequently skipped
- different contexts use disjoint subsets of the composition
- the composed tool has unstable schema variants

Candidate prune signals:

- low usage after TTL
- low success rate
- negative confidence or outcome delta
- poor token compression
- frequent fallback to atomic tools

## Failure Policy

The system should degrade toward simpler, more stable tools.

```text
Tool selection failure:
  fall back to atomic essentials

Composition failure:
  fall back to original atomic tools

Generated tool validation failure:
  reject candidate and keep trace evidence

Budget overflow:
  prune ephemeral first, composed second, preserve atomic essentials

Runtime tool failure:
  emit tool.failed and allow recovery if budget permits
```

## Security And Permissions

Tool permissions are catalog metadata controlled by policy.

The LLM may propose a tool, composition, or generated implementation, but it
does not grant permissions to that tool.

High-risk tools require explicit policy approval. Generated tools start with the
lowest viable permission set and can only invoke approved atomic tools.

## Design Decision

Loom should treat its tools collection as a bounded tool ecology:

- atomic tools are stable and scarce
- composed tools are useful but governed and temporary by default
- ephemeral tools are run-local and disposable
- generated tools are candidates, not trusted catalog members
- context receives a budgeted affordance view, never the full catalog
- trace evidence decides promotion, merge, split, and prune

This keeps dynamic tool composition powerful without letting tool count,
context cost, or decision complexity grow without bound.
