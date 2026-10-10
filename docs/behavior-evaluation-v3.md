# Behavior evaluation v3

[中文说明](behavior-evaluation-v3.zh-CN.md)

V3 separates **Base evaluation** (recorded calls, steps, tokens and independently verified goal outcomes) from **Deep evaluation** (intent, plan quality, progress, investigation efficiency and recovery). It is the default for new Web/service evaluations. Human calibration gates in the [design](superpowers/specs/2026-10-08-behavior-evaluation-evolution-design.md) remain pending; the default change is an operational choice, not a claim of measured accuracy or savings.

## Use it

In Web, open a session's trajectory, then **Deep evaluation → Evaluator settings → Analysis version → v3**. Choose a judge model and budgets. Base analysis also shows the goal/plan timeline and acceptance outcomes. Existing saved v2 analyses retain their original interpretation.

Offline, use the same pipeline:

```bash
uv run python -m loom.evaluation.analyze --trace-path runs/task.trace.jsonl \
  --analysis-version v3 --out-dir .loom/evaluation-v3

uv run python -m loom.evaluation.analyze --trace-path runs/task.trace.jsonl \
  --analysis-version v3 --judge --config config.yaml --model kimi_judge \
  --judge-max-calls 40 --judge-max-tokens 300000 --judge-max-segments 24 \
  --out-dir .loom/evaluation-v3
```

Use your own configured model alias. `--tui` displays evaluator activity. The output contains a self-contained `evaluation-bundle.json`, `report.md`, immutable source trace, hashed factual ledgers and `behavior-checkpoint.json`. Reuse the same output directory to resume the same snapshot/model/settings; increase resource budgets if needed. Use a new directory when changing source, model or analysis settings.

The service accepts `analysis_version: "v3"` in the existing evaluation-start JSON body. Its catalog advertises `analysis_versions`; the default is v3. Pass `analysis_version: "v2"` explicitly for the legacy pipeline. For v3, `max_rounds` is the maximum number of **focused segments**, not a prefix of model calls. The whole source index is scanned before focus selection. A segment currently groups up to four contiguous calls within one step and goal revision. These are deterministic review windows, not claims that the calls formed one causal action.

## Model and time budgets

Set `evaluation_model: judge` at the top level of `config.yaml` (or TOML), where `judge` is an existing entry under `models`. This selects the evaluator independently of `default_model`. Without it, the default model alias is reused with the evaluator's bounded generation profile. You can override the judge in Web.

New Web/service jobs default to 24 calls, 120,000 reported tokens, 600 seconds, 8 focused segments, one evidence follow-up per stage, and a 120-second per-call timeout (`max_call_seconds`). These are independent of task budgets. The non-streaming evaluator transport has a socket timeout and does not make event-loop shutdown wait for cancelled blocking HTTP requests. A remote request may still run after local cancellation; its unreported usage stays unknown.

20% of the total time budget is reserved for synthesis. A timed-out focused call stops further focus work and attempts to summarize saved findings within the remaining budget. The progress area separates scanning, focused review and synthesis, with evidence reads, output correction and per-stage model time. Validated local results are visible during execution and after interruption; pending selected segments and segments outside the review are reported separately.

Saved v2 jobs keep their original version and settings. **New behavior evaluation** prepares a fresh v3 job; resuming v2 reuses only v2 batches. Existing v3 checkpoints from an older prompt version remain readable but require a new evaluation after the rubric changes. Resume keeps the model and analysis settings fixed; total calls, tokens and time may increase.

## Interpret results

