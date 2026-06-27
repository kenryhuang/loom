# Tools System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded Loom tools system with layered tool catalog contracts, budgeted affordance resolution, one-level tool composition, trace-driven evaluation signals, and LLM-step resolver integration.

**Architecture:** Add a focused `loom.tools` package that keeps catalog governance outside `AffordanceLayer` while still emitting ordinary `ToolRef` values for contexts. Keep runtime execution in `RuntimeRegistry.tools`; add an optional LLM step hook that resolves/prunes tools before existing `ToolSelectionConfig` selection runs.

**Tech Stack:** Python 3.11, frozen dataclasses, existing `Result`/`LoomError` contracts, pytest, pytest-asyncio, uv, ruff.

---

## File Structure

- Create `src/loom/tools/contracts.py`
  - Owns `ToolLayer`, `ToolLifecycle`, `CatalogTool`, `ToolCatalog`, `AffordanceBudget`, and `ToolResolution`.
  - Keeps mutable governance metadata outside `ToolRef`.

- Create `src/loom/tools/resolver.py`
  - Owns deterministic budgeted catalog pruning.
  - Resolves `ToolCatalog` plus optional run-local ephemeral tools into a `ToolResolution`.

- Create `src/loom/tools/composer.py`
  - Owns one-level composition constructors.
  - Enforces that composed and ephemeral tools expand only to atomic dependencies.

- Create `src/loom/tools/evaluation.py`
  - Owns trace-driven pattern detection and promotion evidence scoring helpers.

- Create `src/loom/tools/__init__.py`
  - Export shim only.

- Modify `src/loom/llm/api.py`
  - Add optional `tool_resolver` hook to `create_llm_step_function`.
  - Run the hook before existing tool selection.
  - Preserve existing behavior when no resolver is supplied.

- Modify `src/loom/llm/__init__.py`
  - Export `ToolSelectionConfig` and related selection types that are already public in `loom.llm.api`.

- Modify `tests/test_package_structure.py`
  - Add `tools` to the expected top-level submodule names.

- Create `tests/tools/test_tools_contracts.py`
  - Test catalog contracts, immutability normalization, and layer metadata.

- Create `tests/tools/test_tools_resolver.py`
  - Test resolver ordering, budget pruning, token budget behavior, and run-local ephemeral limits.

- Create `tests/tools/test_tools_composer.py`
  - Test one-level composition rules.

- Create `tests/tools/test_tools_evaluation.py`
  - Test trace pattern detection and promotion evidence.

- Modify `tests/unit/test_tool_selection.py`
  - Test LLM step integration with a resolver before selection.

---

### Task 1: Tool Contract Types

**Files:**
- Create: `tests/tools/test_tools_contracts.py`
- Create: `src/loom/tools/contracts.py`
- Create: `src/loom/tools/__init__.py`
- Modify: `tests/test_package_structure.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_tools_contracts.py`:

