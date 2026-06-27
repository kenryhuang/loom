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
