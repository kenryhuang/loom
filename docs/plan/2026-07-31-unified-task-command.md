# Unified Task Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing generic task runner available as `loom task` while preserving its module entry point and behavior.

**Architecture:** Extend the existing top-level lazy dispatcher with one `task` branch. Forward every argument after `task` unchanged to `loom.tasks.cli.main`, leaving task parsing, execution, output, and failures owned by the task package.

**Tech Stack:** Python 3.11, `argparse`, pytest, uv

---

## File Structure

- Modify `src/loom/cli.py`: recognize `task` and lazily dispatch it to the existing task CLI.
- Modify `tests/tasks/test_task_cli.py`: add a regression test for top-level task argument forwarding.

### Task 1: Add Unified Task Dispatch

**Files:**
- Modify: `src/loom/cli.py:9-26`
- Test: `tests/tasks/test_task_cli.py`

- [ ] **Step 1: Write the failing dispatch test**

Append this test to `tests/tasks/test_task_cli.py`:

```python
def test_top_level_cli_dispatches_task_arguments_unchanged(monkeypatch):
    calls = []

    def fake_task_main(argv):
        calls.append(argv)
        return 23

    monkeypatch.setattr("loom.tasks.cli.main", fake_task_main)
    from loom.cli import main as loom_main

    code = loom_main(["task", "Audit this project", "--workspace", ".", "--model", "main"])

    assert code == 23
    assert calls == [["Audit this project", "--workspace", ".", "--model", "main"]]
```

- [ ] **Step 2: Run the new test and verify the missing command failure**

Run:

```bash
uv run pytest tests/tasks/test_task_cli.py::test_top_level_cli_dispatches_task_arguments_unchanged -q
```

Expected: FAIL because top-level `argparse` rejects `task` as an invalid command.

- [ ] **Step 3: Add the minimal dispatcher branch**

Change the command choices and dispatch chain in `src/loom/cli.py` to include:

```python
parser.add_argument(
    "command",
    choices=("task", "campaign", "candidate", "experiment", "task-set", "governance", "optimize"),
)
```

Add the task branch before the campaign branch:

```python
if args.command == "task":
    from loom.tasks.cli import main as command_main
elif args.command == "campaign":
    from loom.campaigns.cli import main as command_main
```

Leave the final `return command_main(args.arguments)` unchanged so all task arguments are forwarded without reinterpretation.

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
uv run pytest tests/tasks/test_task_cli.py::test_top_level_cli_dispatches_task_arguments_unchanged -q
```

Expected: `1 passed`.

- [ ] **Step 5: Run related CLI regression tests**

Run:

```bash
uv run pytest tests/tasks/test_task_cli.py tests/optimize/test_cli.py -q
```

Expected: all selected tests pass with no failures.

- [ ] **Step 6: Run executable help smoke tests**

Run:

```bash
uv run loom task --help
uv run python -m loom.tasks.run --help
```

Expected: both commands exit with status 0 and show `Run a generic Loom LLM task.` plus task-specific options such as `--workspace`, `--model`, and `--tui`.

- [ ] **Step 7: Run static checks for the changed Python files**

Run:

```bash
uv run ruff check src/loom/cli.py tests/tasks/test_task_cli.py
git diff --check
```

Expected: both commands exit with status 0 and report no errors.

- [ ] **Step 8: Commit the implementation**

```bash
git add src/loom/cli.py tests/tasks/test_task_cli.py
git commit -m "feat: expose task through unified CLI"
```
