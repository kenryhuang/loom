import { element } from "../markdown.mjs";

const DIMENSIONS = {
  context_effectiveness: "Context effectiveness",
  tool_effectiveness: "Tool effectiveness",
  loop_progress: "Loop progress",
  token_efficiency: "Token efficiency",
  verify_gate: "Verification",
};
const count = (value) => Number(value || 0).toLocaleString();
const button = (text, action) => {
  const node = element("button", "quiet", text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
};

export class EvaluationView {
  constructor(root, { api, sessionId, analysisId, signal, evidence, onError }) {
    Object.assign(this, {
      root,
      api,
      sessionId,
      analysisId,
      signal,
      evidence,
      onError,
    });
    this.generation = 0;
    this.timer = null;
    signal.addEventListener("abort", () => clearTimeout(this.timer), {
      once: true,
    });
  }
  async open() {
    this.root.append(element("h2", "", "Deep evaluation"));
    this.status = element("p", "muted", "Loading evaluator…");
    this.form = element("form", "evaluation-controls");
    this.results = element("div", "evaluation-results");
    this.root.append(this.status, this.form, this.results);
    try {
      const catalog = await this.api.evaluations(
        this.sessionId,
        this.analysisId,
        this.signal,
      );
      if (this.signal.aborted) return;
      this.model = element("select");
      for (const model of catalog.models) {
        const option = element("option", "", model.label);
        option.value = model.id;
        this.model.append(option);
      }
      this.model.value = catalog.default_model;
      const modelLabel = element("label", "", "Judge model");
      modelLabel.append(this.model);
      this.form.append(modelLabel);
      this.fields = {};
      for (const [key, label] of [
        ["max_calls", "Max calls"],
        ["max_rounds", "Round limit (from start)"],
        ["max_tokens", "Token threshold"],
        ["max_seconds", "Total time budget (seconds)"],
        ["max_evidence_chars", "Evidence characters"],
      ]) {
        if (catalog.defaults[key] == null) continue;
        const input = element("input");
        input.type = "number";
        input.min = catalog.bounds[key][0];
        input.max = catalog.bounds[key][1];
        input.value = catalog.defaults[key];
        input.required = true;
        const wrapper = element("label", "", label);
        wrapper.append(input);
        this.form.append(wrapper);
        this.fields[key] = input;
      }
      this.start = element("button", "primary", "Run deep evaluation");
      this.start.type = "submit";
      this.start.disabled = !catalog.models.length;
      this.cancel = button("Cancel evaluation", async () => {
        this.cancel.disabled = true;
        try {
          await this.api.cancelEvaluation(
            this.sessionId,
            this.analysisId,
            this.job.id,
            this.signal,
          );
          this.status.textContent = "Cancelling evaluation…";
        } catch (error) {
          this.fail(error);
        }
      });
      this.cancel.hidden = true;
      this.form.append(this.start, this.cancel);
      this.root.insertBefore(
        element(
          "p",
          "muted small",
          "Uses the selected model to review this snapshot. Evaluator usage is separate from task usage. Token thresholds are checked between calls; an in-flight response may exceed the threshold. Unknown evidence stays unknown.",
        ),
        this.form,
      );
      this.status.textContent = catalog.models.length
        ? "Semantic evaluation has not run for this snapshot."
        : "Configure a judge model in the service configuration to run evaluation.";
      this.form.addEventListener("submit", (event) => {
        event.preventDefault();
        this.run();
      });
      if (catalog.jobs.length) {
        const select = element("select");
        select.setAttribute("aria-label", "Evaluation history");
        for (const job of catalog.jobs) {
          const option = element(
            "option",
            "",
            `${job.model} · ${job.state} · ${job.created_at}`,
          );
          option.value = job.id;
          select.append(option);
        }
        select.addEventListener("change", () => this.follow(select.value));
        const label = element("label", "", "Saved evaluations");
        label.append(select);
        this.root.insertBefore(label, this.results);
        this.follow(catalog.jobs[0].id);
      }
    } catch (error) {
      this.fail(error);
    }
  }
  fail(error) {
    if (this.signal.aborted) return;
    this.status.textContent = error.message;
    if (this.start) this.start.disabled = false;
    this.onError?.(error);
  }
  async run() {
    this.start.disabled = true;
    try {
      const resumable =
        this.job &&
        ["failed", "cancelled", "interrupted", "budget_exhausted"].includes(
          this.job.state,
        );
      const options = {
        ...(resumable ? this.job.settings : {}),
        model: this.model.value,
      };
      for (const [key, input] of Object.entries(this.fields))
        options[key] = Number(input.value);
      if (
        resumable &&
        options.model === this.job.model &&
        Object.keys(this.job.settings).every(
          (key) =>
            ["max_seconds", "max_tokens", "max_calls"].includes(key) ||
            options[key] === this.job.settings[key],
        )
      ) {
        options.resume_id = this.job.id;
      }
      const job = await this.api.startEvaluation(
        this.sessionId,
        this.analysisId,
        options,
        this.signal,
      );
      if (!this.signal.aborted) this.follow(job.id);
    } catch (error) {
      this.fail(error);
    }
  }
  async follow(id, generation = ++this.generation) {
    clearTimeout(this.timer);
    try {
      const job = await this.api.evaluation(
        this.sessionId,
        this.analysisId,
        id,
        this.signal,
      );
      if (this.signal.aborted || generation !== this.generation) return;
      if (this.job?.id !== job.id) this.results.replaceChildren();
      this.job = job;
      const active = ["queued", "running"].includes(job.state);
      this.start.disabled = active;
      this.start.textContent = [
        "failed",
        "cancelled",
        "interrupted",
        "budget_exhausted",
      ].includes(job.state)
        ? "Resume evaluation"
        : "Run deep evaluation";
      this.cancel.hidden = !active;
      this.cancel.disabled = false;
      this.model.value = job.model;
      this.model.disabled = active;
      for (const [key, input] of Object.entries(this.fields)) {
        input.value = job.settings[key];
        input.disabled = active;
      }
      const usage = job.usage;
      this.status.textContent = `${job.state} · ${job.stage || "Preparing"} · ${job.reviewed_rounds || 0} / ${job.total_rounds ?? "?"} rounds reviewed · ${job.completed_batches || 0} saved batches · ${count(usage.calls)} calls · ${count(usage.total_tokens)} reported tokens${usage.unreported_calls ? ` · ${usage.unreported_calls} calls without final usage` : ""}${job.error ? ` · ${job.error}` : ""}`;
      this.status.textContent += ` · ${count(Math.ceil(job.elapsed_seconds || 0))} / ${count(job.settings.max_seconds)} seconds used`;
      if (job.state === "budget_exhausted")
        this.status.textContent +=
          " · Increase the exhausted total budget, then Resume evaluation; saved batches will be reused. Usage for the unfinished call is unknown.";
      if (job.evaluation) this.render(job.evaluation);
      if (active)
        this.timer = setTimeout(() => this.follow(id, generation), 1200);
    } catch (error) {
      this.fail(error);
    }
  }
  refs(root, refs = []) {
    const actions = element("div", "trajectory-actions");
    for (const ref of refs)
      actions.append(
        button(
          `Evidence ${ref.line_number}${ref.field_path ? ` · ${ref.field_path}` : ""}`,
          () => this.evidence(ref),
        ),
      );
    root.append(actions);
  }
  section(title, open = false) {
    const node = element("details", "trajectory-section");
    node.open = open;
    node.append(element("summary", "", title));
    this.results.append(node);
    return node;
  }
  render(data) {
    this.results.replaceChildren();
    const { semantic, insights, proposals } = data;
    this.results.append(
      element(
        "h3",
        "",
        `Review coverage · ${insights.reviewed_rounds} / ${insights.total_rounds} rounds`,
      ),
      element(
        "p",
        "muted",
        `Analysis ${semantic.coverage.status} · Task completion remains ${insights.task_completion}.`,
      ),
    );
    for (const limitation of semantic.coverage.limitations || [])
      this.results.append(element("p", "notice", limitation));
    const dimensions = element("div", "evaluation-dimensions");
    for (const [key, label] of Object.entries(DIMENSIONS)) {
      const statuses = semantic.coverage.round_dimension_statuses?.[key] || {};
      const node = element("div", "trajectory-metric");
      node.append(
        element("strong", "", label),
        element(
          "p",
          "",
          Object.entries(statuses)
            .map(([name, value]) => `${name}: ${value}`)
            .join(" · ") || "Not reviewed",
        ),
      );
      dimensions.append(node);
    }
    this.results.append(dimensions);
    const findings = this.section(
      `Key findings · ${semantic.diagnoses.length}`,
      true,
    );
    for (const diagnosis of semantic.diagnoses) {
      const node = element("article", "evaluation-finding");
      node.append(
        element(
          "h3",
          "",
          `${DIMENSIONS[diagnosis.dimension]} · ${diagnosis.epistemic_status}`,
        ),
      );
      for (const [key, label] of [
        ["scope", "Scope"],
        ["observation", "Observation"],
        ["interpretation", "Interpretation"],
        ["mechanism", "Mechanism"],
        ["consequence", "Impact"],
        ["improvement_hypothesis", "Hypothesis"],
        ["preserve", "Preserve"],
        ["evidence_coverage", "Coverage"],
      ]) {
        if (diagnosis[key])
          node.append(element("p", "", `${label}: ${diagnosis[key]}`));
      }
      this.refs(node, diagnosis.supporting_refs);
      if (diagnosis.counterevidence_refs.length) {
        node.append(element("strong", "", "Counterevidence"));
        this.refs(node, diagnosis.counterevidence_refs);
      }
      findings.append(node);
    }
    const costs = this.section("Progress and measured cost", true);
    for (const [kind, row] of Object.entries(insights.progress_costs))
      costs.append(
        element(
          "p",
          "",
          `${kind} · ${row.rounds} rounds · ${count(row.known_tokens)} known tokens · ${row.unmeasured_rounds} unmeasured rounds`,
        ),
      );
    for (const segment of insights.stalled_segments) {
      const node = element("article", "evaluation-finding");
      node.append(
        element(
          "p",
          "",
          `Stalled · ${segment.round_ids.length} rounds · ${count(segment.known_tokens)} known tokens · End: ${segment.end_reason}`,
        ),
      );
      this.refs(node, segment.evidence_refs);
      costs.append(node);
    }
    const carry = insights.context_carry;
    if (carry)
      costs.append(
        element(
          "p",
          "muted",
          `Retained message characters: ${carry.retained_fraction == null ? "unknown" : (carry.retained_fraction * 100).toFixed(1) + "%"}. ${carry.basis}`,
        ),
      );
    for (const window of insights.recovery_windows || []) {
      const node = element("article", "evaluation-finding");
      node.append(
        element(
          "p",
          "",
          `After error ${window.failure_seq} · ${window.status} · ${window.rounds} rounds · ${count(window.known_tokens)} known tokens`,
        ),
        element("p", "muted small", window.limitation),
      );
      this.refs(node, [window.failure_ref]);
      costs.append(node);
    }
    const checks = this.section("Requirement verification", true);
    checks.append(element("p", "muted", insights.verification_basis));
    for (const row of semantic.verification) {
      const node = element("article", "evaluation-finding");
      const criterion = insights.criteria?.find(
        (item) => item.id === row.criterion_id,
      );
      node.append(
        element(
          "h3",
          "",
          `${row.criterion_id} · ${row.status}${criterion?.superseded ? " · Superseded" : ""}`,
        ),
      );
      if (criterion)
        node.append(
          element("p", "", criterion.description),
          element(
            "small",
            "muted",
            `${criterion.run_id} · ${criterion.origin}`,
          ),
        );
      for (const key of [
        "rationale",
        "method",
        "oracle",
        "checked_scope",
        "requirement_coverage",
        "freshness",
        "limitation",
      ])
        if (row[key]) node.append(element("p", "", `${key}: ${row[key]}`));
      this.refs(node, row.evidence_refs);
      checks.append(node);
    }
    const framework = this.section(
      "Proposed verification checks · Not executed",
    );
    for (const row of semantic.verification_framework) {
      const node = element("article", "evaluation-finding");
      for (const key of [
        "criterion_id",
        "check",
        "oracle",
        "failure_signal",
        "limitation",
      ])
        if (row[key]) node.append(element("p", "", `${key}: ${row[key]}`));
      framework.append(node);
    }
    const improvements = this.section(
      `Improvement hypotheses · ${proposals.length}`,
      true,
    );
    for (const row of proposals) {
      const node = element("article", "evaluation-finding");
      node.append(
        element("h3", "", `${row.surface} · proposed · Not tested`),
        element("p", "", row.hypothesis),
        element("p", "muted", row.validation),
        element(
          "p",
          "",
          `Preserve: ${row.preserve.join("; ") || "Not specified"}`,
        ),
      );
      this.refs(node, row.supporting_refs);
      improvements.append(node);
    }
    const preserved = this.section("Effective behaviors to preserve");
    for (const row of semantic.preserved_behaviors) {
      const node = element("article");
      node.append(element("p", "", row.behavior));
      this.refs(node, row.evidence_refs);
      preserved.append(node);
    }
    const rounds = this.section(
      `Round reviews · ${semantic.round_analyses.length}`,
    );
    for (const row of semantic.round_analyses) {
      const node = element("details", "trajectory-section");
      node.append(
        element("summary", "", `${row.round_id} · ${row.progress_kind}`),
      );
      for (const key of [
        "pre_state",
        "intent",
        "action",
        "observed_change",
        "post_state",
      ])
        node.append(element("p", "", `${key}: ${row[key]}`));
      this.refs(node, row.evidence_refs);
      for (const [key, value] of Object.entries(row.dimensions)) {
        node.append(
          element("h4", "", `${DIMENSIONS[key]} · ${value.status}`),
          element("p", "", value.rationale),
        );
        this.refs(node, value.evidence_refs);
      }
      rounds.append(node);
    }
  }
}