- Goal states distinguish accepted input, applied input and actual appearance in a recorded model request. Ambiguous natural-language corrections retain the previous requirements and stay unknown until a complete effective goal is recorded.
- Plan acceptance is a runtime transition. A completed node is a declaration until external evidence verifies it. Simple tasks need no formal plan.
- Outcome values are `achieved`, `not_achieved`, `partially_verified` and `unverified`. Runtime completion, report creation or a passing command alone cannot establish the full goal.
- Host verification receipts record bounded scope, before/after hashes, oracle and environment. Achieved requires complete criterion coverage and confirmed final applicability. The existing report gate records document-integrity checks, with partial goal coverage and no exclusivity guarantee. It does not certify the report's claim quality.
- Deep evaluation scans the source, reviews selected suspicious and control segments, and synthesizes validated local findings. Repeated calls nominate candidates; negative results and justified retries can be effective.
- Pipeline completion, attempted coverage and supported coverage are separate. All-unknown results can complete the pipeline while supported coverage remains zero. Unselected segments remain unknown.
- Source quotes and exact delivered ranges accompany findings into synthesis. If synthesis fails, saved local findings remain available.
- Endpoint metrics require cited causal or visibility endpoints. Unsupported cause claims, unseen evidence and unrelated later successes cannot manufacture recovery or adoption metrics. Some endpoints, including plan-response lag, remain unknown when no reliable binding is present. Overlapping causal windows are not additive; redundant cost deduplicates primary call owners. Inferred costs are not predicted savings.

The stage admission policy assigns 15% of calls/tokens to scanning, 60% to focus, 20% to synthesis and at most 5% of calls to repair (one repair per stage response). Very small call budgets use minimum stage allowances but still obey the global limit. Output caps are 2048 tokens for scan/synthesis and 4096 for focus for the built-in provider; smaller configured output limits are honored. Evaluation disables configured `enable_thinking` and lowers configured `reasoning_effort` to `low`, without changing the solver provider. Token thresholds are checked before calls; in-flight usage can exceed them. Interrupted calls retain unknown usage instead of recording zero. Resume preserves cumulative usage and completed stages.

## Evolve and optimization

V3 proposals use `loom.evolution.hypotheses.v2`. They group stable pattern/mechanism/intervention identities, preserve evidence and counterevidence, and include alternative explanations, a predicted endpoint and a concrete validation proposal. Proposals are untested; readiness does not imply experimental support.

You can also generate hypotheses from an existing v3 bundle without another model call:

```bash
uv run python -m loom.evolution.analyze \
  --evaluation-bundle .loom/evaluation-v3/evaluation-bundle.json \
  --out-dir .loom/evolution-v3
```

To feed v3 seed analysis into the existing governed optimization campaign:

```yaml
meta_harness:
  seed_analysis_version: v3
  # Keep your existing proposer_model, solver_model, judge_model and task sets.
```

V3-seeded campaigns require explicit frozen task verifiers and a hard 100% task-success gate with zero baseline regression before cost improvements can qualify. Configure those verifiers to cover required outputs and behaviors that must be preserved. Seed evidence is v3; the existing paired trial runner retains its versioned v1 measurement artifacts and governance. No v3 behavioral dimension is converted into a legacy numeric score. The standalone `assess_paired_experiment` adapter consumes matched task/environment/evaluator records and rejects cheaper candidates with failed preservation checks.

## Calibration and current limits

No human-calibrated accuracy or token-savings claim is made by this release. Defaulting to v3 does not establish that a reviewed held-out corpus has passed these gates. Generate a gate report from recorded human annotations and paired measurements:

```bash
uv run python -m loom.evaluation.calibration --corpus calibration.json --out calibration-report.json
```

The corpus is a JSON array. Each record identifies `episode_id`, `family` (`coding`, `research`, `general`), `split`, two `reviewer_ids`, `adjudicated`, `used_for_prompt_development`, `repeat_runs`, `evidence_audit_passed`, and `unsupported_achieved`. `findings` contain category (`intent`, `plan`, `cycle`, `evidence_adoption`), `human_present` and `v3_present`; `outcomes` contain `sufficient_evidence`, `human_status`, `v3_status`. Cost comparison uses `v2_tokens`, `v3_tokens`, `v2_supported_coverage`, `v3_supported_coverage`.

The report includes raw counts, uncertainty intervals, separate quality/cost gates and an explicit blocked rollout for missing annotations. It conservatively requires 30 independently reviewed held-out episodes across all three families. Review identity/independence must be established by the corpus owner. The tool does not perform human annotation or automatically change defaults.
