# Cross-step behavior evaluation

[中文](behavior-sequence-refactor.zh-CN.md)

## Design

Base records facts; Deep interprets behavior; Acceptance verifies deliverables; Evolve reuses supported diagnoses. A completed node or changed output does not establish task success. Offline analysis uses the immutable trace only.

- Capture plan/workflow snapshots including initial node activation. Normalize content/objective and nested workflow schemas. Bind execution to the plan active at request time, with explicit association provenance. Preserve ambiguous or missing associations.
- Record canonical resource paths, byte ranges and content hashes in filesystem observations; record before/after hashes for writes. Historical traces retain their actual limitations. Treat opaque shell commands as possible mutation boundaries.
- Detect repeated resource observations separately from redundant behavior. Consider intervening writes, truncation, retries and actual model visibility. Never infer continuous immutability from two matching snapshots.
- Build an activity signal timeline and low-progress candidates. Ten tool calls without a recognizable new result nominate a window, not a waste verdict. Deep supplies cited information gain, eliminated hypotheses, decisions, useful artifact changes, recovery and verification landmarks.
- Keep bounded cost-owning segments, but include neighboring steps and linked repetition endpoints in focused review. Context-only segments do not count as reviewed. Evidence must still be delivered through the evidence reader; existing budgets/checkpoints remain enforced.
- Merge graph nodes by exact provenance and kind; merge findings by mechanism and evidence, retaining multiple dimensions. Report endpoints per episode/problem with unique call ownership, known tokens and measured elapsed time. Overlapping endpoint costs must not be summed.

## Delivery plan

1. Repair plan extraction and temporal execution associations.
2. Add resource observations, repeat detection and activity signals.
3. Extend focused review with cross-step context, semantic landmarks and stable endpoint aggregation.
4. Add Web and CLI presentation; bump analyzer, prompt and session cache versions. Preserve old report readers.
5. Test temporal/goal boundaries, file mutations, truncation, opaque commands, parallel calls, evidence coverage and deduplication. Replay existing sessions offline without paid model calls.

Acceptance requires meaningful plan descriptions in existing traces, conservative repetition classification, source-linked progress records, bounded cross-step review and no double-counted findings/costs. No internal model reasoning or external file immutability is inferred. Arbitrary shell parsing and exhaustive semantic review remain outside the contract.

## Delivery evidence (2026-10-11)

All five implementation steps are complete. Relevant Python suites passed 545 tests and the Web suite passed 88 tests.
Final prompt/report and endpoint-display changes were followed by 53 focused Python tests and 13 focused Web tests.
Two existing sessions were replayed through the restarted service using `session-trajectory.4.1`; all 112 plan-node
snapshots in the multi-step sample now have descriptions, with 46 model calls linked to active plan nodes. A low-progress
candidate remains explicitly unjudged. The actual API result was rendered in a DOM check. Fixture judges verify bounded
cross-step context, progress persistence and checkpoint reuse; no paid model evaluation or semantic calibration was run.

## Subsequent budget fix and live resume (2026-10-11)

A live run stopped after 3 calls / 24,454 tokens / 13.9 seconds because the old scan-only 15% ceiling was exhausted.
Compact navigation, durable page manifests, shared total capacity with downstream reserves, budget-planned focus samples
and bounded synthesis quotes replace this failure mode. Saved scan pages retain their identity while only the unscanned
suffix is repacked. Relevant Python tests (367) and Web tests (88) passed.

The live run resumed without increasing its 24-call / 120,000-token / 600-second limits. Three saved scan stages were
reused and two additional pages scanned. It completed at 8 cumulative calls, 76,597 tokens and 86.76 seconds. Only 2/23
segments were focused, with one having any supported dimension and no supported behavioral diagnoses or progress landmarks.
These limitations remain explicit; pipeline completion does not establish adequate semantic coverage or absence of issues.

## Optional plan assessment validation (2026-10-11)

A focused segment with no bound execution plan could fail with `Invalid plan assessment`, including after a paid repair.
The prompt now explicitly requires an empty plan assessment list in this case; informal planning remains assessable under
`plan_quality`. Invalid optional plan rows are omitted individually, with field-level validation issues in the saved stage
and a limitation in the final coverage report. Other valid judgments survive without another model call. Evidence source
validation stays strict; future/inactive plans cannot support an adherence verdict. Existing saved stages remain reusable.
Live resume exposed the same failure mode in dimension assessments. Invalid scope/dimension/status/rationale entries are
also isolated, with any missing dimension retained as unknown. Valid sibling assessments and progress remain available;
rejected rows never count as supported conclusions. Diagnostics identify the entry and invalid fields in saved stages.

Validation: 384 related Python tests, 88 Web tests and Ruff passed. After service restart, `evaluation_b728…` resumed
within its original limits and completed all four selected segments at 9 cumulative calls / 91,358 tokens / 205.76 seconds.
The final successful responses needed no item omissions, but none of the four segments had evidence-supported dimension
verdicts, diagnoses or progress landmarks. The formatting failure is resolved; semantic usefulness remains limited.