```python
from loom.core import ToolRef
from loom.tools import (
    AffordanceBudget,
    CatalogTool,
    ToolCatalog,
    ToolLifecycle,
)


def test_tool_lifecycle_normalizes_sequence_fields():
    lifecycle = ToolLifecycle(
        layer="composed",
        created_from_trace_ids=["trace-1", "trace-2"],
        ttl_steps=20,
        usage_count=4,
        distinct_context_shapes=3,
        success_rate=0.75,
        confidence_delta=0.1,
        token_savings_estimate=120,
        decay_score=0.2,
    )

    assert lifecycle.created_from_trace_ids == ("trace-1", "trace-2")
    assert lifecycle.layer == "composed"


def test_catalog_tool_preserves_tool_ref_and_dependencies():
    ref = ToolRef("search_summary", "Search and summarize notes")
    lifecycle = ToolLifecycle(layer="composed", created_from_trace_ids=("trace-1",))
    tool = CatalogTool(ref, lifecycle, atomic_dependencies=["search", "summarize"])

    assert tool.ref is ref
    assert tool.layer == "composed"
    assert tool.atomic_dependencies == ("search", "summarize")


def test_tool_catalog_keeps_layers_separate_and_returns_all_tools_in_order():
    atomic = CatalogTool(ToolRef("search", "Search"), ToolLifecycle(layer="atomic"))
    composed = CatalogTool(ToolRef("search_summary", "Search summary"), ToolLifecycle(layer="composed"))
    candidate = CatalogTool(ToolRef("candidate", "Candidate"), ToolLifecycle(layer="candidate"))
    catalog = ToolCatalog(atomic=[atomic], composed=[composed], candidates=[candidate])

    assert catalog.atomic == (atomic,)
    assert catalog.composed == (composed,)
    assert catalog.candidates == (candidate,)
    assert catalog.all_tools == (atomic, composed, candidate)
    assert catalog.get("search_summary").value == composed


def test_affordance_budget_defaults_match_bounded_context_design():
    budget = AffordanceBudget()

    assert budget.max_tool_schema_tokens == 4000
    assert budget.max_tools == 15
    assert budget.max_composed_tools == 5
    assert budget.max_ephemeral_tools == 3
```

Modify `tests/test_package_structure.py`:

```python
submodule_names = {"composition", "core", "evolution", "examples", "llm", "observability", "runtime", "tools", "tui"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/tools/test_tools_contracts.py tests/test_package_structure.py -q
```

Expected: FAIL because `loom.tools` does not exist and top-level package expectations do not include the new submodule yet.

- [ ] **Step 3: Implement minimal contracts**

Create `src/loom/tools/contracts.py`:

```python
"""Tool catalog contracts for bounded Loom tool governance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from loom.core.models import Result, ToolRef, err, make_loom_error, ok, thaw_json

ToolLayer = Literal["atomic", "composed", "ephemeral", "candidate"]


def _tuple(value):
    return tuple(value or ())


@dataclass(frozen=True, slots=True)
class ToolLifecycle:
    layer: ToolLayer
    created_from_trace_ids: tuple[str, ...] = ()
    ttl_steps: int | None = None
    usage_count: int = 0
    distinct_context_shapes: int = 0
    success_rate: float = 0.0
    confidence_delta: float = 0.0
    token_savings_estimate: int = 0
    decay_score: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_from_trace_ids", _tuple(self.created_from_trace_ids))


@dataclass(frozen=True, slots=True)
class CatalogTool:
    ref: ToolRef
    lifecycle: ToolLifecycle
    atomic_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "atomic_dependencies", _tuple(self.atomic_dependencies))

    @property
    def id(self) -> str:
        return self.ref.id

    @property
    def layer(self) -> ToolLayer:
        return self.lifecycle.layer


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    atomic: tuple[CatalogTool, ...] = ()
    composed: tuple[CatalogTool, ...] = ()
    candidates: tuple[CatalogTool, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "atomic", _tuple(self.atomic))
        object.__setattr__(self, "composed", _tuple(self.composed))
        object.__setattr__(self, "candidates", _tuple(self.candidates))

    @property
    def all_tools(self) -> tuple[CatalogTool, ...]:
        return (*self.atomic, *self.composed, *self.candidates)

    def get(self, tool_id: str) -> Result:
        for tool in self.all_tools:
            if tool.id == tool_id:
                return ok(tool)
        return err(make_loom_error("VALIDATION_FAILED", "Tool not found", retryable=False, metadata={"tool_id": tool_id}))


@dataclass(frozen=True, slots=True)
class AffordanceBudget:
    max_tool_schema_tokens: int = 4000
    max_tools: int = 15
    max_composed_tools: int = 5
    max_ephemeral_tools: int = 3


@dataclass(frozen=True, slots=True)
class ToolResolution:
    tools: tuple[ToolRef, ...]
    included_ids: tuple[str, ...]
    pruned_ids: tuple[str, ...] = ()
    token_estimate: int = 0
    over_budget: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _tuple(self.tools))
        object.__setattr__(self, "included_ids", _tuple(self.included_ids))
        object.__setattr__(self, "pruned_ids", _tuple(self.pruned_ids))


def estimate_tool_schema_tokens(tool: ToolRef) -> int:
    payload = {
        "id": tool.id,
        "description": tool.description,
        "input_schema": thaw_json(tool.input_schema),
        "output_schema": thaw_json(tool.output_schema),
    }
    return max(1, len(json.dumps(payload, sort_keys=True, separators=(",", ":"))) // 4)
```

