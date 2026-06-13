# Loom TUI

Real-time terminal visualization for Loom loop execution — Codex/Claude style.

## What it does

The TUI provides a live, interactive view of a Loom loop as it runs:

- **Event Timeline** (left panel): Chronological stream of all events — loop start, steps, LLM calls, tool calls, completions
- **Event Detail** (right panel): Full details of the selected event — LLM prompts/responses, tool inputs/outputs, trace data
- **Status Bar**: Live metrics — step count, token usage, duration, run status

## Style

Dark theme inspired by Codex CLI and Claude's terminal interface:
- Tokyo Night color palette
- Monospace typography
- Colored event icons (green for success, magenta for LLM, orange for tools, red for errors)
- Keyboard-driven navigation

## Usage

### Quick start

```python
from loom.tui import run_with_tui

result = await run_with_tui(loop_handle, initial_context)
```

### Demo

```bash
# Counter loop (no LLM needed)
uv run python -m loom.tui.demo

# LLM loop (requires .env with API key)
LOOM_RUN_LIVE_LLM=1 uv run python -m loom.tui.demo
```

### Programmatic usage

```python
from loom.tui.tui_app import LoomTuiApp
from loom.tui.tui_collector import TuiEventCollector
from loom.runtime.engine import create, run

# Create collector
collector = TuiEventCollector()

# Create loop handle and context
handle = create(loop_definition).unwrap()
context = make_context()

# Create and run TUI
app = LoomTuiApp(collector)
app.set_loop_info(role="my agent", goal="do the thing")

# Run loop in background, TUI in foreground
async def _run():
    result = await run(handle, context, trace_sink=collector)
    await collector.put_sentinel()
    return result

import asyncio
loop_task = asyncio.create_task(_run())
await app.run_async()
result = await loop_task
```

## Keybindings

| Key | Action |
|-----|--------|
| `j` | Select next event |
| `k` | Select previous event |
| `g` | Jump to first event |
| `G` | Jump to latest event |
| `Tab` | Switch focus between panels |
| `q` | Quit |

## Architecture

```
Loop Execution → Trace Events → TuiEventCollector → asyncio.Queue → LoomTuiApp
                                                                     ├── TimelineWidget (left)
                                                                     ├── DetailPanel (right)
                                                                     └── StatusBar (bottom)
```

### Event types visualized

| Event | Icon | Color | Description |
|-------|------|-------|-------------|
| `run.started` | ▶ | Green | Loop run begins |
| `run.completed` | ✓ | Green | Loop run finishes |
| `step.started` | → | Cyan | Step iteration begins |
| `step.completed` | ← | Cyan | Step iteration finishes |
| `llm.requested` | ◎ | Magenta | LLM API call sent |
| `llm.completed` | ◉ | Magenta | LLM response received |
| `llm.failed` | ✗ | Red | LLM call failed |
| `tool.started` | ⚙ | Orange | Tool execution begins |
| `tool.completed` | ⚙ | Green | Tool execution finishes |
| `tool.failed` | ⚙ | Red | Tool execution failed |

## Dependencies

```bash
uv sync --extra tui
# or
pip install textual rich
```

## Files

- `tui_collector.py` — Async trace sink that captures events into a queue
- `tui_app.py` — Textual app with timeline, detail panel, and status bar
- `tui_runner.py` — High-level `run_with_tui()` that wires everything together
- `demo.py` — Standalone demo script
- `__init__.py` — Public API
