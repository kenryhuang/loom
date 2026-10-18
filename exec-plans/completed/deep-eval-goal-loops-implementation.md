# Goal-loop analysis implementation

Date: 2026-10-17. Status: implementation and automated regression complete.

Source: [design and usage](../../docs/deep-eval-goal-loops.md).

- [x] S1 Fixed-source single-run adapter, structured patch contracts and exact evidence pointers.
- [x] S2 Persistent Goal / Attempt / Phase extraction, paged entity catalogue, bounded reconciliation and aliases.
- [x] S3 Exclusive cost atoms, known shared vs uncertain/unassigned vs later reuse, interval union and conservation.
- [x] S4 Service version dispatch, explicit run scope, budgets, cancellation, partial results and durable resume.
- [x] S5 Reasoning efficiency goal tree, occurrence graph, call timeline, evidence/details, exports and Deep controls.
- [x] S6 Cited diagnostics, explicit diagnostic windows and deterministic/scripted scenario regression.

## Validation

```sh
.venv/bin/python -m pytest tests/evaluation tests/service/test_semantic.py tests/service/test_goal_loops.py tests/service/test_trajectory.py -q
node --test tests/web/*.test.mjs
.venv/bin/ruff check src/loom/evaluation/goal_loops src/loom/service/semantic.py tests/evaluation/test_goal_loops.py tests/service/test_goal_loops.py
git diff --check
```

Results: **313 Python tests passed; 135 Web tests passed; Ruff and whitespace checks passed.**
The new suites contain 14 evaluator tests, 2 service tests and 5 Web tests. Browser rendering was also checked in an isolated headless Chrome fixture, without a production service or evaluator.

Cases include exact range paging, original source/run isolation, complete-call review coverage, conflicting usage, overlapping timestamps, shared ownership, later reuse, ambiguous ownership, invalid schema/domain entry isolation, future-support rejection, merge aliases, mid-stage resume, cancellation, persistence and compatibility. A scripted already-indexed-document scenario keeps retrieval, repeated operation, verification and framework submission separate without automatically declaring waste.

## Delivery and remaining calibration

Select **Goal loops · Execution & cost** in Deep evaluation settings and choose Run scope; open the result in Reasoning efficiency. Runtime tasks and acceptance are unchanged. Historical v2/v3 readers and compatible resumes remain available; v3 remains the default.

The implementation uses one bounded reconciliation pass, reports unresolved/unknown structure, and diagnoses bounded windows after the structural pass. Complete means the selected pipeline finished, not perfect semantic reconstruction. The first bundle embeds facts and the cost ledger; phase change fields are bounded text. The detailed differences from the draft contract are documented in design section 13.

Real-session goal-boundary accuracy, diagnostic usefulness and model-specific budgets still require annotated samples and human review before changing defaults. No real evaluator was called, no production task was changed, no Evolve proposal was applied, and the running service was not restarted.