Create `src/loom/tools/__init__.py`:

```python
"""Tools public API for Loom."""

from loom.tools.contracts import (
    AffordanceBudget,
    CatalogTool,
    ToolCatalog,
    ToolLayer,
    ToolLifecycle,
    ToolResolution,
    estimate_tool_schema_tokens,
)

__all__ = [
    "AffordanceBudget",
    "CatalogTool",
    "ToolCatalog",
    "ToolLayer",
    "ToolLifecycle",
    "ToolResolution",
    "estimate_tool_schema_tokens",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/tools/test_tools_contracts.py tests/test_package_structure.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/tools tests/tools/test_tools_contracts.py tests/test_package_structure.py
git commit -m "feat: add tool catalog contracts"
```

---

### Task 2: Budgeted Tool Resolver

**Files:**
- Create: `tests/tools/test_tools_resolver.py`
- Create: `src/loom/tools/resolver.py`
- Modify: `src/loom/tools/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_tools_resolver.py`:

```python
from loom.core import ToolRef
from loom.tools import (
    AffordanceBudget,
    CatalogTool,
    ToolCatalog,
    ToolLifecycle,
    ToolResolver,
)


def _tool(tool_id, layer="atomic", *, score=0.0, description=None):
    lifecycle = ToolLifecycle(layer=layer, usage_count=int(score * 10), success_rate=score, token_savings_estimate=int(score * 100))
    deps = ("search", "read") if layer in {"composed", "ephemeral", "candidate"} else ()
    return CatalogTool(ToolRef(tool_id, description or f"{tool_id} tool"), lifecycle, deps)


def test_resolver_keeps_atomic_before_composed_and_ephemeral():
    catalog = ToolCatalog(atomic=[_tool("search"), _tool("read")], composed=[_tool("summary", "composed", score=0.9)])
    ephemeral = (_tool("run_plan", "ephemeral", score=0.1),)

    resolution = ToolResolver().resolve(catalog, AffordanceBudget(max_tools=4), ephemeral=ephemeral)

    assert resolution.included_ids == ("search", "read", "summary", "run_plan")
    assert resolution.pruned_ids == ()
    assert [tool.id for tool in resolution.tools] == ["search", "read", "summary", "run_plan"]


def test_resolver_prunes_ephemeral_before_composed_when_tool_count_is_tight():
    catalog = ToolCatalog(
        atomic=[_tool("search"), _tool("read")],
        composed=[_tool("summary", "composed", score=0.9), _tool("draft", "composed", score=0.5)],
    )
    ephemeral = (_tool("scratch", "ephemeral"),)

    resolution = ToolResolver().resolve(catalog, AffordanceBudget(max_tools=3), ephemeral=ephemeral)

    assert resolution.included_ids == ("search", "read", "summary")
    assert "scratch" in resolution.pruned_ids
    assert "draft" in resolution.pruned_ids


def test_resolver_limits_composed_and_ephemeral_layers():
    catalog = ToolCatalog(
        atomic=[_tool("search")],
        composed=[_tool("high", "composed", score=0.9), _tool("low", "composed", score=0.1)],
    )
    ephemeral = (_tool("scratch_a", "ephemeral"), _tool("scratch_b", "ephemeral"))

    resolution = ToolResolver().resolve(
        catalog,
        AffordanceBudget(max_tools=5, max_composed_tools=1, max_ephemeral_tools=1),
        ephemeral=ephemeral,
    )

    assert resolution.included_ids == ("search", "high", "scratch_a")
    assert set(resolution.pruned_ids) == {"low", "scratch_b"}


def test_resolver_marks_required_atomic_over_budget_instead_of_dropping_it():
    catalog = ToolCatalog(atomic=[_tool("terminal", description="x" * 200)])

    resolution = ToolResolver(required_atomic_ids=("terminal",)).resolve(
        catalog,
        AffordanceBudget(max_tools=1, max_tool_schema_tokens=1),
    )

    assert resolution.included_ids == ("terminal",)
    assert resolution.over_budget is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/tools/test_tools_resolver.py -q
```

