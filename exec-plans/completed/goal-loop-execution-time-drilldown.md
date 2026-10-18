# Goal-loop execution time and public process drilldown

Date: 2026-10-18. Design: [Goal-loop analysis, section 16](../../docs/deep-eval-goal-loops.md).

## Implemented

- [x] Deterministic `execution_view` over the pinned source and immutable call ledger.
- [x] Disjoint model-only / tool-only / overlap / gap time partition; unknown timestamps remain unknown.
- [x] Phase evidence windows separate related calls from other calls without changing ownership.
- [x] Recorded public statement → tool invocation → observation graph using exact run/trace/tool call IDs; archive-order links stay separate from causality.
- [x] Time annotations on phase nodes, full-run time summary and an entry for calls outside all phase windows.
- [x] Collapsible call groups with timestamps, duration, TTFT, TPS, output tokens, targets, public excerpts and source links.
- [x] Read-time upgrade of historical reports without evaluator calls or artifact mutation.
- [x] Synthetic tests for overlapping/nested calls, scope conservation, absent links, repeated tool IDs, citations, escaping and UI navigation.

## Validation

Read-only projection of the latest saved report reproduced its 997-second call span as 918 seconds model-only, 27 seconds tool-only, 28 seconds overlap, and 24 seconds gaps. All 20 tools linked to their recorded model invocation IDs. The first investigation window contains 26 calls, 4 explicitly related; 12 calls fall outside all phase windows. These facts do not reinterpret the old semantic graph.

An isolated headless Chrome rendered the saved report's overview and expanded investigation. The running Loom service and production artifacts were not modified. No model request was made.

Automated checks:

```sh
.venv/bin/python -m pytest -q tests/evaluation tests/service/test_semantic.py tests/service/test_goal_loops.py tests/service/test_trajectory.py tests/llm/test_structured.py
node --test tests/web/*.test.mjs
.venv/bin/ruff check src/loom/evaluation/goal_loops src/loom/service/semantic.py tests/evaluation/test_execution_view.py tests/service/test_goal_loops.py
git diff --check
```

The service must load the updated code before existing reports gain the read-time projection; no new Deep evaluation is required.

Results: **347 Python tests and 138 Web tests passed; Ruff and whitespace checks passed.**
