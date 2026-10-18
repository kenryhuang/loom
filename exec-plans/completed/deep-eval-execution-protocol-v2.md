# Goal-loop evaluation execution protocol v2

Date: 2026-10-17. Status: implemented; automated regression complete.

Design and usage: [Goal-loop analysis, section 15](../../docs/deep-eval-goal-loops.md#15-执行协议-v22026-10-17).

## Problem and outcome

The first evaluator used one large patch contract for structure, reconciliation and diagnosis. Reconciliation could recreate phases and diagnoses, existing IDs could only be rejected as new keys, and output truncation retried the same oversized task. The observed job spent 39 calls and 536,630 reported tokens before exhausting its budget.

The new scheduler restricts each stage to its own operations. Structure builds or updates entities; reconciliation only resolves explicit duplicate candidates; diagnosis only emits cited findings. No candidate means no reconciliation model call. Truncated output creates smaller durable work units instead of a same-sized response repair.

## Completed work

- [x] Separate schemas, prompts, output limits and read limits for each stage.
- [x] Explicit entity updates with immutable boundaries, exact citations, deduplicated call additions and amendment history.
- [x] Deterministic same-parent duplicate candidates; evidence-validated merge/distinct/unresolved decisions.
- [x] Durable request splitting, exact character-range subdivision, bounded depth and explicit unreviewed gaps.
- [x] Preserve validated partial results and reported costs across budget stops, cancellation and resume.
- [x] Upgrade stopped `goal-loops.1` checkpoints without rescanning completed structural batches; archive old downstream work.
- [x] Expose protocol, split requests, coverage gaps, skipped coordination and diagnostic scope in the existing UI.
- [x] Document compatibility and the distinction between pipeline completion and source coverage.

The engine identifier and exported bundle schema remain unchanged. Analyzer/prompt versions are `goal-loops.2` / `goal-loops-rubric.2`. Legacy completed results remain readable. v3 remains the default engine; this change applies to Goal loops.

## Validation

Commands:

```sh
.venv/bin/python -m pytest -q tests/evaluation tests/service/test_semantic.py tests/service/test_goal_loops.py tests/service/test_trajectory.py tests/llm/test_structured.py
node --test tests/web/*.test.mjs
.venv/bin/ruff check src/loom/evaluation/goal_loops src/loom/llm/structured.py src/loom/service/semantic.py tests/evaluation/test_goal_loop_protocol.py tests/evaluation/test_goal_loops.py tests/service/test_goal_loops.py tests/llm/test_structured.py
git diff --check
```

Results: **341 Python tests passed, 136 Web tests passed, Ruff and whitespace checks passed.**

An additional read-only offline replay used an in-memory copy of the existing production checkpoint and a mock evaluator. All 23 structural batches, 24 phases and reported usage were retained. No reconciliation request was scheduled; only one diagnostic request remained, with 26,439 input characters. This verifies scheduling and migration, not real-model latency or diagnostic quality. No production checkpoint was written and no paid model request was made.

## Deployment and limits

The running service has not been restarted. After updating the service, Resume can migrate the stopped old job. Existing elapsed time and usage remain charged, so an exhausted total budget must be raised before resuming. Completed jobs with atomic review gaps stay explicitly partial; they are not silently retried forever.

Real-model semantic quality, duplicate-candidate recall and model-specific budgets still need calibration. The implementation does not promise complete explanation from partial diagnostic windows or convert involved cost into estimated savings.