Expected: FAIL because `ToolResolver` does not exist.

- [ ] **Step 3: Implement resolver**

Create `src/loom/tools/resolver.py`:

```python
"""Budgeted resolver for current-context tool affordances."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from loom.core.models import ToolRef
from loom.tools.contracts import AffordanceBudget, CatalogTool, ToolCatalog, ToolResolution, estimate_tool_schema_tokens


@dataclass(frozen=True, slots=True)
class ToolResolver:
    required_atomic_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_atomic_ids", tuple(self.required_atomic_ids))

    def resolve(
        self,
        catalog: ToolCatalog,
        budget: AffordanceBudget | None = None,
        *,
        ephemeral: Iterable[CatalogTool] | None = None,
    ) -> ToolResolution:
        budget = budget or AffordanceBudget()
        ephemeral_tools = tuple(ephemeral or ())

        required_atomic, optional_atomic = _partition_required(catalog.atomic, self.required_atomic_ids)
        composed = tuple(sorted(catalog.composed, key=_priority_score, reverse=True))[: budget.max_composed_tools]
        ephemeral_limited = ephemeral_tools[: budget.max_ephemeral_tools]

        candidates = (*required_atomic, *optional_atomic, *composed, *ephemeral_limited)
        excluded_by_layer_limit = _ids(catalog.composed[budget.max_composed_tools :]) + _ids(ephemeral_tools[budget.max_ephemeral_tools :])

        included: list[CatalogTool] = []
        pruned: list[str] = list(excluded_by_layer_limit)
        total_tokens = 0
        over_budget = False

        for tool in candidates:
            tool_tokens = estimate_tool_schema_tokens(tool.ref)
            must_keep = tool.layer == "atomic" and tool.id in self.required_atomic_ids
            fits_count = len(included) < budget.max_tools
            fits_tokens = total_tokens + tool_tokens <= budget.max_tool_schema_tokens
            if must_keep or (fits_count and fits_tokens):
                included.append(tool)
                total_tokens += tool_tokens
                if must_keep and (not fits_count or not fits_tokens):
                    over_budget = True
            else:
                pruned.append(tool.id)

        return ToolResolution(
            tools=tuple(tool.ref for tool in included),
            included_ids=tuple(tool.id for tool in included),
            pruned_ids=tuple(dict.fromkeys(pruned)),
            token_estimate=total_tokens,
            over_budget=over_budget,
        )


def _partition_required(tools: tuple[CatalogTool, ...], required_ids: tuple[str, ...]) -> tuple[tuple[CatalogTool, ...], tuple[CatalogTool, ...]]:
    required = tuple(tool for tool in tools if tool.id in required_ids)
    optional = tuple(tool for tool in tools if tool.id not in required_ids)
    return required, optional


def _priority_score(tool: CatalogTool) -> float:
    lifecycle = tool.lifecycle
    return (
        lifecycle.success_rate
        + lifecycle.confidence_delta
        + lifecycle.usage_count * 0.01
        + lifecycle.distinct_context_shapes * 0.05
        + lifecycle.token_savings_estimate * 0.001
        - lifecycle.decay_score
    )


def _ids(tools: Iterable[CatalogTool]) -> list[str]:
    return [tool.id for tool in tools]
```

