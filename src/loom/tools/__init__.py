"""Tools public API for Loom."""

from loom.tools.composer import create_composed_tool, create_ephemeral_tool
from loom.tools.contracts import (
    AffordanceBudget,
    CatalogTool,
    ToolCatalog,
    ToolLayer,
    ToolLifecycle,
    ToolResolution,
    estimate_tool_schema_tokens,
)
from loom.tools.evaluation import PromotionEvidence, ToolPattern, detect_tool_patterns, should_promote
from loom.tools.resolver import ToolResolver

__all__ = [
    "AffordanceBudget",
    "CatalogTool",
    "ToolCatalog",
    "ToolLayer",
    "ToolLifecycle",
    "ToolPattern",
    "ToolResolution",
    "ToolResolver",
    "PromotionEvidence",
    "create_composed_tool",
    "create_ephemeral_tool",
    "detect_tool_patterns",
    "estimate_tool_schema_tokens",
    "should_promote",
]
