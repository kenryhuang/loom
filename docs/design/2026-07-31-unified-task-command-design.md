# Unified Task Command Design

## Goal

Expose the existing generic task runner through the unified Loom CLI as:

```bash
loom task "Check this project" --workspace . --model main --tui
```

The existing `python -m loom.tasks.run` entry point remains supported.

## Design

The top-level dispatcher in `src/loom/cli.py` will accept `task` alongside the
existing command names. When selected, it will lazily import
`loom.tasks.cli.main` and pass all remaining arguments to it unchanged.

The task CLI remains the sole owner of task-specific argument parsing,
configuration discovery, execution, output, and error handling. The unified
dispatcher will not duplicate any of that behavior.

The resulting call path is:

```text
loom task <arguments>
  -> loom.cli.main()
  -> loom.tasks.cli.main(<arguments>)
  -> run_generic_task()
```

## Compatibility

- `python -m loom.tasks.run ...` continues to work without modification.
- Existing unified commands and their argument forwarding remain unchanged.
- Task command options keep their current meanings and defaults.
- Task failures retain the current non-zero `SystemExit` behavior produced by
  `loom.tasks.cli.main`.

## Testing

Add a top-level CLI regression test that substitutes the task command handler,
invokes `loom.cli.main` with `task` arguments, and verifies that the remaining
arguments are forwarded unchanged.

Run the focused CLI tests and an executable help smoke test:

```bash
uv run pytest tests/tasks/test_task_cli.py tests/optimize/test_cli.py -q
uv run loom task --help
```

The smoke test must show the generic task runner help rather than the unified
dispatcher help.

## Out of Scope

- Renaming or removing the module-based task entry point.
- Refactoring all Loom commands onto shared `argparse` subparsers.
- Changing task configuration, runtime, output, or error semantics.
- Registering an additional `loom-task` executable.