Update `src/loom/tools/__init__.py`:

```python
from loom.tools.resolver import ToolResolver
```

and add `"ToolResolver"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/tools/test_tools_resolver.py tests/tools/test_tools_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/tools tests/tools/test_tools_resolver.py
git commit -m "feat: add budgeted tool resolver"
```

---

### Task 3: One-Level Tool Composer

**Files:**
- Create: `tests/tools/test_tools_composer.py`
- Create: `src/loom/tools/composer.py`
- Modify: `src/loom/tools/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_tools_composer.py`:

```python
from loom.core import ToolRef
from loom.tools import CatalogTool, ToolLifecycle, create_composed_tool, create_ephemeral_tool


def _catalog_tool(tool_id, layer):
    deps = ("search",) if layer == "composed" else ()
    return CatalogTool(ToolRef(tool_id, f"{tool_id} tool"), ToolLifecycle(layer=layer), deps)


def test_create_composed_tool_from_atomic_dependencies():
    search = _catalog_tool("search", "atomic")
    read = _catalog_tool("read", "atomic")

    result = create_composed_tool(
        "search_summary",
        "Search then summarize",
        (search, read),
        trace_ids=("trace-1",),
        ttl_steps=10,
    )

    assert result.ok
    composed = result.value
    assert composed.layer == "composed"
    assert composed.atomic_dependencies == ("search", "read")
    assert composed.lifecycle.created_from_trace_ids == ("trace-1",)
    assert composed.lifecycle.ttl_steps == 10


def test_create_composed_tool_rejects_composed_dependencies():
    search = _catalog_tool("search", "atomic")
    summary = _catalog_tool("summary", "composed")

    result = create_composed_tool("nested", "Nested", (search, summary))

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_create_ephemeral_tool_is_run_local_and_atomic_only():
    search = _catalog_tool("search", "atomic")
    read = _catalog_tool("read", "atomic")

    result = create_ephemeral_tool("scratch", "Scratch plan", (search, read))

    assert result.ok
    assert result.value.layer == "ephemeral"
    assert result.value.lifecycle.ttl_steps == 0
    assert result.value.atomic_dependencies == ("search", "read")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/tools/test_tools_composer.py -q
```

Expected: FAIL because composer functions do not exist.

- [ ] **Step 3: Implement composer**

Create `src/loom/tools/composer.py`:

```python
"""One-level tool composition helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from loom.core.models import Result, ToolRef, err, make_loom_error, ok
from loom.tools.contracts import CatalogTool, ToolLifecycle


def create_composed_tool(
    tool_id: str,
    description: str,
    atomic_tools: Iterable[CatalogTool],
    *,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    timeout_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    trace_ids: Iterable[str] | None = None,
    ttl_steps: int | None = None,
) -> Result:
    dependencies = tuple(atomic_tools)
    validation = _validate_atomic_dependencies(dependencies)
    if not validation.ok:
        return validation
    ref = ToolRef(tool_id, description, input_schema=input_schema, output_schema=output_schema, timeout_ms=timeout_ms, metadata=metadata)
    lifecycle = ToolLifecycle(layer="composed", created_from_trace_ids=tuple(trace_ids or ()), ttl_steps=ttl_steps)
    return ok(CatalogTool(ref, lifecycle, tuple(tool.id for tool in dependencies)))


def create_ephemeral_tool(
    tool_id: str,
    description: str,
    atomic_tools: Iterable[CatalogTool],
    *,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    timeout_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    trace_ids: Iterable[str] | None = None,
) -> Result:
    dependencies = tuple(atomic_tools)
    validation = _validate_atomic_dependencies(dependencies)
    if not validation.ok:
        return validation
    ref = ToolRef(tool_id, description, input_schema=input_schema, output_schema=output_schema, timeout_ms=timeout_ms, metadata=metadata)
    lifecycle = ToolLifecycle(layer="ephemeral", created_from_trace_ids=tuple(trace_ids or ()), ttl_steps=0)
    return ok(CatalogTool(ref, lifecycle, tuple(tool.id for tool in dependencies)))


def _validate_atomic_dependencies(tools: tuple[CatalogTool, ...]) -> Result:
    if len(tools) < 2:
        return err(make_loom_error("VALIDATION_FAILED", "Tool composition requires at least two atomic tools", retryable=False))
    non_atomic = tuple(tool.id for tool in tools if tool.layer != "atomic")
    if non_atomic:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Tool composition can only depend on atomic tools",
                retryable=False,
                metadata={"non_atomic_dependencies": non_atomic},
            )
        )
    return ok(None)
```

