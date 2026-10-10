# Trace analysis: Base evaluation

Base evaluation is a deterministic report over an immutable trace snapshot. It performs no model requests and reads no current workspace files. Deep evaluation judges intent, planning, progress and causal effectiveness separately.

The report has five sections, with all-source and per-run views:

1. Basic information: raw and analyzed event counts, logical steps and execution attempts, calls, input/output/total tokens, and runtime acceptance checks.
2. Tools: grouped by tool, declared mapping of capability family, and recorded purpose; outcomes, failure reasons, timing distributions, truncation, explicit retries and identical-input repetitions.
3. Runtime: lifecycle intervals and the disjoint model-only, tool-only, overlapping and unattributed portions of recorded running time.
4. Models: per-model, purpose and recorded-stage calls, durations, token totals, usage coverage and per-call distributions.
5. Outputs and completeness: recorded artifacts and workspace paths, interruption records, missing usage/durations/artifacts, unscoped calls and clock anomalies.

## Accounting contract

- `base.statistics` uses `loom.base-facts.v1`. `base.metrics` remains a compatibility view; Web and CLI reports consume the same statistics. Behavior/Evolve bundles retain this factual baseline alongside semantic findings.
- Service raw event counts include streaming events; analyzed counts exclude stream and token-delta events. Both are pinned to the analysis cursor. Offline analysis counts the events present in its source.
- A logical step is identified by run, trace and step number. Starting that step again after resume increases attempts, not logical step count. Unknown or missing identities are reported separately from measured calls.
- Tool results use explicit errors, cancellation, timeouts, rejection and command exit status. A recorded `no_match` result is not a command failure. An interrupted operation with uncertain effects is unknown, not success. Success rate is `success / (success + failed)`; no resolved calls yields null.
- Timeout is a failure reason, not an extra outcome to add to failure counts. Identical input is a repetition fact, not proof of retry, wasted work or lack of progress. Retry counts require an explicit `retry_of` association.
- Provider token usage is counted once per reconstructed call. Missing/conflicting usage is not zero. Reported input, output and total coverage are independent; known sums do not imply complete billing data.
- Call duration uses an explicit nonnegative duration when available, otherwise paired timestamps. Missing/reversed endpoints remain unmeasured. P95 uses nearest rank among measured calls and reports its sample count.
- Lifecycle time is reconstructed from recorded transitions. Run durations are summed; the session span is shown separately because concurrent runs and gaps differ. Running time excludes recorded pause, input-wait and queue intervals.
- Activity distribution is a timestamp interval sweep within recorded running intervals. Overlap includes nested calls (for example a finish tool awaiting verification), not just parallel work. Cumulative call durations can exceed wall time. Acceptance is a purpose dimension and must not be added again to model/tool time.
- Unattributed time is not assumed to be framework overhead. Clock reversals, missing timestamps, missing call endpoints and incomplete usage remain visible. Provider-internal retries, precise first-token latency, cache/reasoning breakdown and monetary cost are not currently calculated.
- Acceptance shows recorded conditions, reasons and method; a semantic check remains a model judgment. An evidence artifact is not itself a passed verdict. Prior semantic outcome fields remain available to Deep evaluation without a second unverified headline in the factual UI.

The shared implementation lives in `evaluation/base_metrics.py`; token accounting uses `evaluation/token_ledger.py`. The service version and behavior analyzer version change when these semantics change, so cached reports are regenerated on the next analysis request. Existing saved reports remain readable.
