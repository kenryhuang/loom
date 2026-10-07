# Loom Python Port Design

## Objective

Build a complete Python implementation of Loom in a sibling directory named `loom_py`, located next to the current `loom` directory:

```text
/Users/huanggui/workspace/
  loom/
  loom_py/
```

The Python implementation will follow approach 1: faithfully preserve Loom's semantics, public concepts, error model, trace behavior, and planned feature scope, while using Python-native internals.

## Scope

The Python project will implement both the TypeScript code that currently exists and the phases described in `exec-plan.md`.

Current TypeScript behavior to port:

- Core contracts: IDs, JSON values, `Result`, `LoomError`, immutable Context, MinimalLoop, Trace.
- Runtime: `create`, `step`, `done`, `run`, registry, cancellation, trace persistence.
- Observability: in-memory trace store and trace sink.
- LLM support: prompt builder, tool conversion, token tracker, LLM step function, OpenAI provider.
- Examples: minimal counter loop and LLM loop.

Planned phases to complete in Python:

- Phase 1: context patching, project, emit, merge, knowledge views.
- Phase 2: chain and nest composition.
- Phase 3: fork composition with bounded concurrency, error modes, and quorum.
- Phase 4: Level 1 meta evolution with context mutations, registry transactions, triggers, and evaluator.
- Phase 5: Level 2/3 evolution with implementation refs, structure mutations, graph validation, and shadow evaluation.
- Phase 6: trace tree/path/summary queries, JSONL trace store, sampling, and archive manifests.

## Architecture

`loom_py` will be a standalone Python package using a `src` layout:

```text
loom_py/
  pyproject.toml
  README.md
  src/loom/
    __init__.py
    core/
    runtime/
    observability/
    composition/
    evolution/
    llm/
    examples/
  tests/
```

The module boundaries mirror the TypeScript project and `exec-plan.md`:

- `loom.core`: serializable contracts, immutable data, IDs, errors, result values, context patches and boundaries.
- `loom.runtime`: loop creation, step execution, completion checks, running loops, cancellation, registries, scheduler.
- `loom.observability`: trace storage, trace readers, JSONL persistence, sampling, archive manifests.
- `loom.composition`: chain, nest, fork, meta, composite traces, composition graph validation.
- `loom.evolution`: mutation models, versioned registry, strategy, evaluator, transaction engine, shadow evaluation.
- `loom.llm`: provider abstractions, prompt building, LLM tool conversion, token tracking, LLM-backed step functions, OpenAI provider.
- `loom.examples`: runnable example factories for each major capability.

## Python API Shape

The implementation will preserve Loom semantics instead of copying TypeScript syntax.

- Data contracts will use `@dataclass(frozen=True, slots=True)` where practical.
- Append-only collections will use tuples in stored immutable objects.
- Helper constructors will accept ordinary Python lists and dictionaries and normalize them into immutable dataclasses.
- `Result` will expose `Result.ok(value)`, `Result.err(error)`, plus module helpers `ok(value)` and `err(error)`.
- `LoomError` will be a frozen dataclass with the same error codes as TypeScript.
- Runtime functions will be asynchronous where the TypeScript API is promise-based: `async def step(...)`, `async def done(...)`, `async def run(...)`.
- Public package exports will be available from `loom.__init__` for ergonomic usage.

## Immutability

Python cannot exactly reproduce JavaScript `Object.freeze`, so the port will use practical immutability:

- Frozen dataclasses for framework objects.
- Tuple normalization for arrays such as observations, decisions, traces, children, tags, tools, loops, resources, and knowledge lists.
- `MappingProxyType` or frozen JSON-normalized mappings for metadata and JSON object values when the object is stored inside Context or Trace.
- Context updates will always create new Context values through patches, merge operations, loop steps, or explicit example helper functions.

Mutation attempts should fail naturally for dataclass fields and tuple collections. The Python tests will assert immutability through attempted assignment and collection mutation.

## Runtime Semantics

The runtime will preserve the TypeScript behavior:

- `create(definition, options)` validates loop definition fields and returns a `LoopHandle` with a trace reader.
- `step(handle, context, options)` emits `step.started`, handles cancellation before invoking user code, calls the loop step, freezes or normalizes the returned context, persists `step.completed`, and converts thrown exceptions into `LoomError(code="INTERNAL")`.
- `done(handle, context, options)` checks cancellation, goal step budget, required evaluators, then delegates to the loop's done function.
- `run(handle, initial_context, options)` loops until done, accumulates traces, returns run metrics, and respects explicit max step limits.
- Expected failures return `Result.err`; invariant failures may raise in tests or development-only validation paths.

Cancellation and timeout will be implemented with Python async primitives:

