# Real Project Smoke Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic Loom example loop that audits a real local project by inspecting repository context, running smoke commands, optionally running a yakDB CLI smoke path, and emitting a traceable markdown report.

**Architecture:** Add a focused `loom.examples.real_project_smoke` module with small dataclasses, command helpers, report synthesis, a one-step `MinimalLoopDefinition`, and a `python -m` entrypoint. The implementation treats target repositories as read-only and writes only to temporary smoke workspaces outside the target tree.

**Tech Stack:** Python 3.11, frozen dataclasses, `subprocess`, `tempfile`, existing Loom `Context`/`Trace`/`StepResult`/`Result`, pytest, uv, ruff.

---

## File Structure

- Create `src/loom/examples/real_project_smoke.py`
  - Owns config, project inspection, command execution, CLI smoke, report synthesis, Loom loop factory, run helper, and module `main()`.

- Modify `src/loom/examples/__init__.py`
  - Export the public API from `real_project_smoke`.

- Create `tests/integration/test_real_project_smoke.py`
  - Tests report synthesis, command capture, skipped CLI smoke, one-step Loom loop output, and invalid target handling using temporary projects.

---

### Task 1: Project Inspection And Report Skeleton

**Files:**
- Create: `tests/integration/test_real_project_smoke.py`
- Create: `src/loom/examples/real_project_smoke.py`
- Modify: `src/loom/examples/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_real_project_smoke.py`:

