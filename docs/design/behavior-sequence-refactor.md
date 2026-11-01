# Cross-step behavior evaluation

[中文](behavior-sequence-refactor.zh-CN.md)

Implemented extension: [Reasoning efficiency process graph](reasoning-efficiency-graph.md), covering acquisition windows, evidence adoption, cause/conclusion latency and visualization. See its implementation notes for the new tab and versioned contracts.

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

## Evidence delivery audit and live validation (2026-10-11)

The implementation status above describes code and contract coverage, not calibrated semantic quality. An audit of
`evaluation_78e4971f311346499aa8dcd6960f0f53` found that the planet-simulator task had a recorded goal and passed all four
runtime acceptance checks (model judgment), while the UI displayed the absence of separate independent verification
receipts as “Goal acceptance: Not assessed”. These are now separate fields, with the actual acceptance conditions/results
shown only for the matching episode and after the applied goal.

The original deep review completed but had no supported dimension judgments: five assessments were omitted, three had
no citations, and two cited whole events when only prefixes had been delivered. Focus had received 23 packets; the
synthesis stage's claim that no evidence was delivered was incorrect. Synthesis previously omitted dimension assessments,
progress and plan adherence, keeping only diagnoses and preserved behaviors.

Analyzer `behavior-evaluation.3.6` / rubric `behavior-rubric.3.4` now:

- Prefer model response content, tool arguments and actual output fields over repeated request/event envelopes; share the
  bounded initial evidence allowance across source fields so later actions are represented.
- Assign stage-local evidence IDs resolved to exact delivered character ranges. Raw pointers remain compatible; unknown
  IDs, wrong sources and unread ranges cannot support a judgment.
- Request all five dimensions explicitly; retain distinct reasons for missing/invalid answers, missing citations, unread
  citations and evaluator uncertainty. Rejected proposed claims remain diagnostic data, never supported conclusions.
- Carry assessments, progress, plan judgments and their cited evidence into synthesis, with actual focus packet counts.
- Correct the toolbar alignment and separate runtime acceptance from independent verification in the dashboard.

The same immutable trace was evaluated again using the same model and original resource limits. Historical results were
retained; the new contract starts a new evaluation instead of silently reinterpreting saved judgments.

| Measurement | Original | New `evaluation_f44164312c814c91a712013c3d61d954` |
| --- | ---: | ---: |
| Reviewed segments | 2/2 | 2/2 |
| Supported dimension judgments | 0/10 | 8/10 |
| Cited progress landmarks | 0 | 5 |
| Plan adherence assessments | 2 (one unknown) | 2 followed |
| Evaluator calls | 4 | 4 |
| Reported tokens | 39,382 | 48,597 |
| Elapsed seconds | ~76 | 52.70 |

The remaining two unknown judgments are specific: the terminal reporting segment has no investigation to assess, and the
investigation segment has no recovery opportunity. The final reporting segment does contain a recovery: after
`complete_node` was rejected, the agent switched to `finish` and submitted the report. Progress now includes locating the
source, reading orbital parameters/mechanics, calculating speeds, checking tests/time scaling and delivering the report.

The new report has ten graph nodes/eight edges but no diagnoses or Evolve proposals. All seven causal/visibility endpoint
metrics still lack qualifying endpoints. In particular, historical tool injection links based only on content matching
cannot establish the strict visibility identity needed for evidence-adoption latency. A task that checks orbital realism
also need not establish a defect/root-cause/repair chain. These remain unmeasured; absence of a diagnosis is not proof
that no improvement is possible. This single live regression demonstrates a functioning evidence-to-judgment path, not
broad semantic calibration or reduced token cost.

Validation: 388 related Python tests, 95 Web tests, Ruff, and desktop/mobile browser rendering. The restarted service
completed the live evaluation within the original 24-call / 120,000-token / 600-second budget.

## Deterministic observations and semantic interpretation (2026-10-11)

Implemented scope: the first two layers below. Experiment execution and automatic application of Evolve proposals remain outside this change.

### Computed facts, independent of Deep evaluation

`behavior_observations.py` extends the sequence contract to `loom.behavior-sequence.v2`:

