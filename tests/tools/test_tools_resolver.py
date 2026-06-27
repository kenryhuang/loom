from loom.core import ToolRef
from loom.tools import (
    AffordanceBudget,
    CatalogTool,
    ToolCatalog,
    ToolLifecycle,
    ToolResolver,
)


def _tool(tool_id, layer="atomic", *, score=0.0, description=None):
    lifecycle = ToolLifecycle(
        layer=layer,
        usage_count=int(score * 10),
        success_rate=score,
        token_savings_estimate=int(score * 100),
    )
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
