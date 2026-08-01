# Loom TUI

Real-time terminal visualization for Loom loop execution — Codex/Claude style.

## What it does

The TUI provides a live, interactive view of a Loom loop as it runs:

- **Event Stream**: Chronological semantic progress stream with inline collapsible detail boxes
- **Inline Details**: Fixed-height scrollable detail area for LLM prompts/responses, tool inputs/outputs, and trace data
- **LLM Progress**: Streaming reasoning is updated in place; tool-only transport responses and duplicate lifecycle events stay out of the visible stream
- **Tool Outcomes**: Tool calls update one row in place from a compact target to structured result data such as exit status, test counts, bytes, or replacements
- **Status Bar**: Live metrics — step count, token usage, duration, run status

The TUI is a curated presentation of the run, not a replacement for its trace.
Every raw event remains in the configured JSONL trace. Successful action and
observation events that repeat decisions or tool results are hidden from the
visible timeline; guard failures and other actionable observations remain.
Expanding a row retains full reasoning, inputs, outputs, diffs, stdout, stderr,
and error metadata.

## Style

Dark theme inspired by Codex CLI and Claude's terminal interface:
- Tokyo Night color palette
- Monospace typography
- Timeline gutter with `●` event nodes and inline detail blocks connected by `│`
- Flat columns: scope, event, description, step, and status
- Keyboard-driven navigation

## Usage

### Quick start

```python
from loom.tui import run_with_tui

result = await run_with_tui(loop_handle, initial_context)
```

After the loop completes, the TUI stays in the foreground with the final event
state visible. Press `q` to exit and return the run result.

### Demo

```bash
# Counter loop (no LLM needed)
uv run python -m loom.tui.demo

# LLM loop (requires .env with API key)
LOOM_RUN_LIVE_LLM=1 uv run python -m loom.tui.demo
```

The demo also remains open after completion until `q` is pressed.

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
| `Enter` / `Space` | Expand or collapse selected event detail |
| `y` | Copy selected event detail as plain text |
| `Y` | Copy full event transcript as plain text |
| `q` | Quit |

## Architecture

```
Loop Execution → Trace Events → TuiEventCollector → asyncio.Queue → LoomTuiApp
                                                                     ├── EventFeedWidget
                                                                     │   └── EventItem + EventDetailBox
                                                                     └── StatusBar (bottom)
```

### Event Rows

Rows use a flat timeline layout:

```text
● SCOPE   event        description                                   step     status
│  ┌─ details
│  │ ...
│  └─
```

An LLM round produces a streaming `Thinking` row when reasoning is available.
Transport-only request and tool-call response events do not add rows. A genuine
final text response is shown as an `Answer` row. Each tool execution occupies one
row: argument deltas and runtime lifecycle events are merged into it, and a
completed row summarizes structured output instead of repeating invocation
arguments. Run, step, plan, decision, guard, tool, and answer rows keep their
chronological positions and remain available in the full scrollable history.

## Dependencies

```bash
uv sync --group tui
# or
pip install textual rich
```

## Files

- `tui_collector.py` — Async trace sink that captures events into a queue
- `tui_app.py` — Textual app with event stream, inline detail boxes, and status bar
- `tui_runner.py` — High-level `run_with_tui()` that wires everything together
- `demo.py` — Standalone demo script
- `__init__.py` — Public API
