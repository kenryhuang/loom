"""Loom TUI — real-time terminal visualization of Loom loop execution.

Usage:
    from loom.tui import run_with_tui
    result = await run_with_tui(loop_handle, initial_context)
"""

from loom.tui.tui_runner import run_with_tui

__all__ = ["run_with_tui"]