```python
from pathlib import Path

from loom.examples.real_project_smoke import (
    RealProjectSmokeConfig,
    inspect_project,
    synthesize_report,
)


def test_inspect_project_extracts_readme_purpose_and_pyproject_name(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text("# SampleDB\n\nThe AI-native file database.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "sampledb"\n', encoding="utf-8")

    info = inspect_project(project)

    assert info.name == "sampledb"
    assert info.purpose == "The AI-native file database."
    assert "README.md" in info.files


def test_synthesize_report_includes_observed_sections(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)
    project_info = inspect_project(tmp_path)

    report = synthesize_report(config, project_info, smoke=None, cli_smoke=None)

    assert report.startswith("# Real Project Smoke Audit:")
    assert "## Purpose" in report
    assert "## Repository State" in report
    assert "## Smoke Test" in report
    assert "## Improvement Directions" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: FAIL because `loom.examples.real_project_smoke` does not exist.

- [ ] **Step 3: Implement minimal inspection/report code**

Create `src/loom/examples/real_project_smoke.py` with `RealProjectSmokeConfig`, `ProjectInfo`, `inspect_project()`, and `synthesize_report()`.

Update `src/loom/examples/__init__.py` to export:

```python
from loom.examples.real_project_smoke import (
    ProjectInfo,
    RealProjectSmokeConfig,
    inspect_project,
    synthesize_report,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/examples tests/integration/test_real_project_smoke.py
git commit -m "feat: add real project smoke inspection"
```

---

### Task 2: Smoke Command And CLI Smoke Runners

**Files:**
- Modify: `tests/integration/test_real_project_smoke.py`
- Modify: `src/loom/examples/real_project_smoke.py`

- [ ] **Step 1: Write failing command runner tests**

Append to `tests/integration/test_real_project_smoke.py`:

```python
import sys

from loom.examples.real_project_smoke import run_command, run_smoke_test, run_yakdb_cli_smoke


def test_run_command_captures_exit_code_stdout_and_stderr(tmp_path: Path):
    result = run_command((sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)"), cwd=tmp_path, timeout_seconds=5)

    assert result.exit_code == 2
    assert "out" in result.stdout
    assert "err" in result.stderr


def test_run_smoke_test_uses_configured_command(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path, smoke_command=(sys.executable, "-c", "print('smoke ok')"))

    result = run_smoke_test(config)

    assert result.exit_code == 0
    assert "smoke ok" in result.stdout


def test_yakdb_cli_smoke_skips_non_yakdb_projects(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)
    project_info = inspect_project(tmp_path)

    result = run_yakdb_cli_smoke(config, project_info)

    assert result.skipped is True
    assert "not detected" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: FAIL because command runner functions do not exist.

- [ ] **Step 3: Implement command runners**

Add `CommandResult`, `CliSmokeResult`, `run_command()`, `run_smoke_test()`, and `run_yakdb_cli_smoke()`.

`run_yakdb_cli_smoke()` should:

- skip when `cli_smoke_enabled` is false
- skip when the inspected project is not yakDB-like
- create a temporary directory using `tempfile.TemporaryDirectory(prefix="loom-yakdb-smoke-")`
- run `uv run yakdb init`, `uv run yakdb index`, `uv run yakdb grep`, and `uv run yakdb read` with `cwd=config.target_path`
- return captured command results and inferred findings

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/examples/real_project_smoke.py tests/integration/test_real_project_smoke.py
git commit -m "feat: add real project smoke command runners"
```

---

### Task 3: Loom Loop And Public Run Helper

**Files:**
- Modify: `tests/integration/test_real_project_smoke.py`
- Modify: `src/loom/examples/real_project_smoke.py`

- [ ] **Step 1: Write failing loop tests**

Append to `tests/integration/test_real_project_smoke.py`:

```python
import asyncio

from loom.examples.real_project_smoke import (
    make_real_project_smoke_context,
    make_real_project_smoke_loop,
    run_real_project_smoke,
)


def test_real_project_smoke_loop_produces_report_and_trace(tmp_path: Path):
    project = tmp_path / "loop-project"
    project.mkdir()
    (project / "README.md").write_text("# Loop Project\n\nA project for loop smoke testing.\n", encoding="utf-8")
    config = RealProjectSmokeConfig(
        target_path=project,
        smoke_command=(sys.executable, "-c", "print('loop smoke ok')"),
        cli_smoke_enabled=False,
    )

    result = asyncio.run(run_real_project_smoke(config))

    assert result.ok
    assert result.value.metrics.steps == 1
    assert "loop smoke ok" in result.value.output
    assert len(result.value.traces) == 1
    assert len(result.value.traces[0].observations) == 4


def test_real_project_smoke_context_rejects_missing_target(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path / "missing")

    result = make_real_project_smoke_context(config)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_loop_factory_returns_one_step_loop(tmp_path: Path):
    config = RealProjectSmokeConfig(target_path=tmp_path)

    loop = make_real_project_smoke_loop(config)

    assert loop.identity.role == "real project smoke auditor"
    assert loop.goal.objective.startswith("Audit")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: FAIL because loop factory/helper functions do not exist.

- [ ] **Step 3: Implement Loom loop**

Add:

- `make_real_project_smoke_context(config) -> Result`
- `make_real_project_smoke_loop(config) -> MinimalLoopDefinition`
- `run_real_project_smoke(config) -> Result`

The loop should append observations for inspect, smoke, cli smoke, and report, then return a `RunResult` with markdown report output.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loom/examples/real_project_smoke.py tests/integration/test_real_project_smoke.py
git commit -m "feat: add real project smoke loom loop"
```

---

### Task 4: Module Entrypoint And YakDB Manual Demo Path

**Files:**
- Modify: `tests/integration/test_real_project_smoke.py`
- Modify: `src/loom/examples/real_project_smoke.py`

- [ ] **Step 1: Write failing CLI formatting tests**

Append to `tests/integration/test_real_project_smoke.py`:

```python
from loom.examples.real_project_smoke import DEFAULT_YAKDB_PATH, parse_args


def test_parse_args_defaults_to_yakdb_path():
    config = parse_args(())

    assert str(config.target_path) == DEFAULT_YAKDB_PATH


def test_parse_args_accepts_custom_path_and_smoke_command(tmp_path: Path):
    config = parse_args((str(tmp_path), "--smoke-command", "python -c pass", "--no-cli-smoke"))

    assert config.target_path == tmp_path
    assert config.smoke_command == ("python", "-c", "pass")
    assert config.cli_smoke_enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
```

Expected: FAIL because CLI helpers do not exist.

- [ ] **Step 3: Implement CLI helpers**

Add:

- `DEFAULT_YAKDB_PATH = "/Users/huanggui/workspace/yakDB"`
- `parse_args(argv)`
- `main(argv=None)`
- `if __name__ == "__main__": main()`

Use `argparse` and `shlex.split()` for `--smoke-command`.

- [ ] **Step 4: Run focused tests and manual help**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py -q
uv run python -m loom.examples.real_project_smoke --help
```

Expected: tests PASS and help exits 0.

- [ ] **Step 5: Commit**

```bash
git add src/loom/examples/real_project_smoke.py tests/integration/test_real_project_smoke.py
git commit -m "feat: add real project smoke cli entrypoint"
```

---

### Task 5: Verification And Real yakDB Demo Run

**Files:**
- No required source changes.

- [ ] **Step 1: Run full verification**

Run:

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

Expected: PASS.

- [ ] **Step 2: Run the real yakDB demo command**

Run:

```bash
uv run python -m loom.examples.real_project_smoke /Users/huanggui/workspace/yakDB --smoke-command "uv run pytest -q"
```

Expected: command exits 0 and prints a markdown report mentioning yakDB purpose, smoke test result, CLI smoke result, repository state, and improvement directions.

- [ ] **Step 3: Commit final cleanup if needed**

If lint/format required edits:

```bash
git add src tests
git commit -m "chore: finalize real project smoke loop"
```

If no cleanup was needed, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage:
  - real project inspection: Task 1
  - deterministic smoke command: Task 2
  - yakDB CLI smoke path: Task 2
  - Loom loop traces/actions/observations: Task 3
  - default yakDB entrypoint: Task 4
  - real yakDB manual run: Task 5

- Safety:
  - target repo is read-only
  - temporary CLI smoke writes outside target repo
  - automated tests use temporary fixture projects

- Scope:
  - no TUI changes
  - no yakDB source changes
  - no live LLM dependency
