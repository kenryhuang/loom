# Incremental Deep Eval adaptation

The 2026-10-19 design is the target; this checkout currently has `goal_loops_v1`
(`structure → reconcile → utility → diagnose`), not the modules named in the design's
implementation notes. Preserve existing user edits and legacy evaluations.

Implement a separately versioned `goal_graph_v1` engine using the current
EvidenceStore, run facts, service jobs and cost ledger:

1. Compile immutable action cards and a full source index; distinguish captured,
   presented and adopted evidence. Do not send raw results or request histories by default.
2. Review bounded chronological batches with bounded relevant goal/stage state.
   Accept atomic groups of goals, stable stages, per-card contributions, relations,
   questions/closure and diagnostics; generate occurrence IDs on the host.
3. Validate exact delivered references, knowledge cutoffs, hierarchy and forward
   dependencies before publishing a revision. Retain conflicts without blocking
   unrelated batches. Resume only unfinished work; no outline/cycles gate.
4. Bound targeted reads and format repair; reserve estimated input plus maximum
   output before each request. Keep unknown usage reservations and saved responses.
5. Project an expandable occurrence graph and Markdown report from the same
   revision; retain unique call accounting and authoritative acceptance records.
6. Wire service/default selection and web graph/report/details; keep legacy readers.
7. Test atomicity, temporal isolation, stable IDs, coverage, budgets, resume,
   source visibility, accounting and UI; update the design's implementation claims.

Semantic quality on the private run_40b063 trace must not be claimed without a real
evaluation. Missing sample data is a calibration limitation, not a reason to fabricate results.

## Delivery and verification

- Steps 1–6 implemented in the new goal graph engine and web renderer.
- Step 7: 386 related Python tests, 144 browser tests, Ruff and diff checks pass.
- Existing local trace: 123 events → 36 cards → 5 fake-provider requests, max
  10,720 prompt characters, conserved costs; no real evaluator calls or DB writes.
- Service restarted while idle (PID 20655). HTTP checks confirmed the new catalog
  default, graph asset (200), and preserved legacy artifact availability.
- The design now maps actual repository paths and separates implemented scope
  from future goal aliasing, rich acceptance projection and real-model calibration.
- The manual run_40b063 source is absent locally. Its report remains the product
  quality target, not claimed as an automatically reproduced result.

## Input cost audit follow-up

- Added goal-cards.2 / goal-graph.2: bounded metadata views, field-only boundary
  extraction, compact presentation history and actual-delivery reference checks.
- Native output schema now participates in input admission and token reservation.
- Persist request component profiles; expose them beside reported provider usage.
- Removed whole-checkpoint copies from context selection and repeated tool-ID scans.
- Offline native-schema sample: 36 cards, 5 calls, 78,192 total input characters,
  maximum 20,966 per call. Fake provider only; no billed-token claim.
- 394 related Python tests and 21 related browser checks pass; details in §15.

## Skeleton / Report presentation

- Bottom results now expose only Skeleton and Report tabs.
- Added deterministic dependency-ranked DAG layout, shared G/S display IDs and
  goal/stage inspectors; preserve cross-goal references without copying costs.
- Root scope edges are distinct from semantic result edges; no inferred sequence
  arrows or fake completion. Report and evidence stay local and read-only.
- Browser surfaces are unavailable in this environment; validation uses DOM and
  geometry tests, including the sample's branching/join topology.
- Validation: 146 frontend tests and 10 incremental-graph backend tests passed;
  the idle service was restarted and both graph modules passed HTTP smoke checks.

## Token policy correction

- The real 0/40-card run exhausted the fixed 2,600 output-token cap on all ten
  responses. Removed graph-specific output and cumulative token stopping gates;
  retained time/call limits and actual/unknown usage accounting.
- Kept bounded metadata batches and selective evidence reads; compact delta-only
  output avoids repeated root declarations and unnecessary reference lists.
- Rubric .3 can explicitly upgrade .2 checkpoints without losing accepted work.
  Legacy evaluators retain their budget behavior; graph UI hides the token budget.
- Validation: 75 related Python tests and 146 frontend tests passed, including
  uncapped provider requests, >2,600 reported output tokens, old-checkpoint resume,
  unknown usage, bounded metadata and exclusion of raw trace/tool result bodies.

## Independent action review and incremental relations

- Upgrade to goal-graph.3 / rubric.4. Split intrinsic assessments, attribution,
  comparisons and semantic edges into independently committed records. Keep exact
  sequence/caller metadata separate from inferred result dependencies.
- Text providers return typed JSONL; retain complete lines around invalid records.
  Native JSON keeps flat arrays. Legacy group responses remain readable without
  group-wide rollback. Persist per-response validation failures and batch aliases.
- Add a bounded prior-action index for later action → S → G attribution and
  result-use links. Reuse accepted historical evidence internally while validating
  all newly asserted references against actual current delivery.
- Resume pending batches by trace cutoff, preserve valid work on timeout and show
  the actual failure. Existing .2 checkpoints can upgrade without losing judgments.
- Read-only replay of one saved real response retained one reviewed assessment
  and five needs-evidence assessments; invalid graph declarations/links stayed
  rejected. No billed calls or production checkpoint rewrites were performed.
- Validation: 71 related Python tests, 146 frontend tests, Ruff and diff checks
  passed. Cross-window attribution tests also cover evidence retained from earlier
  summary pointers and rejection of undelivered new relationship references.
