# Real Project Smoke Loop Design

## Purpose

The current Loom demos are intentionally small and mostly deterministic. They
show loop mechanics, but they do not demonstrate Loom handling a real project
with real repository state, real commands, real warnings, and a useful
engineering report.

This design adds a real project smoke audit demo. The primary example is:

```text
/Users/huanggui/workspace/yakDB
```

The demo should inspect that project, run a smoke test, run a real CLI path, and
produce a concise engineering report with project purpose and improvement
directions. It should do this through a Loom loop so the run produces ordinary
observations, decisions, actions, and traces.

## Goals

- Provide a non-toy example that uses a real local project.
- Exercise Loom's runtime, tool calling, trace, and reporting patterns.
- Run actual smoke commands instead of returning mocked observations.
- Produce a useful report for a developer:
  - project purpose
  - repository status
  - smoke test result
  - CLI smoke result
  - risks and improvement directions
- Keep the target repository read-only except for temporary smoke workspaces
  outside the target tree.
- Make automated tests independent of the user's local yakDB checkout.

## Non-Goals

- Do not fix yakDB.
- Do not modify files inside the target repository.
- Do not require a live LLM provider for the default demo.
- Do not make this a TUI-first demo.
- Do not depend on `/Users/huanggui/workspace/yakDB` in CI tests.
- Do not build a generic benchmarking framework.

## Existing Context

Loom currently has:

- a counter demo in `loom.tui.demo`
- an LLM demo with a fixed `search-notes` mock tool
- example factories in `loom.examples.factories`
- runtime tracing and event capture
- a tools layer for bounded tool affordances

The real yakDB exploration found:

- yakDB's stated purpose is an AI-native file database: parse files once, then
  read/search/glob them through CLI, REST, MCP, or Python APIs.
- `uv run pytest -q` passes in the local checkout.
- The test suite emits warnings, including watcher coroutine lifecycle warnings.
- A real embedded CLI flow works:
  - `yakdb init`
  - `yakdb index`
  - `yakdb grep`
  - `yakdb read`
- The CLI smoke showed `.yakdb/blobs/.../original.txt` in grep results, which
  suggests internal YakDB storage can pollute search output unless excluded.
- The local yakDB checkout can be dirty; the demo must report that state instead
  of assuming a clean tree.

## Recommended Shape

Create a new module:

```text
src/loom/examples/real_project_smoke.py
```

It exposes a deterministic Loom loop and a CLI-friendly entrypoint:

```bash
uv run python -m loom.examples.real_project_smoke /Users/huanggui/workspace/yakDB
```

The loop should run one audit step that calls real local tools and appends a
final report observation.

## Architecture

### Components

```text
RealProjectSmokeConfig
  target_path
  pytest_command
  cli_smoke_enabled
  command_timeout_seconds

ProjectInspector
  reads README/pyproject/file layout/git status
  infers purpose and tech stack

SmokeRunner
  runs pytest or configured smoke command
  captures exit code/stdout/stderr/duration

CliSmokeRunner
  runs project-specific CLI smoke when yakDB is detected
  uses a temporary directory outside the target repo
  captures command outputs

ReportSynthesizer
  turns observations into a structured markdown report

Loom loop
  records inspect/test/cli/report actions and observations
```

### Data Flow

```text
target project path
  -> inspect project
  -> run smoke test
  -> run yakDB CLI smoke if applicable
  -> synthesize report
  -> StepResult with report output
  -> trace contains all actions and observations
```

### Loop Behavior

The loop should complete after one step. This is an audit loop, not a
multi-step autonomous repair loop.

The step records four logical actions:

1. inspect project
2. run smoke tests
3. run CLI smoke
4. synthesize report

Each action should have a corresponding observation. The final `StepResult`
output is the markdown report.

## Report Format

The generated report should be deterministic markdown:

```markdown
# Real Project Smoke Audit: yakDB

## Purpose

...

## Repository State

...

## Smoke Test

...

## CLI Smoke

...

## Improvement Directions

1. ...
2. ...
3. ...
```

The yakDB default case should surface these improvement directions when the
evidence is present:

- Exclude `.yakdb/` internal storage from grep/search/index paths by default.
- Clean up watcher lifecycle warnings around `_consume_queue`.
- Keep README, CLI behavior, and package extras aligned around embedded mode.

The report should separate observed facts from inferred recommendations.

## Error Handling

The demo should degrade into a report instead of crashing whenever possible.

- Missing target path: return `Result.err(VALIDATION_FAILED)`.
- Missing README/pyproject: report as unavailable, continue.
- Git unavailable or target is not a git repo: report as unavailable, continue.
- Smoke command timeout: record timeout observation and mark smoke as failed.
- Smoke command non-zero exit: record output and mark smoke as failed.
- CLI smoke unavailable: report skipped with reason.

Hard failures should only occur when the target path is invalid or required
local execution primitives are unavailable.

## Safety

The target repository must be treated as read-only.

Allowed:

- read files
- run configured smoke commands
- run `git status`
- create temporary smoke workspaces under the system temp directory

Not allowed:

- edit target files
- delete target files
- run cleanup commands inside target
- modify git state in target
- write reports into target by default

If a smoke command creates normal project artifacts, the report should mention
new dirty files rather than deleting them.

## Testing Strategy

Automated tests should not depend on the local yakDB checkout.

Use temporary fixture projects that mimic enough shape to test:

- README purpose extraction
- pyproject command inference
- git status reporting when not a git repo
- successful command capture
- non-zero command capture
- report synthesis
- Loom loop output and trace content

The real yakDB path is a manual/demo path documented in the module or README.

## Public API

The module should expose:

```python
RealProjectSmokeConfig
run_real_project_smoke(config) -> Result
make_real_project_smoke_loop(config) -> MinimalLoopDefinition
make_real_project_smoke_context(config) -> Context
```

The module can also provide a `main()` function for:

```bash
python -m loom.examples.real_project_smoke <path>
```

## Design Decision

Implement the yakDB case as a full Loom loop, not as a plain script. This keeps
the demo aligned with Loom's core model:

- real actions
- real observations
- traceable command execution
- deterministic report output
- no dependency on live LLMs

This creates a credible real-world example while keeping the first
implementation small and safe.
