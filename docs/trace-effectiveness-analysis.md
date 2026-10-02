# Trace effectiveness analysis

`loom.evaluation.analyze` defaults to v2 on the CLI. It reads a recorded trace;
it does not replay the agent's tools or execute the commands found in the trace.

## Usage

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/your-trace.jsonl \
  --out-dir .loom/evaluation/your-analysis
```

Without `--judge`, the output contains task definitions, chronology, context
deltas, tool results and injection evidence, token usage and observed verification
evidence. Semantic dimensions are `not_evaluated`; task criteria are `unverified`.

```bash
uv run python -m loom.evaluation.analyze \
  --trace-path runs/your-trace.jsonl \
  --out-dir .loom/evaluation/your-analysis \
  --judge --config config.yaml --model your-named-model --stream
```

`--task 'original task text'` supplies task information explicitly, with its
provenance separate from trace-derived requirements. `--tui` displays analysis
events. `--stream` streams evaluator output when the provider supports it.

The evaluator diagnoses five dimensions:

- `context_effectiveness`: information needed for each decision, additions,
  removals, retained constraints, source coverage and result injection.
- `tool_effectiveness`: the need, tool/schema, arguments, outcome and subsequent
  use, including effects that cross step boundaries.
- `loop_progress`: new facts, useful negative results, artifact changes,
  verification, blockers, repetitions and task drift.
- `token_efficiency`: measured input/output cost related to progress, with
  testable optimization hypotheses instead of a high-token penalty.
- `verify_gate`: task requirements mapped to actual checks, oracles, results,
  final claims and unresolved coverage/version limitations.

## Evidence budgets

| Option | Default | Meaning |
| --- | ---: | --- |
| `--judge-batch-rounds` | 8 | Source rounds per chronological analysis batch |
| `--judge-max-read-rounds` | 6 | Evidence expansion exchanges per batch/synthesis |
| `--judge-max-evidence-chars` | 80000 | Total source preview and expansion characters |
| `--judge-max-prompt-chars` | 100000 | Maximum system plus conversation characters per call |

Initial evidence previews include explicit lengths and truncation. The model can
request original fields and character ranges from the registered source snapshot.
Each response provides `returned_ref`, the exact delivered range to cite.
References must resolve and belong to the relevant task/run scope. A valid
reference to unread text is insufficient: diagnoses exceeding delivered ranges
become `unknown`. Budget exhaustion produces explicit incomplete coverage.

The analyzer uses compact navigation indexes and splits oversized batches into
smaller chronological batches. A single round still exceeding the prompt budget
is reported as unevaluated. Increasing a character
budget does not guarantee it fits a particular provider's token context limit.

## Artifacts

`evaluation-bundle.json` declares `loom.evaluation.bundle.v2`, the source SHA-256,
analyzer and prompt versions, configuration, factual/semantic coverage, analyzer
usage and paths to:

- `task-contracts.jsonl`, `trajectory.jsonl`, `context-deltas.jsonl`.
- `round-analyses.jsonl`: each round's before/after state, intent, action,
  progress and five-dimensional assessments, with decision-time/later references.
- `tool-uses.jsonl`, `token-ledger.jsonl`.
- `verification-evidence.jsonl`, `verification-coverage.jsonl`.
- `diagnoses.jsonl`, `preserved-behaviors.jsonl`, `verification-framework.jsonl`.
- `evidence-index.jsonl` and `report.md`.

The proposed verification framework has `executed=false`. Observed command success
only supports its recorded scope; neither `run.completed: pass` nor printed
`PASSED` establishes task verification. Missing criteria, assertions, version
binding or source coverage remain explicit limitations. Model confidence is
self-reported, not a calibrated probability. Token sums cover reported usage;
missing values are unknown and evaluator usage is reported separately.
Supported verification requires an explicit oracle and complete coverage of the
original criterion. Execution applicability to the final artifact must be grounded
in recorded version evidence; a model's `freshness=confirmed` cannot override an
unknown binding or later edits. Historical traces without this evidence remain
unverified even when commands passed.

## Compatibility

```bash
uv run python -m loom.evaluation.analyze \
  --analysis-version v1 --trace-path runs/your-trace.jsonl \
  --out-dir .loom/evaluation/legacy
```

Python callers use `EvaluationConfig(..., analysis_version="v2")` to select v2;
the existing Python default remains v1 for downstream compatibility.
The current evolution bundle loader explicitly rejects v2. Wiring diagnoses into
optimization is a subsequent integration, not an implicit score conversion.

Shared reconstruction bug fixes also apply to v1: source chronology is preserved,
JSON action tools are associated, and colliding identities are disambiguated.
The v1 field schema remains unchanged; historical output order and previously
colliding IDs are not preserved as defects.

Protocol tests use deterministic providers to check evidence access, budgets,
parsing and verification guards. They do not establish a real model's diagnostic
precision. Model-specific calibration requires comparing generated diagnoses
with the human-reviewed examples in
[the sample review](analysis/2026-09-05-trace-effectiveness-sample-review.md).
The first real-model smoke trial remained incomplete and missed a known diagnosis;
see [the validation record](analysis/2026-09-05-trace-effectiveness-validation.md).
Semantic accuracy has not passed the release gate; these outputs require review
before guiding automatic optimization.