Update `src/loom/tools/__init__.py`:

```python
from loom.tools.composer import create_composed_tool, create_ephemeral_tool
```

and add both names to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/tools/test_tools_composer.py tests/tools/test_tools_resolver.py tests/tools/test_tools_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/tools tests/tools/test_tools_composer.py
git commit -m "feat: add one-level tool composer"
```

---

### Task 4: Trace-Driven Tool Evaluation

**Files:**
- Create: `tests/tools/test_tools_evaluation.py`
- Create: `src/loom/tools/evaluation.py`
- Modify: `src/loom/tools/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_tools_evaluation.py`:

```python
from loom.core import Action, Trace, new_run_id
from loom.tools import PromotionEvidence, detect_tool_patterns, should_promote


def _trace(trace_id, context_id, tool_ids, *, outcome="pass", confidence=0.8):
    actions = tuple(Action(f"{trace_id}-{tool_id}", "tool", f"Use {tool_id}", target=tool_id) for tool_id in tool_ids)
    return Trace(
        id=trace_id,
        run_id=new_run_id(),
        loop_id="loop",
        loop_version="v1",
        step_number=0,
        root_trace_id=trace_id,
        started_at="2026-06-27T00:00:00.000Z",
        ended_at="2026-06-27T00:00:00.001Z",
        duration_ms=1,
        input_context_id=context_id,
        output_context_id=f"{context_id}-out",
        outcome=outcome,
        actions=actions,
        metadata={"decisionConfidence": confidence},
    )


def test_detect_tool_patterns_counts_distinct_contexts():
    traces = (
        _trace("trace-1", "ctx-a", ("search", "read")),
        _trace("trace-2", "ctx-b", ("read", "search")),
        _trace("trace-3", "ctx-c", ("search", "write")),
    )

    patterns = detect_tool_patterns(traces, min_distinct_contexts=2)

    assert len(patterns) == 1
    assert patterns[0].tool_ids == ("read", "search")
    assert patterns[0].trace_ids == ("trace-1", "trace-2")
    assert patterns[0].distinct_context_shapes == 2


def test_should_promote_requires_at_least_two_evidence_categories():
    weak = PromotionEvidence(reuse=False, compression=True, quality=False, stability=False, auditability=False)
    strong = PromotionEvidence(reuse=True, compression=True, quality=False, stability=False, auditability=False)

    assert should_promote(weak) is False
    assert should_promote(strong) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/tools/test_tools_evaluation.py -q
