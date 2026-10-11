import { renderBehaviorEvaluation } from "./behavior-evaluation.mjs";
import { element } from "../markdown.mjs";
import { badge, number, seconds, label, disclosure } from "./dashboard.mjs";

const DIMENSIONS = {
  context_effectiveness: "Context effectiveness",
  tool_effectiveness: "Tool effectiveness",
  loop_progress: "Loop progress",
  token_efficiency: "Token efficiency",
  verify_gate: "Verification",
};
const stageLabel = (stage) => {
  if (stage?.startsWith("scan:")) return "Scanning behavior index";
  if (stage?.startsWith("focus:")) return "Analyzing behavior segment";
  if (stage === "synthesis") return "Summarizing findings";
  return stage || "Preparing";
};
const activityLabel = (call) =>
  ({ read_evidence: "Reviewing additional evidence", repair: "Correcting evaluator output" })[call?.activity] || stageLabel(call?.stage);
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
    this.root.classList.add("deep-evaluation");
    this.status = element("p", "notice", "Loading evaluator…");
    this.status.setAttribute("role", "status");
    this.form = element("form", "deep-controls");
    this.toolbar = element("div", "dashboard-toolbar deep-toolbar");
    this.settingsPanel = element("details", "evaluation-settings dashboard-panel");
    this.settingsPanel.append(element("summary", "", "Evaluation settings"));
    this.settingsFields = element("div", "evaluation-controls");
    this.settingsPanel.append(this.settingsFields);
    this.form.append(this.toolbar, this.settingsPanel);
    this.results = element("div", "evaluation-results");
    this.progress = element("section", "evaluation-progress dashboard-panel");
    this.progress.setAttribute("aria-label", "Evaluation progress");
    this.progress.hidden = true;
    this.root.append(this.form, this.status, this.progress, this.results);
    try {
      const catalog = await this.api.evaluations(
        this.sessionId,
        this.analysisId,
        this.signal,
      );
      if (this.signal.aborted) return;
      this.catalog = catalog;
      this.model = element("select");
      for (const model of catalog.models) {
        const option = element("option", "", model.label);
        option.value = model.id;
        this.model.append(option);
      }
      this.model.value = catalog.default_model;
      const modelLabel = element("label", "", "Judge model");
      modelLabel.append(this.model);
      this.toolbar.append(modelLabel);
      this.version = element("select");
      for (const version of catalog.analysis_versions || ["v2"]) {
        const option = element(
          "option",
          "",
          version === "v3"
            ? "v3 · Behavior evaluation"
            : "v2 · Legacy effectiveness",
        );
        option.value = version;
        this.version.append(option);
      }
      this.version.value = catalog.default_analysis_version || "v2";
      const versionLabel = element("label", "", "Analysis version");
      versionLabel.append(this.version);
      this.settingsFields.append(versionLabel);
      this.version.addEventListener("change", () => this.applyDefaults());
      this.fields = {};
      for (const [key, label] of [
        ["max_calls", "Max calls"],
        ["max_rounds", "Review limit"],
        ["max_tokens", "Token budget"],
        ["max_seconds", "Time budget · seconds"],
        ["max_call_seconds", "Call timeout · seconds"],
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
        this.settingsFields.append(wrapper);
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
          this.status.hidden = false;
        } catch (error) {
          this.fail(error);
        }
      });
      this.cancel.hidden = true;
      this.fresh = button("New evaluation", () => {
        clearTimeout(this.timer);
        ++this.generation;
        this.job = null;
        this.version.value = this.catalog.default_analysis_version || "v2";
        this.model.value = this.catalog.default_model;
        this.applyDefaults();
        this.start.textContent = "Run deep evaluation";
        this.start.disabled = !this.catalog.models.length;
        this.model.disabled = this.version.disabled = false;
        for (const field of Object.values(this.fields)) field.disabled = false;
        if (this.history) this.history.value = "";
        this.renderedData = null;
        this.results.replaceChildren(element("p", "empty-state", "No evaluation selected"));
        this.progress.hidden = true;
        this.settingsPanel.open = false;
        this.status.hidden = true;
      });
      const actions = element("div", "deep-actions");
      actions.append(this.start, this.cancel, this.fresh); this.toolbar.append(actions);
      const notes = disclosure(this.settingsPanel, "Budget accounting");
      notes.append(element("p", "muted small", "Evaluator usage is separate from task usage. Token budgets are checked between calls; an in-flight response can exceed the limit. Review limit: focused segments in v3, first rounds in v2."));
      this.status.textContent = catalog.models.length ? "" : "Configure a judge model to run evaluation.";
      this.status.hidden = !!catalog.models.length;
      this.results.append(element("p", "empty-state", "No evaluation yet"));
      this.form.addEventListener("invalid", () => { this.settingsPanel.open = true; }, true);
      this.form.addEventListener("submit", (event) => {
        event.preventDefault();
        this.run();
      });
      this.history = element("select");
      this.history.setAttribute("aria-label", "Evaluation history");
      const freshOption = element("option", "", "New evaluation"); freshOption.value = ""; this.history.append(freshOption);
      for (const job of catalog.jobs) this.historyOption(job);
      this.history.addEventListener("change", () => this.history.value ? this.follow(this.history.value) : this.fresh.click());
      const historyLabel = element("label", "", "Saved evaluations"); historyLabel.append(this.history);
      this.toolbar.prepend(historyLabel);
      if (catalog.jobs.length) {
        this.history.value = catalog.jobs[0].id;
        this.follow(catalog.jobs[0].id);
      }

    } catch (error) {
      this.fail(error);
    }
  }
  historyOption(job) {
    if (!this.history) return;
    let option = [...this.history.options].find(o => o.value === job.id);
    if (!option) { option = element("option"); option.value = job.id; this.history.append(option); }
    const date = job.created_at ? new Date(job.created_at) : null;
    option.textContent = `${date && !Number.isNaN(date.getTime()) ? date.toLocaleString() + " · " : ""}${label(job.state)}`;
    option.title = `${job.model || ""} · ${job.id}`;
  }
  applyDefaults() {
    const defaults = this.catalog.defaults_by_version?.[this.version.value] || this.catalog.defaults;
    for (const [key, input] of Object.entries(this.fields)) input.value = defaults[key];
    this.start.textContent = "Run deep evaluation";
  }
  fail(error) {
    if (this.signal.aborted) return;
    this.status.textContent = error.message;
    this.status.hidden = false;
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
        ...(resumable && this.version.value === (this.job.analysis_version || "v2")
          ? this.job.settings
          : this.catalog.defaults_by_version?.[this.version.value] || {}),
        model: this.model.value,
        analysis_version: this.version.value,
      };
      for (const [key, input] of Object.entries(this.fields))
        options[key] = Number(input.value);
      if (
        resumable &&
        options.model === this.job.model &&
        this.version.value === (this.job.analysis_version || "v2") &&
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
      if (this.job?.id !== job.id) {
        this.results.replaceChildren();
        this.renderedData = null;
      }
      this.historyOption(job);
      if (this.history) this.history.value = job.id;
      const wasActive =
        this.job && ["queued", "running"].includes(this.job.state);
      this.job = job;
      const active = ["queued", "running"].includes(job.state);
      if (active && !wasActive && this.settingsPanel)
        this.settingsPanel.open = false;
      this.start.disabled = active;
      if (this.fresh) this.fresh.disabled = active;
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
      if (this.version) { this.version.value = job.analysis_version || "v2"; this.version.disabled = active; }
      for (const [key, input] of Object.entries(this.fields)) {
        input.value = job.settings[key] ?? this.catalog?.defaults_by_version?.[job.analysis_version || "v2"]?.[key] ?? this.catalog?.defaults[key] ?? "";
        input.disabled = active;
      }
      this.status.textContent = job.error || "";
      if (job.state === "budget_exhausted") {
        this.status.textContent += " Adjust the exhausted total budget in settings, then Resume evaluation. Saved stages are retained.";
      }
      this.status.hidden = !this.status.textContent;
      this.renderProgress(job);
      if (job.evaluation) this.render(job.evaluation);
      if (active)
        this.timer = setTimeout(() => this.follow(id, generation), 1200);
    } catch (error) {
      this.fail(error);
    }
  }
  renderProgress(job) {
    if (!this.progress) return;
    this.progress.hidden = false;
    const active = ["queued", "running"].includes(job.state);
    const behavior = job.analysis_version === "v3";
    const total = behavior
      ? job.selected_segments
      : (job.selected_rounds ?? job.total_rounds);
    const reviewed = behavior
      ? job.reviewed_segments || 0
      : job.reviewed_rounds || 0;
    const call = job.current_call;
    const heading = element("header", "dashboard-panel-heading");
    heading.append(element("h3", "", "Evaluation run"), badge(label(job.state), job.state));
    const coverage = element("progress");
    coverage.setAttribute("aria-label", behavior ? "Reviewed segment coverage" : "Reviewed round coverage");
    if (total != null && total > 0) { coverage.max = total; coverage.value = Math.min(reviewed, total); }
    const phases = element("div", "deep-phases");
    if (behavior) {
      for (const [family, title, detail, done] of [
        ["scan", "Scan", `${job.scanned_pages || 0} / ${job.scan_pages ?? "—"} pages`, job.scan_complete],
        ["focus", "Analyze", `${reviewed} / ${job.selected_segments ?? "—"} segments`, job.scan_complete && job.selected_segments != null && reviewed >= job.selected_segments],
        ["synthesis", "Summarize", job.synthesis_complete ? "Saved" : "Pending", job.synthesis_complete],
      ]) {
        const phase = element("div", `deep-phase${done ? " done" : active && job.stage?.startsWith(family) ? " active" : ""}`);
        phase.append(element("strong", "", title), element("span", "", detail),
          element("small", "muted", seconds(job.stage_timings?.[family]?.elapsed_seconds == null ? null : job.stage_timings[family].elapsed_seconds * 1000)));
        phases.append(phase);
      }
    } else phases.append(element("span", "muted", `${reviewed} / ${total ?? "—"} rounds reviewed`));
    const metrics = element("div", "deep-budget-grid");
    for (const [title, used, limit, format] of [
      ["Evaluator calls", job.usage.calls, job.settings?.max_calls, number],
      ["Reported tokens", job.usage.total_tokens, job.settings?.max_tokens, number],
      ["Elapsed", job.elapsed_seconds, job.settings?.max_seconds, v => seconds(v == null ? null : v * 1000)],
    ]) {
      const metric = element("div", "deep-budget"), value = element("div", "deep-budget-value");
      value.append(element("strong", "", format(used)), element("span", "muted", `/ ${format(limit)}`));
      const gauge = element("div", "deep-budget-track"), fill = element("span");
      gauge.setAttribute("role", "img"); gauge.setAttribute("aria-label", `${title}: ${format(used)} / ${format(limit)}`);
      fill.style.width = `${limit > 0 && used != null ? Math.min(100, used / limit * 100) : 0}%`;
      if (limit > 0 && used >= limit) metric.classList.add("exhausted");
      gauge.append(fill); metric.append(element("span", "muted", title), value, gauge); metrics.append(metric);
    }
    const current = element("p", "evaluation-current");
    current.setAttribute("role", "status");
    current.hidden = !active;
    if (active && call?.state === "waiting") {
      const elapsed = Math.max(0, Math.floor((job.elapsed_seconds || 0) - (call.started_elapsed_seconds || 0)));
      current.textContent = `${activityLabel(call)} · Waiting for evaluator response · ${elapsed}s${behavior ? ` / ${call.timeout_seconds ?? job.settings?.max_call_seconds ?? 120}s` : ""}`;
    } else if (active) current.textContent = stageLabel(job.stage);
    if (job.usage.unreported_calls) heading.append(badge(`${job.usage.unreported_calls} calls · usage ${active ? "pending" : "unknown"}`));
    const log = element("ol", "evaluation-progress-log");
    log.setAttribute("aria-label", "Evaluation activity");
    if (!job.progress) {
      const signature = JSON.stringify([
        job.id,
        job.state,
        job.stage,
        job.completed_batches,
        job.usage.calls,
      ]);
      if (signature !== this.observedSignature) {
        if (this.observedJob !== job.id) this.observedProgress = [];
        this.observedJob = job.id;
        this.observedSignature = signature;
        this.observedProgress = [
          ...(this.observedProgress || []),
          {
            elapsed_seconds: job.elapsed_seconds,
            message: `${job.state} · ${job.stage || "Preparing"} · ${job.completed_batches || 0} saved batches`,
          },
        ].slice(-100);
      }
    }
    for (const item of job.progress || this.observedProgress || []) {
      const row = element("li");
      row.append(
        element("time", "muted", `${Math.floor(item.elapsed_seconds || 0)}s`),
        element("span", "", item.message),
      );
      log.append(row);
    }
    const previous = this.progress.querySelector(".evaluation-progress-log");
    const bottom =
      !previous ||
      previous.scrollHeight - previous.scrollTop - previous.clientHeight < 30;
    const scroll = previous?.scrollTop || 0;
    const logOpen = this.progress.querySelector("details")?.open || false;
    const logs = element("details", "dashboard-disclosure"); logs.open = logOpen;
    logs.append(element("summary", "", `Activity log · ${job.completed_batches || 0} saved ${behavior ? "stages" : "batches"}`), log);
    this.progress.replaceChildren(heading, phases, coverage, metrics, current, logs);
    log.scrollTop = bottom ? log.scrollHeight : scroll;
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
    const signature = JSON.stringify(data);
    if (signature === this.renderedData) return;
    const opened = new Set([...this.results.querySelectorAll("details[open][data-detail]")].map(n => n.dataset.detail));
    const filters = [...this.results.querySelectorAll("[data-deep-filter]")].map(n => [n.dataset.deepFilter, n.value]);
    this.results.replaceChildren();
    this.renderedData = signature;
    if (data.schema_version === "loom.evaluation.bundle.v3") {
      renderBehaviorEvaluation(this, data);
      for (const [key, value] of filters) {
        const node = [...this.results.querySelectorAll("[data-deep-filter]")].find(n => n.dataset.deepFilter === key);
        if (node && [...node.options].some(o => o.value === value)) { node.value = value; node.dispatchEvent(new node.ownerDocument.defaultView.Event("change")); }
      }
      for (const node of this.results.querySelectorAll("details[data-detail]")) if (opened.has(node.dataset.detail)) node.open = true;
      return;
    }
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
