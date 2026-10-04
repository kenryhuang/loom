# Loom TUI

Real-time terminal visualization for Loom loop execution — Codex/Claude style.

## What it does

The TUI provides a live, interactive view of a Loom loop as it runs:

- **Folded Process**: Execution is collapsed by default into one process row per run. Its live preview shows the current `Thought: …`, `Tool call (name)`, or paused / failed / completed state
- **Event Stream**: Expand a process to see one line per event, including thoughts, tool calls, run, step, routing, planning, decision, and observation records. Thought deltas and tool lifecycle updates stay in their existing rows. Click an event or press Enter / Space on its selected row to read the full detail
- **Thought and Tool Details**: Expand to read the complete thought, tool arguments, results, diffs, stdout, stderr, and errors in a scrollable area. Large service artifacts load on demand. Streaming updates keep the row expanded; failures remain visible in its preview
- **Final Result**: Expanded Markdown with headings, lists, tables, links, and code blocks, followed by a deduplicated list of successfully edited or written files
- **Session Recovery**: Resume and live execution use the same presentation. Recovery replays the current run across history pages so earlier file edits are included
- **Status Bar**: Live metrics — step count, token usage, duration, run status

The TUI is a curated presentation of the run, not a replacement for its trace.
Every raw event remains in the configured JSONL trace. Successful action and
observation events are contained in the process event stream. Failures and
pending input requests appear in the process preview and the session question
area. The session view retains the latest 500 rows; a pruned process displays
its retained and total row counts. Earlier events remain in service history.

## Style

Dark theme inspired by Codex CLI and Claude's terminal interface:
- Tokyo Night color palette
- Monospace typography
- Two levels of disclosure: process → event rows → complete event details
- Expanded Markdown results and a separate modified-file list
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
from loom.tui.compact import CompactLoomTuiApp
from loom.tui.tui_collector import TuiEventCollector
from loom.runtime.engine import create, run

# Create collector
collector = TuiEventCollector()

# Create loop handle and context
handle = create(loop_definition).unwrap()
context = make_context()

# Create and run TUI
app = CompactLoomTuiApp(collector)
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
| `Ctrl+C` / `q` | Quit |

## Architecture

```
Loop Execution → Trace Events → TuiEventCollector → CompactLoomTuiApp
Session API → replay / live events → SessionTuiApp
                                      ├── CompactEventFeedWidget
                                      │   └── CompactEventItem + Markdown
                                      └── StatusBar
```

### Event Rows

The main view keeps execution progress concise:

```text
▶ Process · 24 events
Thought: Inspect the update path before changing the index.
Tool call (read_file)      src/index.py
Tool call (edit_file)      src/index.py
Tool call (shell_execute)  pytest -q

Result
<rendered Markdown>

Modified files · 1
  src/index.py
```

Each model call updates one thought summary in place, and each tool invocation
updates one row from running to completed or failed. Lifecycle events update the
folded group for their run. Persistent sessions render their authoritative
assistant message as the final result, so intermediate model replies and the
finish tool's report do not create duplicate results. File lists are scoped to
each run and derived from successful `edit_file` and `write_file` events.

## Dependencies

```bash
uv sync --group tui
# or
pip install textual rich
```

## Files

- `tui_collector.py` — Async trace sink that captures events into a queue
- `tui_app.py` — Textual app with event stream, inline detail boxes, and status bar
- `compact.py` — Default compact progress view, process folding, Markdown results, and file summaries
- `tui_runner.py` — High-level `run_with_tui()` that wires everything together
- `demo.py` — Standalone demo script
- `__init__.py` — Public API