```

Expected: FAIL because evaluation helpers do not exist.

- [ ] **Step 3: Implement evaluation helpers**

Create `src/loom/tools/evaluation.py`:

```python
"""Trace-driven helpers for tool evolution proposals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from loom.core.models import Trace


@dataclass(frozen=True, slots=True)
class ToolPattern:
    tool_ids: tuple[str, ...]
    trace_ids: tuple[str, ...]
    distinct_context_shapes: int


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    reuse: bool = False
    compression: bool = False
    quality: bool = False
    stability: bool = False
    auditability: bool = False

    @property
    def score(self) -> int:
        return sum((self.reuse, self.compression, self.quality, self.stability, self.auditability))


def detect_tool_patterns(traces: tuple[Trace, ...], *, min_distinct_contexts: int = 3) -> tuple[ToolPattern, ...]:
    grouped: dict[tuple[str, ...], dict[str, set[str] | list[str]]] = defaultdict(lambda: {"trace_ids": [], "contexts": set()})
    for trace in traces:
        tool_ids = tuple(sorted({action.target for action in trace.actions if action.kind == "tool" and action.target}))
        if len(tool_ids) < 2:
            continue
        grouped[tool_ids]["trace_ids"].append(trace.id)
        grouped[tool_ids]["contexts"].add(trace.input_context_id)

    patterns = []
    for tool_ids, data in grouped.items():
        contexts = data["contexts"]
        trace_ids = data["trace_ids"]
        if len(contexts) >= min_distinct_contexts:
            patterns.append(ToolPattern(tool_ids, tuple(trace_ids), len(contexts)))
    return tuple(sorted(patterns, key=lambda pattern: (-pattern.distinct_context_shapes, pattern.tool_ids)))


def should_promote(evidence: PromotionEvidence, *, minimum_categories: int = 2) -> bool:
    return evidence.score >= minimum_categories
```

Update `src/loom/tools/__init__.py`:

```python
from loom.tools.evaluation import PromotionEvidence, ToolPattern, detect_tool_patterns, should_promote
```

and add the names to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/tools/test_tools_evaluation.py tests/tools/test_tools_composer.py tests/tools/test_tools_resolver.py tests/tools/test_tools_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/tools tests/tools/test_tools_evaluation.py
git commit -m "feat: add trace-driven tool evaluation"
```

---

### Task 5: LLM Resolver Integration

**Files:**
- Modify: `tests/unit/test_tool_selection.py`
- Modify: `src/loom/llm/api.py`
- Modify: `src/loom/llm/__init__.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/unit/test_tool_selection.py`:

```python
class TestCreateLlmStepWithToolResolver:
    @pytest.mark.asyncio
    async def test_tool_resolver_runs_before_llm_tool_selection(self):
        from loom.core.models import MinimalLoopDefinition, new_loop_id, new_loop_version
        from loom.llm.api import LlmToolCall, create_llm_step_function
        from loom.runtime import create, create_runtime_registry, run

        ctx = _make_context_with_tools(["search", "write", "delete"])
        captured_tool_names = []

        class Provider:
            model = "resolver-test"

            async def chat(self, messages, tools=None, cancellation=None):
                if tools:
                    captured_tool_names.append(tuple(tool["function"]["name"] for tool in tools))
                return ok(
                    LlmResponse(
                        content='{"reasoning":"done","action":{"kind":"none","description":"done"},"alternatives":[],"confidence":1}',
                    )
                )

        def resolver(context):
            return (tool for tool in context.affordances.tools if tool.id == "search")

        loop = create(
            MinimalLoopDefinition(
                id=new_loop_id(),
                version=new_loop_version(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="test"),
                step=create_llm_step_function(Provider(), tool_resolver=resolver),
                done=lambda context, runtime: ok(len(context.state.decisions) > 0),
            ),
            registry=create_runtime_registry(),
        ).unwrap()

        result = await run(loop, ctx, max_steps=1)

        assert result.ok
        assert captured_tool_names == [("search",)]
        assert result.value.traces[0].metadata["toolResolution"]["included"] == ("search",)
        assert set(result.value.traces[0].metadata["toolResolution"]["pruned"]) == {"write", "delete"}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_tool_selection.py::TestCreateLlmStepWithToolResolver::test_tool_resolver_runs_before_llm_tool_selection -q
```

Expected: FAIL because `create_llm_step_function` does not accept `tool_resolver`.

- [ ] **Step 3: Implement resolver hook**

Modify `src/loom/llm/api.py`.

Add `Iterable` to imports from `collections.abc`:

```python
from collections.abc import Iterable, Mapping
```

Change signature:

```python
def create_llm_step_function(
    provider: Any,
    *,
    prompt_options: dict[str, Any] | None = None,
    enable_tool_calling: bool = True,
    max_tool_calls_per_step: int = 5,
    tool_selection: ToolSelectionConfig | None = None,
    tool_resolver: Any = None,
):
```

Inside `llm_step`, replace:

```python
all_tools = context.affordances.tools
effective_tools = all_tools
tool_selection_result: ToolSelectionResult | None = None
```

with:

```python
all_tools = context.affordances.tools
tool_resolution_metadata: dict[str, Any] | None = None
if enable_tool_calling and tool_resolver is not None:
    resolved = tool_resolver(context)
    if inspect.isawaitable(resolved):
        resolved = await resolved
    if isinstance(resolved, Result):
        if not resolved.ok:
            return resolved
        resolved = resolved.value
    all_tools, tool_resolution_metadata = _normalize_tool_resolution(all_tools, resolved)

effective_tools = all_tools
tool_selection_result: ToolSelectionResult | None = None
```

When building `trace_metadata`, add:

```python
if tool_resolution_metadata is not None:
    trace_metadata["toolResolution"] = tool_resolution_metadata
```

Add helper near tool selection helpers:

```python
def _normalize_tool_resolution(original_tools: tuple[Any, ...], resolved: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if hasattr(resolved, "tools"):
        tools = tuple(resolved.tools)
        included = tuple(getattr(resolved, "included_ids", tuple(tool.id for tool in tools)))
        pruned = tuple(getattr(resolved, "pruned_ids", tuple(tool.id for tool in original_tools if tool.id not in included)))
        metadata = {
            "included": included,
            "pruned": pruned,
            "tokenEstimate": getattr(resolved, "token_estimate", None),
            "overBudget": getattr(resolved, "over_budget", False),
        }
        return tools, metadata

    tools = tuple(resolved)
    included = tuple(tool.id for tool in tools)
    pruned = tuple(tool.id for tool in original_tools if tool.id not in included)
    return tools, {"included": included, "pruned": pruned, "tokenEstimate": None, "overBudget": False}
```

Modify `src/loom/llm/__init__.py` to export existing selection classes:

```python
ToolSelectionConfig,
ToolSelectionResult,
build_tool_selection_prompt,
```

- [ ] **Step 4: Run resolver integration test**

Run:

```bash
uv run pytest tests/unit/test_tool_selection.py::TestCreateLlmStepWithToolResolver::test_tool_resolver_runs_before_llm_tool_selection -q
```

Expected: PASS.

- [ ] **Step 5: Run LLM and tools tests**

Run:

```bash
uv run pytest tests/unit/test_tool_selection.py tests/llm/test_llm.py tests/tools -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/loom/llm tests/unit/test_tool_selection.py
git commit -m "feat: resolve tool affordances before llm selection"
```

---

### Task 6: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run full test suite**

Run:

```bash
uv run pytest
```

Expected: 141+ tests pass, live smoke tests skipped unless environment enables them.

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 3: Run format check**

Run:

```bash
uv run ruff format --check src tests
```

Expected: PASS.

- [ ] **Step 4: Commit any final cleanup**

If any formatting or lint fixes were required:

```bash
git add src tests docs/superpowers/plans/2026-06-27-tools-system-implementation.md
git commit -m "chore: finalize tools system implementation"
```

If no cleanup was required, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage:
  - layered tools: Task 1
  - bounded context budget: Task 2
  - one-level composition: Task 3
  - trace-driven evolution signals: Task 4
  - LLM resolver before selection: Task 5
  - verification: Task 6

- Scope choices:
  - Generated tool sandbox is intentionally not implemented. The spec says generated tools are preserved as a controlled extension; this plan implements the governance primitives needed before a sandbox exists.
  - Runtime execution remains in `RuntimeRegistry.tools`; this plan does not introduce a second execution path.
  - `AffordanceLayer` remains unchanged; catalog governance lives in `loom.tools`.

- No implementation task should modify unrelated TUI, composition, observability persistence, or OpenAI provider behavior.
