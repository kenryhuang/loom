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
