import {IncrementalGraphView} from "./incremental-graph.mjs";
import {renderGoalSummary, GOAL_SCHEMA} from "./goal-loops.mjs";
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
  if (stage?.startsWith("memory:")) return "Extracting reusable memory from trace evidence";
  if (stage?.startsWith("batch:")) return "Reviewing goals and action contributions";
  if (stage?.startsWith("structure:")) return "Reconstructing goals and phase occurrences";
  if (stage?.startsWith("reconcile:")) return "Reconciling goal boundaries";
  if (stage?.startsWith("utility:")) return "Evaluating action utility";
  if (stage?.startsWith("diagnose:")) return "Diagnosing goal execution";
  if (stage?.startsWith("scan:")) return "Scanning behavior index";
  if (stage?.startsWith("focus:")) return "Analyzing behavior segment";
  if (stage === "synthesis") return "Summarizing findings";
  return stage || "Preparing";
};
const activityLabel = (call) =>
  ({ read_evidence: "Reviewing additional evidence", repair: "Correcting evaluator output" })[call?.activity] || stageLabel(call?.stage);
const count = (value) => Number(value || 0).toLocaleString();
const RESUMABLE = new Set(["failed", "cancelled", "interrupted", "budget_exhausted", "incomplete"]);
const RUN_VERSIONS = new Set(["goal_loops_v1", "goal_graph_v1"]);
const createdLabel = value => {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "Creation time unavailable";
};
const button = (text, action) => {
  const node = element("button", "quiet", text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
};

export class EvaluationView {
  constructor(root, { api, sessionId, analysisId, signal, evidence, onError, onResult, onExplore }) {
    Object.assign(this, {
      root,
      api,
      sessionId,
      analysisId,
      signal,
      evidence,
      onError,
      onResult,
      onExplore,
    });
    this.generation = 0;
    this.timer = null;
    this.cancellingIds = new Set();
    this.cancelTimers = new Map();
    signal.addEventListener("abort", () => {
      clearTimeout(this.timer);
      for (const timer of this.cancelTimers.values()) clearTimeout(timer);
    }, {
      once: true,
    });
  }
  async open() {
    this.root.classList.add("deep-evaluation");
    this.status = element("p", "notice", "Loading evaluator…");
    this.status.setAttribute("role", "status");
    this.form = element("form", "deep-controls");
    this.toolbar = element("div", "dashboard-toolbar deep-toolbar");
    this.historyPanel = element("section", "evaluation-history dashboard-panel");
    this.historyPanel.setAttribute("aria-label", "Evaluation history");
    this.historyHeading = element("header", "dashboard-panel-heading");
    this.historyTitle = element("h3", "", "Evaluation history");
    this.historyHeading.append(this.historyTitle);
    this.historyList = element("ul", "evaluation-history-list");
    this.historyPanel.append(this.historyHeading, this.historyList);
    this.settingsPanel = element("details", "evaluation-settings dashboard-panel setup-block");
    this.settingsSummary = element("summary", "", "Evaluation settings · New evaluation");
    this.settingsPanel.append(this.settingsSummary);
    this.settingsFields = element("div", "evaluation-controls");
    this.settingsPanel.append(this.settingsFields);
    this.form.append(this.toolbar, this.settingsPanel, this.historyPanel);
    this.results = element("div", "evaluation-results");
    this.progress = element("section", "evaluation-progress dashboard-panel");
    this.progress.setAttribute("aria-label", "Evaluation progress");
    this.progress.hidden = true;
    this.root.append(element('p', 'analysis-session-id', `Session: ${this.sessionId}`), this.form, this.status, this.progress, this.results);
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
          version === "goal_graph_v1" ? "Goal process · Incremental graph" : version === "goal_loops_v1" ? "Goal loops · Execution & cost" : version === "v3"
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
      this.runSelect = element("select"); this.runSelect.setAttribute("aria-label", "Evaluation run scope");
      for (const id of catalog.run_ids || []) this.ensureRunOption(id);
      for (const job of catalog.jobs) this.ensureScopeOption(job);
      if (!this.runSelect.options.length || [...this.version.options].some(option => !RUN_VERSIONS.has(option.value))) this.ensureScopeOption({});
      this.runSelect.value = catalog.run_ids?.at(-1) || this.runSelect.options[0]?.value || "";
      this.runLabel = element("label", "", "Run scope"); this.runLabel.append(this.runSelect); this.toolbar.prepend(this.runLabel);
      this.memoryLabel = element('label', 'evaluation-memory-toggle');
      this.memory = element('input'); this.memory.type = 'checkbox'; this.memory.checked = !!catalog.memory_default;
      this.memory.setAttribute('aria-label', 'Extract memory');
      this.memoryLabel.append(this.memory, document.createTextNode('Extract memory · contributions, facts, failure lessons & workflows'));
      this.settingsPanel.append(this.memoryLabel);
      const scopeVisibility = () => {
        this.memoryLabel.hidden = !catalog.supports_memory || this.version.value !== 'goal_graph_v1';
        for (const key of ["max_rounds", "max_tokens"]) if(this.fields?.[key]) this.fields[key].parentElement.hidden = this.version.value === "goal_graph_v1";
      };
      scopeVisibility();
      this.version.addEventListener("change", () => { this.applyDefaults(); scopeVisibility(); });
      this.fields = {};
      for (const [key, label] of [
        ["max_calls", "Max calls"],
        ["max_rounds", "Review limit"],
        ["max_tokens", "Token budget"],
        ["max_seconds", "Time budget · seconds"],
        ["max_call_seconds", "Call timeout · seconds"],
        ["max_prompt_chars", "Request characters · per call"],
        ["max_evidence_chars", "Evidence characters · total"],
      ]) {
        if (catalog.defaults[key] == null) continue;
        const input = element("input");
        input.type = "number";
        input.min = catalog.bounds[key][0];
        input.max = catalog.bounds[key][1];
        input.value = catalog.defaults[key];
        input.required = true;
        if (key === "max_prompt_chars") input.title = "Maximum characters in one model request, including instructions, metadata, evidence and response schema.";
        if (key === "max_evidence_chars") input.title = "Cumulative evidence read allowance across the evaluation. This does not set the per-request input limit.";
        const wrapper = element("label", "", label);
        wrapper.append(input);
        this.settingsFields.append(wrapper);
        this.fields[key] = input;
      }
      scopeVisibility();
      this.start = element("button", "primary", "New evaluation");
      this.start.type = "submit";
      this.start.disabled = !catalog.models.length;
      const actions = element("div", "deep-actions");
      actions.append(this.start); this.toolbar.append(actions);
      const notes = disclosure(this.settingsPanel, "Budget accounting");
      notes.append(element("p", "muted small", "Evaluator usage is separate from task usage. Incremental graph review records tokens without a token budget or output cap. Time and call limits still apply; unknown usage is marked explicitly. Legacy evaluators check reported usage between requests."));
      this.status.textContent = catalog.models.length ? "" : "Configure a judge model to run evaluation.";
      this.status.hidden = !!catalog.models.length;
      this.results.append(element("p", "empty-state", "No evaluation yet"));
      this.form.addEventListener("invalid", () => { this.settingsPanel.open = true; }, true);
      this.form.addEventListener("submit", (event) => {
        event.preventDefault();
        this.createEvaluation();
      });
      this.runSelect.addEventListener("change", () => {
        this.newEvaluation();
      });
      this.matchScopeVersion();
      this.applyDefaults();
      this.renderHistory();
      const latest = this.scopedJobs()[0];
      if (latest) this.follow(latest.id);

    } catch (error) {
      this.fail(error);
    }
  }
  newEvaluation() {
    if (this.deleting || this.submitting) return;
    clearTimeout(this.timer);
    ++this.generation;
    this.job = null;
    this.selectedHistoryId = null;
    this.settingsPanel.hidden = false;
    this.version.value = this.catalog.default_analysis_version || "v2";
    this.matchScopeVersion();
    this.model.value = this.catalog.default_model;
    this.applyDefaults();
    this.start.hidden = false;
    this.start.textContent = "New evaluation";
    this.start.disabled = !this.catalog.models.length || !this.version.value;
    this.model.disabled = this.version.disabled = false;
    if (this.runSelect) {
      this.runSelect.disabled = false;
    }
    for (const field of Object.values(this.fields)) field.disabled = false;
    this.memory.disabled = false;
    this.renderHistory();
    this.renderedData = null;
    this.graphView = null;
    this.onResult?.(null);
    this.results.replaceChildren(element("p", "empty-state", "No evaluation selected"));
    this.progress.hidden = true;
    this.settingsPanel.open = false;
    this.status.hidden = true;
    this.root.insertBefore(this.status, this.progress);
  }
  runName(id) {
    const index = (this.catalog?.run_ids || []).indexOf(id);
    return index >= 0 ? `Run ${index + 1}` : `Run ${id}`;
  }
  savedScope(job) {
    if (job.run_id) return {key: `run:${job.run_id}`, label: this.runName(job.run_id), id: job.run_id};
    if (["goal_graph_v1", "goal_loops_v1"].includes(job.analysis_version)) return {key: "missing-run", label: "Run unavailable"};
    return {key: "snapshot", label: "Snapshot scope"};
  }
  ensureRunOption(id) {
    if ([...this.runSelect.options].some(option => option.value === id)) return;
    const name = this.runName(id);
    const option = element("option", "", name === `Run ${id}` ? name : `${name} · ${id}`);
    option.value = id; this.runSelect.append(option);
  }
  scopeValue(job) {
    return job.run_id || (this.savedScope(job).key === "missing-run" ? "__missing_run__" : "");
  }
  ensureScopeOption(job) {
    if (job.run_id) return this.ensureRunOption(job.run_id);
    const value = this.scopeValue(job);
    if ([...this.runSelect.options].some(option => option.value === value)) return;
    const option = element("option", "", this.savedScope(job).label);
    option.value = value; this.runSelect.append(option);
  }
  matchScopeVersion() {
    const run = !!this.runSelect.value && this.runSelect.value !== "__missing_run__";
    for (const option of this.version.options) option.disabled = RUN_VERSIONS.has(option.value) !== run;
    if (this.version.selectedOptions[0]?.disabled) this.version.value = [...this.version.options].find(option => !option.disabled)?.value || "";
  }
  scopedJobs() {
    return this.catalog.jobs.filter(job => this.scopeValue(job) === this.runSelect.value);
  }
  rememberJob(job) {
    const index = this.catalog.jobs.findIndex(row => row.id === job.id);
    if (index < 0) this.catalog.jobs.unshift(job);
    else this.catalog.jobs[index] = job;
    this.ensureScopeOption(job);
  }
  renderHistory() {
    if (!this.historyList) return;
    const jobs = this.scopedJobs();
    this.historyTitle.textContent = `Evaluation history · ${jobs.length}`;
    this.start.disabled = !!this.submitting || !!this.deleting || !this.catalog.models.length || !this.model.value || !this.version.value
      || this.runSelect.value === "__missing_run__" || !!(this.selectedHistoryId && this.selectedHistoryId !== this.job?.id);
    this.historyList.replaceChildren();
    for (const job of jobs) {
      const item = element("li", "evaluation-history-item");
      item.dataset.selected = String(this.selectedHistoryId === job.id);
      const select = button("", () => this.follow(job.id));
      select.className = "evaluation-history-record";
      select.dataset.evaluationId = job.id;
      select.setAttribute("aria-current", String(this.selectedHistoryId === job.id));
      select.disabled = !!this.submitting || !!this.deleting;
      const scope = this.savedScope(job);
      const field = (name, value, code = false) => {
        const cell = element("span", "evaluation-history-field");
        cell.append(element("span", "evaluation-history-field-label", name), element(code ? "code" : "span", "evaluation-history-field-value", value));
        select.append(cell);
      };
      field(scope.id ? scope.label : "Run scope", scope.id || (scope.key === "snapshot" ? "Snapshot scope" : "Run ID was not recorded"), !!scope.id);
      field("Evaluation", job.id, true);
      field("Created", createdLabel(job.created_at));
      field("Judge model", job.model || "Unknown model");
      const status = element("span", "evaluation-history-status");
      status.append(badge(label(job.state), job.state));
      if (job.status_version) status.append(badge(`${label(job.execution_state)} · ${label(job.result_state)}`));
      const actions = element("div", "evaluation-history-actions");
      if (["queued", "running"].includes(job.state)) {
        const cancel = button("Cancel", () => this.cancelEvaluation(job.id));
        cancel.className = "evaluation-history-cancel";
        cancel.setAttribute("aria-label", `Cancel evaluation ${job.id}`);
        cancel.title = this.cancellingIds.has(job.id) ? "Cancellation requested; waiting for the evaluation to stop." : "Cancel this evaluation.";
        cancel.disabled = !!this.submitting || !!this.deleting || this.cancellingIds.has(job.id);
        actions.append(cancel);
      } else if (RESUMABLE.has(job.state)) {
        const retry = job.execution_state === 'completed' && job.retryable_work > 0;
        const resume = button(retry ? "Retry failed items" : "Resume", () => this.resumeEvaluation(job.id, retry));
        resume.className = "primary evaluation-history-resume";
        resume.setAttribute("aria-label", `${retry ? 'Retry failed items in' : 'Resume'} evaluation ${job.id}`);
        if (retry) resume.title = `Retry ${job.retryable_work} settled items within the saved total budget; accepted results remain saved.`;
        resume.disabled = !!this.submitting || !!this.deleting || !!this.resumingId || !this.catalog.models.some(model => model.id === job.model)
          || (this.catalog.analysis_versions && !this.catalog.analysis_versions.includes(job.analysis_version || "v2"));
        actions.append(resume);
      }
      item.append(select, status, actions);
      if (this.catalog.supports_delete) {
        const remove = button("Delete", () => this.deleteEvaluation(job.id));
        remove.className = "danger evaluation-history-delete";
        remove.setAttribute("aria-label", `Delete evaluation ${job.id}`);
        remove.disabled = !!this.submitting || !!this.deleting || ["queued", "running"].includes(job.state);
        remove.title = ["queued", "running"].includes(job.state)
          ? "Cancel the evaluation and wait for it to stop before deleting."
          : "Permanently delete this evaluation and its saved records.";
        actions.append(remove);
      }
      this.historyList.append(item);
    }
    if (!jobs.length) this.historyList.append(element("li", "empty-state", "No evaluations in this scope. Start a new evaluation to create a history record."));
  }
  applyDefaults() {
    this.settingsSummary.textContent = "Evaluation settings · New evaluation";
    const defaults = this.catalog.defaults_by_version?.[this.version.value] || this.catalog.defaults;
    for (const [key, input] of Object.entries(this.fields)) input.value = defaults[key];
    for (const key of ["max_rounds", "max_tokens"]) if(this.fields[key]) this.fields[key].parentElement.hidden = this.version.value === "goal_graph_v1";
    if (this.memory) {
      this.memory.checked = !!this.catalog.memory_default;
      this.memoryLabel.hidden = !this.catalog.supports_memory || this.version.value !== 'goal_graph_v1';
    }
    this.start.textContent = "New evaluation";
  }
  fail(error) {
    if (this.signal.aborted) return;
    this.status.textContent = error.message;
    this.status.hidden = false;
    // Request failures can happen before a saved evaluation has loaded.
    // Keep those errors visible in the run panel without stale metrics.
    const existingHeading = this.progress.querySelector(".dashboard-panel-heading");
    if (!existingHeading || !this.job || this.selectedHistoryId !== this.job.id) {
      const heading = element("header", "dashboard-panel-heading");
      heading.append(element("h3", "", "Evaluation run"));
      this.progress.replaceChildren(heading, this.status);
    } else existingHeading.after(this.status);
    this.progress.hidden = false;
    if (this.start) this.start.disabled = false;
    this.onError?.(error);
  }
  async createEvaluation() {
    if (this.start.disabled || this.deleting || this.submitting || !this.form.reportValidity()) return;
    const model = this.model.value, version = this.version.value, memory = this.memory.checked;
    const values = Object.fromEntries(Object.entries(this.fields).map(([key, input]) => [key, input.value]));
    this.newEvaluation();
    this.model.value = model;
    this.version.value = version;
    this.applyDefaults();
    this.memory.checked = memory;
    for (const [key, value] of Object.entries(values)) this.fields[key].value = value;
    await this.run();
  }
  async run() {
    if (this.deleting || this.submitting || (this.selectedHistoryId && this.selectedHistoryId !== this.job?.id)) return;
    if (this.job && !RESUMABLE.has(this.job.state)) return;
    this.submitting = true;
    this.start.disabled = true;
    this.runSelect.disabled = true;
    this.renderHistory();
    try {
      const resumable = this.job && RESUMABLE.has(this.job.state);
      const options = {
        ...(resumable && this.version.value === (this.job.analysis_version || "v2")
          ? this.job.settings
          : this.catalog.defaults_by_version?.[this.version.value] || {}),
        model: this.model.value,
        analysis_version: this.version.value,
        ...(["goal_loops_v1", "goal_graph_v1"].includes(this.version.value) ? {run_id: this.runSelect.value} : {}),
      };
      for (const [key, input] of Object.entries(this.fields))
        options[key] = Number(input.value);
      if (this.catalog.supports_memory && this.version.value === 'goal_graph_v1') options.extract_memory = this.memory.checked;
      else delete options.extract_memory;
      if (
        resumable &&
        options.model === this.job.model &&
        (options.run_id || null) === (this.job.run_id || null) &&
        this.version.value === (this.job.analysis_version || "v2") &&
        Object.keys(this.job.settings).every(
          (key) =>
            ["max_seconds", "max_tokens", "max_calls", ...(options.analysis_version === "goal_graph_v1" ? ["max_prompt_chars", "max_evidence_chars", "max_call_seconds"] : [])].includes(key) ||
            options[key] === this.job.settings[key],
        )
      ) {
        options.resume_id = this.job.id;
        if (this.retryFailed) options.retry_failed = true;
      }
      if (this.job && !options.resume_id) throw new Error("Resume must keep the saved model, run and analysis settings. Use New evaluation to change them.");
      if (!this.job) options.new_evaluation = true;
      const job = await this.api.startEvaluation(
        this.sessionId,
        this.analysisId,
        options,
        this.signal,
      );
      this.submitting = false;
      if (!this.signal.aborted) {
        this.rememberJob(job);
        await this.follow(job.id);
      }
    } catch (error) {
      this.fail(error);
    } finally {
      this.submitting = false;
      if (!this.signal.aborted) {
        this.runSelect.disabled = false;
        this.renderHistory();
      }
    }
  }
  async cancelEvaluation(id) {
    const job = this.catalog.jobs.find(row => row.id === id);
    if (this.deleting || this.submitting || this.cancellingIds.has(id) || !["queued", "running"].includes(job?.state)) return;
    this.cancellingIds.add(id);
    this.renderHistory();
    try {
      await this.api.cancelEvaluation(this.sessionId, this.analysisId, id, this.signal);
      if (!this.signal.aborted) await this.refreshCancellation(id);
    } catch (error) {
      this.cancellingIds.delete(id);
      if (!this.signal.aborted) { this.renderHistory(); this.fail(error); }
    }
  }
  async refreshCancellation(id) {
    this.cancelTimers.delete(id);
    try {
      const job = await this.api.evaluation(this.sessionId, this.analysisId, id, this.signal);
      if (this.signal.aborted) return;
      if (!this.catalog.jobs.some(row => row.id === id)) { this.cancellingIds.delete(id); return; }
      this.rememberJob(job);
      if (["queued", "running"].includes(job.state)) {
        this.cancelTimers.set(id, setTimeout(() => this.refreshCancellation(id), 1200));
      } else {
        this.cancellingIds.delete(id);
        if (this.selectedHistoryId === id && !this.deleting && !this.submitting) await this.follow(id);
      }
      if (!this.signal.aborted) this.renderHistory();
    } catch (error) {
      this.cancellingIds.delete(id);
      if (!this.signal.aborted) { this.renderHistory(); this.fail(error); }
    }
  }
  async resumeEvaluation(id, retryFailed = false) {
    if (this.deleting || this.submitting || this.resumingId) return;
    this.resumingId = id;
    this.retryFailed = retryFailed;
    this.renderHistory();
    try {
      if (this.selectedHistoryId !== id || this.job?.id !== id) {
        const loaded = await this.follow(id);
        if (!loaded || this.selectedHistoryId !== id || this.job?.id !== id) return;
      }
      if (this.signal.aborted || !RESUMABLE.has(this.job.state) || this.start.disabled) return;
      if (this.form.reportValidity()) await this.run();
    } finally {
      this.resumingId = null;
      this.retryFailed = false;
      if (!this.signal.aborted) this.renderHistory();
    }
  }
  async deleteEvaluation(id) {
    const job = this.catalog.jobs.find(row => row.id === id);
    if (!this.catalog.supports_delete || !job || this.deleting || this.submitting || ["queued", "running"].includes(job.state)) return;
    const confirmed = this.root.ownerDocument.defaultView.confirm(
      `Permanently delete evaluation ${job.id}?\n\nThis removes its metadata, checkpoint, evaluator records and saved results, including earlier revisions. The original task trace and results shared by other evaluations are retained. This cannot be undone.`,
    );
    if (!confirmed) return;
    this.deleting = true;
    clearTimeout(this.timer);
    ++this.generation;
    const selected = this.selectedHistoryId;
    const controls = [...this.form.querySelectorAll("button, input, select")];
    const disabled = controls.map(node => node.disabled);
    controls.forEach(node => { node.disabled = true; });
    let error;
    try {
      await this.api.deleteEvaluation(this.sessionId, this.analysisId, job.id, this.signal);
      this.catalog.jobs = this.catalog.jobs.filter(row => row.id !== job.id);
    } catch (failure) {
      error = failure;
    } finally {
      this.deleting = false;
      if (!this.signal.aborted) controls.forEach((node, i) => { node.disabled = disabled[i]; });
    }
    if (this.signal.aborted) return;
    if (!error && selected === job.id) this.newEvaluation();
    else {
      this.renderHistory();
      // Deleting a different row preserves the selection, including an in-flight detail load or polling.
      if (selected && (this.job?.id !== selected || ["queued", "running"].includes(this.job?.state))) await this.follow(selected);
    }
    if (error) this.fail(error);
    else {
      this.status.textContent = "Evaluation permanently deleted.";
      this.status.hidden = false;
    }
  }
  async follow(id, generation) {
    if (this.deleting || this.submitting) return;
    generation ??= ++this.generation;
    clearTimeout(this.timer);
    this.selectedHistoryId = id;
    this.renderHistory();
    this.start.disabled = true;
    if (this.job?.id !== id) {
      this.progress.hidden = true;
      this.settingsPanel.hidden = true;
      this.renderedData = this.graphView = null;
      this.onResult?.(null);
      this.results.replaceChildren(element("p", "empty-state", "Loading evaluation…"));
    }
    try {
      const job = await this.api.evaluation(
        this.sessionId,
        this.analysisId,
        id,
        this.signal,
      );
      if (this.signal.aborted || generation !== this.generation) return;
      if (this.job?.id !== job.id) {
        this.onResult?.(null, job);
        this.results.replaceChildren();
        this.renderedData = null;
      }
      this.rememberJob(job);
      const wasActive =
        this.job && ["queued", "running"].includes(this.job.state);
      const loadSettings = this.job?.id !== job.id;
      this.job = job;
      this.settingsPanel.hidden = false;
      this.settingsSummary.textContent = "Evaluation settings · Saved evaluation";
      const active = ["queued", "running"].includes(job.state);
      if (active && !wasActive && this.settingsPanel)
        this.settingsPanel.open = false;
      this.start.hidden = false;
      if (loadSettings) {
        this.model.value = this.catalog.models.some(model => model.id === job.model) ? job.model : this.catalog.default_model;
        this.version.value = job.analysis_version || "v2";
        if (!this.version.value) this.version.value = this.catalog.default_analysis_version || "v2";
        this.memory.checked = !!job.settings.extract_memory;
        for (const [key, input] of Object.entries(this.fields)) {
          input.value = job.settings[key] ?? this.catalog.defaults_by_version?.[this.version.value]?.[key] ?? this.catalog.defaults[key] ?? "";
        }
      }
      this.model.disabled = this.version.disabled = this.memory.disabled = false;
      for (const input of Object.values(this.fields)) input.disabled = false;
      this.memoryLabel.hidden = !this.catalog.supports_memory || this.version.value !== 'goal_graph_v1';
      for (const key of ["max_rounds", "max_tokens"]) if(this.fields[key]) this.fields[key].parentElement.hidden = this.version.value === "goal_graph_v1";
      this.runSelect.disabled = false;
      this.runSelect.value = this.scopeValue(job);
      this.matchScopeVersion();
      this.renderHistory();
      this.status.textContent = job.error || "";
      if (job.error_details?.some(detail => detail.code === "request.timeout")) {
        this.status.textContent += " Increase Call timeout · seconds in Evaluation settings, then use Resume on this history record. Saved results are retained.";
      }
      if (job.state === "budget_exhausted") {
        this.status.textContent += /input budget reached/i.test(job.error || "")
          ? " Increase Request characters · per call in Evaluation settings, then Resume evaluation. Saved stages are retained."
          : " Adjust the exhausted total budget in settings, then Resume evaluation. Saved stages are retained.";
      }
      this.status.hidden = !this.status.textContent;
      this.renderProgress(job);
      if (job.evaluation) this.render(job.evaluation);
      if (active)
        this.timer = setTimeout(() => this.follow(id, generation), 1200);
      return job;
    } catch (error) {
      if (generation !== this.generation) return;
      this.fail(error);
    }
  }
  renderProgress(job) {
    if (!this.progress) return;
    this.progress.hidden = false;
    const active = ["queued", "running"].includes(job.state);
    const goalLoops = ["goal_loops_v1", "goal_graph_v1"].includes(job.analysis_version);
    const behavior = job.analysis_version === "v3";
    const total = goalLoops ? job.total_packets : behavior
      ? job.selected_segments
      : (job.selected_rounds ?? job.total_rounds);
    const reviewed = goalLoops ? job.reviewed_packets || 0 : behavior
      ? job.reviewed_segments || 0
      : job.reviewed_rounds || 0;
    const call = job.current_call;
    const heading = element("header", "dashboard-panel-heading");
    heading.append(element("h3", "", "Evaluation run"), badge(label(job.state), job.state));
    if (job.status_version) heading.append(badge(`${label(job.execution_state)} · ${label(job.result_state)}`));
    const coverage = element("progress");
    coverage.setAttribute("aria-label", goalLoops ? "Reviewed evidence coverage" : behavior ? "Reviewed segment coverage" : "Reviewed round coverage");
    if (total != null && total > 0) { coverage.max = total; coverage.value = Math.min(reviewed, total); }
    const phases = element("div", "deep-phases");
    if (job.analysis_version === "goal_graph_v1") {
      for (const [title,value] of [["Cards reviewed",`${reviewed} / ${total ?? "—"}`],["Graph revision",job.graph_revision||0],["Conflicts",job.review_gaps||0]]) {
        const phase=element("div","deep-phase");phase.append(element("strong","",title),element("span","",String(value)));phases.append(phase);
      }
      if (job.status_version) {
        for (const [title, value] of [['Unfinished work', job.pending_work], ['Quality gaps', job.retryable_work], ['Unknown judgments', job.unknown_judgments]]) {
          const phase = element('div', 'deep-phase'); phase.append(element('strong', '', title), element('span', '', String(value ?? 0))); phases.append(phase);
        }
      }
    } else if (goalLoops) {
      for (const [title, value] of [["Structure", `${reviewed} / ${total ?? "—"} evidence pages`], ["Reconciliation", job.reconciliation_complete ? (job.execution_protocol >= 2 && !job.reconciliation_candidates ? "Skipped · no candidates" : "Saved") : "Pending"], ["Diagnosis", `${job.diagnosed_goals || 0} goals`]]) {
        const phase = element("div", "deep-phase"); phase.append(element("strong", "", title), element("span", "", value)); phases.append(phase);
      }
    } else if (behavior) {
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
      ["Reported tokens", job.usage.total_tokens, job.analysis_version === "goal_graph_v1" ? null : job.settings?.max_tokens, number],
      ["Elapsed", job.elapsed_seconds, job.settings?.max_seconds, v => seconds(v == null ? null : v * 1000)],
    ]) {
      const metric = element("div", "deep-budget"), value = element("div", "deep-budget-value");
      value.append(element("strong", "", format(used)));
      if (limit != null) value.append(element("span", "muted", `/ ${format(limit)}`));
      const gauge = element("div", "deep-budget-track"), fill = element("span");
      gauge.setAttribute("role", "img"); gauge.setAttribute("aria-label", `${title}: ${format(used)} / ${format(limit)}`);
      fill.style.width = `${limit > 0 && used != null ? Math.min(100, used / limit * 100) : 0}%`;
      if (limit > 0 && used >= limit) metric.classList.add("exhausted");
      gauge.append(fill); metric.append(element("span", "muted", title), value);
      if (limit != null) metric.append(gauge);
      metrics.append(metric);
    }
    const current = element("p", "evaluation-current");
    current.setAttribute("role", "status");
    current.hidden = !active;
    if (job.state === "queued") {
      const queue = job.queue;
      const running = queue?.running || [];
      current.textContent = queue
        ? `Queue position ${queue.position ?? "—"} · ${running.length} / ${queue.workers} evaluators running`
        : "Queued · Waiting for an evaluator slot";
      for (const other of running) {
        const row = element("span", "", ` · ${other.model} · ${other.session_id} · ${stageLabel(other.stage)}`);
        row.style.display = "block";
        current.append(row);
      }
    } else if (active && call?.state === "waiting") {
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
    this.progress.replaceChildren(heading, ...(this.status ? [this.status] : []), phases, coverage, metrics, current, logs);
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
    const signature = JSON.stringify([this.job?.id, data]);
    this.onResult?.(data, this.job);
    if (signature === this.renderedData) return;
    if (data.skeleton?.projection_version) {
      if (!this.graphView) this.graphView=new IncrementalGraphView(element("section"),{evidence:ref=>this.evidence(ref)});
      this.results.replaceChildren(this.graphView.root);this.graphView.update({...data, session_id: data.session_id || this.sessionId}, this.job?.id);this.renderedData=signature;return;
    }
    const opened = new Set([...this.results.querySelectorAll("details[open][data-detail]")].map(n => n.dataset.detail));
    const filters = [...this.results.querySelectorAll("[data-deep-filter]")].map(n => [n.dataset.deepFilter, n.value]);
    this.results.replaceChildren();
    this.renderedData = signature;
    if (data.schema_version === GOAL_SCHEMA) {
      renderGoalSummary(this.results, data, this.onExplore); return;
    }
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