- Execution binds to the accepted/completed snapshot effective at request time and in the applicable goal scope. Proposed snapshots cannot override an effective plan; an accepted empty plan clears its nodes. Unknown or ambiguous bindings remain explicit. Historical traces that first expose the goal in a request retain an explicitly marked inferred goal association.
- Per-call checks record unmet declared dependencies and activity after declared node completion. Per-goal node records expose the presence/absence of recorded calls. These checks do not claim that declared completion proves deliverable correctness or that an unused branch was required.
- Compare actual returned payloads from arbitrary tools, including composite shell commands, excluding volatile duration/command metadata. Record input equality separately. Resource path/range/version comparisons retain their stronger identity and mutation checks. Identical output from different inputs is still an observation, not automatically a suspicious retry.
- Report resource identity coverage and comparable-result coverage separately. Unknown file identity does not become a zero-repeat conclusion. Repeated-call time and associated model tokens use unique tool/call ownership; missing durations/usage remain counted. Associated model tokens are not attributed wasted tokens.
- Preserve recorded content observations, explicit decisions/control actions, plan transitions, file changes and verification statuses/assurance. Verification linked to a different recorded goal revision cannot supply the current goal's first-pass milestone.
- Calculate first recorded content, first explicit decision/control action, first passed check and acceptance-pass milestones, including cumulative calls/tokens/elapsed time. No semantic model is needed. Absence of an explicit decision event does not mean no reasoning occurred.
- Calculate adjacent observation intervals with exclusive previous endpoints. Prefix indexes avoid repeatedly scanning the trace or copying cumulative call-ID arrays into every observation. “No new recognizable result” windows remain candidates, not proofs of no useful progress.

### Deep interpretation of those facts

Each focus receives a bounded selection of at most eight source-linked facts, with included/available coverage. It returns `fact_assessments` for necessity of repetition, justification of deviations, relevance of observations, use of evidence in decisions and applicability of verification. Counts, hashes and timestamps remain host-computed.

Judgments cite the relevant recorded action; plan text alone cannot substantiate execution. Repetition judgments require both endpoints. Same-action responses and tool inputs/results are valid sources for plan/control-action interpretation. Missing judgments and unsupported citations remain unknown with distinct reasons. Progress/diagnosis references link back to fact IDs through validated source citations; this provenance link does not establish causality.

Terminal verification events now reach focus context even without a solver round ID and can anchor progress in the final segment. Context-only facts do not increase review coverage. Base and Deep Web views and the CLI report expose the two layers separately; old bundles remain readable.

A live regression initially exceeded the synthesis prompt limit after adding detailed facts. Summary inputs now compact local judgments, bound source quotes, and report summary input/quote coverage. Full results remain in checkpoints/bundles, including any entries outside the bounded summary. No total budget increase was needed.

### Validation

Analyzer `behavior-evaluation.3.8`, rubric `behavior-rubric.3.6`, session cache `session-trajectory.4.3`.
Live evaluation `evaluation_59a5246a085741aa91d7e9a482b69687` completed in 4 calls / 60,026 reported tokens / 90.06 seconds under the original 24-call / 120,000-token / 600-second limits. All 16 selected fact judgments reached synthesis: 15 had valid supporting citations, one remained unknown because its citations did not cover the relevant observation. Eight of ten dimension judgments were supported; all six semantic progress landmarks linked to recorded facts. The report identified the final-node `complete_node` misuse and the subsequent `finish` recovery. Citation validation is not an independent calibration of semantic correctness.

For that same trace, Base alone computes four milestones (source lines 38, 68, 82 and 86), six plan-bound solver calls and zero unmet declared dependencies. It compares four of five completed tool results and explicitly reports zero resource identities across six solver tool attempts. Composite-output comparison is tested with repeated content; this trace itself contains no detected repeats. No task/environment experiments were executed.

Validation includes 400 related Python tests, 97 Web tests, Ruff, and desktop/mobile rendering of the actual result. Regression cases cover proposed/empty/stale plans, goal boundaries, equal composite-command results with different inputs, missing usage, both-endpoint citations, terminal verification, immutable checkpoints, bounded synthesis and indexed interval accounting.