- Timeout support will use `asyncio.wait_for` or equivalent scoped timeout handling.
- External cancellation will support an explicit cancellation token object and map cancellations to `ABORTED`.
- Timeout errors map to `TIMEOUT` and are retryable.

## Context Boundary Semantics

Phase 1 will implement the context scoping rules from `design.md`:

- `project(parent, child_goal, options)` creates a child Context with its own identity and goal, empty state by default, inherited read-only knowledge, and filtered affordances.
- `emit(child_context, child_loop, status, options)` extracts child observations, decisions, knowledge candidates, trace IDs, and metrics without mutating the parent.
- `merge(parent, child_output, policy)` returns a new parent Context through explicit patch operations.
- Default merge appends child summary state and does not automatically accept child knowledge.

## Composition

Composition functions will return loop handles or loop definitions that are ordinary Loom loops.

- `chain` runs loops sequentially and passes each output Context to the next loop.
- `nest` runs an outer loop, projects a child Context, runs an inner loop, emits child output, and merges the result back into the parent.
- `fork` splits a Context into isolated slices, runs worker loops concurrently with bounded concurrency, and merges successful outputs according to error mode and quorum settings.
- `meta` runs an inner loop, analyzes traces through an evolver loop, applies candidate mutations transactionally, evaluates the candidate, and commits or rolls back.

Composite traces will preserve parent/root relationships and record child trace IDs.

## Evolution

Evolution implements the levels from `exec-plan.md`:

- Level 1: ContextMutation through `ContextPatch`, triggered by repeated gap signals.
- Level 2: LoopMutation through registered implementation refs and immutable definition updates.
- Level 3: StructureMutation through validated composition graph patches.

The versioned registry will:

- Store loop records by `loop_id` and `version`.
- Track active versions separately from historical versions.
- Begin transactions only against the active base version.
- Stage candidate versions without mutating prior definitions.
- Commit accepted candidates and roll back rejected candidates.

Shadow evaluation will compare old and candidate versions with isolated traces and detect high-severity regressions before publication.

## Observability

Observability will include:

- In-memory trace storage with the same query filters as TypeScript.
- Trace tree rebuilding by `root_trace_id`.
- Trace path lookup from leaf to root.
- Trace summary aggregation by outcome, duration, errors, gap, and surprise signals.
- JSONL append-only trace persistence with restart reload.
- Snapshot sampling and snapshot hash generation through stable JSON serialization.
- Archive manifests with chunk hashes, record counts, root trace IDs, and validation.

## LLM Support

The LLM implementation will preserve the current TypeScript behavior:

- Prompt builder produces system/user messages from identity, goal, state, knowledge, budget, constraints, and tools.
- Tool refs convert to OpenAI-compatible function tools.
- Token tracker accumulates prompt, completion, and total tokens.
- LLM step supports tool-call loops, appends tool observations, parses structured JSON decisions, falls back to custom actions for unstructured responses, and enforces token/tool-call budgets.
- OpenAI provider sends chat completion requests, parses content/tool calls/usage, and maps HTTP/network failures to `LLM_FAILED`.

The OpenAI provider will use a small injectable HTTP client boundary so tests do not require network access.

## Testing Strategy

The project will use `pytest` and `pytest-asyncio`.

Tests will cover:

- Ported equivalents of current TypeScript tests for core, runtime, observability, LLM, and examples.
- New tests for Phase 1 context boundaries.
- New tests for Phase 2 chain/nest behavior.
- New tests for Phase 3 fork concurrency, error modes, and quorum.
- New tests for Phase 4 Level 1 evolution.
- New tests for Phase 5 Level 2/3 mutation and shadow evaluation.
- New tests for Phase 6 JSONL persistence, trace queries, sampling, and archive validation.

The implementation will follow test-first development for behavioral code: write a failing test, verify it fails for the expected reason, implement the smallest change, then verify the test passes.

## Tooling

`pyproject.toml` will define:

- Package metadata for `loom`.
- Python version target: Python 3.11 or newer.
- Test dependencies: `pytest`, `pytest-asyncio`.
- Optional developer tooling: `ruff` for linting and formatting if available locally.

Verification commands:

```bash
python -m pytest
python -m ruff check src tests
python -m ruff format --check src tests
```

If `ruff` is not installed or not declared in the local environment, pytest remains the required behavioral verification command.

## Acceptance Criteria

- `loom_py` exists as a directory fully parallel to `loom`.
- `loom_py` is a standalone Python package with source, tests, examples, and README.
- Public APIs cover current TypeScript exports and the planned Phase 1-6 capabilities.
- All Python tests pass.
- No files inside the existing `loom` implementation are modified except planning/spec documents needed to guide this work.
- The final implementation reports exact verification commands and outcomes.
