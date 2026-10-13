import { element, renderMarkdown } from "../markdown.mjs";
import { FeedView } from "./feed.mjs";

const option = (label, value) => { const node = element("option", "", label); node.value = value; return node; };
const button = (label, action) => { const node = element("button", "quiet", label); node.type = "button"; node.addEventListener("click", action); return node; };
const active = new Set(["preparing", "queued", "running", "cancelling"]);
const resumable = new Set(["paused", "budget_exhausted", "interrupted"]);
const historyLabel = job => `${job.workspace?.branch || `${job.mode === "worktree" ? "worktree" : "read-only"} · ${job.id}`} · ${job.state} · ${job.created_at}`;
const budgetLabels = { max_steps: "Steps", max_llm_calls: "Model calls", max_tool_calls: "Tool calls", max_tokens: "Tokens", max_duration_seconds: "Seconds" };
// Older running services enforce these limits but do not include them in the response.
const legacyBudgetLimits = { max_steps: 100, max_llm_calls: 200, max_tokens: 2000000, max_duration_seconds: 3600, max_tool_calls: 500 };
const number = value => value.toLocaleString("en-US");

export class DiagnosisView {
  constructor(root) { this.root = root; }
  close() {
    this.abort?.abort();
    clearTimeout(this.timer);
    clearTimeout(this.eventTimer);
    this.feed?.destroy();
    this.dialog?.remove();
    this.generation = (this.generation || 0) + 1;
    this.busy = false;
    this.loadingEvents = false;
  }
  async open(api, sessionId) {
    this.close();
    this.abort = new AbortController();
    this.api = api; this.sessionId = sessionId; this.after = 0; this.job = null; this.workspaceKey = null;
    this.commandError = null;
    this.root.replaceChildren();
    this.notice = element("p", "notice"); this.notice.hidden = true;
    const toolbar = element("div", "dashboard-toolbar diagnosis-toolbar");
    this.run = element("select"); this.run.setAttribute("aria-label", "Diagnosis run");
    this.run.append(option("Latest run", "latest"), option("All runs", "all"));
    this.model = element("select"); this.model.setAttribute("aria-label", "Diagnosis model");
    this.model.append(option("Session default model", ""));
    this.mode = element("select"); this.mode.setAttribute("aria-label", "Diagnosis mode");
    this.mode.append(option("Worktree verification", "worktree"), option("Read-only investigation", "read_only"));
    this.cleanup = element("select"); this.cleanup.setAttribute("aria-label", "Diagnosis cleanup");
    this.cleanup.append(option("Auto-delete worktree", "delete"), option("Keep for review", "keep"));
    this.mode.addEventListener("change", () => { this.cleanup.disabled = this.mode.value === "read_only"; });
    this.start = button("Diagnose session", () => this.command("start"));
    this.start.disabled = true;
    this.stop = button("Stop", () => this.command("cancel")); this.stop.hidden = true;
    this.resume = button("Resume diagnosis", () => this.command("resume")); this.resume.disabled = true;
    const selectors = element("div", "diagnosis-selectors"), actions = element("div", "diagnosis-actions");
    selectors.append(this.run, this.model, this.mode, this.cleanup);
    actions.append(this.start, this.resume, this.stop, button("Refresh", () => this.open(api, sessionId)));
    toolbar.append(selectors, actions);
    this.question = element("textarea"); this.question.rows = 2; this.question.maxLength = 4000;
    this.question.placeholder = "Optional: what went wrong?"; this.question.setAttribute("aria-label", "Diagnosis question");
    const advanced = this.budgetPanel = element("details", "diagnosis-budget"); advanced.append(element("summary", "", "Budgets"));
    this.budgetFields = {};
    this.budgetRanges = {};
    const fields = element("div", "diagnosis-budget-fields");
    for (const [key, label] of Object.entries(budgetLabels)) {
      const field = element("label", "", label), input = element("input");
      input.type = "number"; input.min = "1"; input.step = "1"; input.setAttribute("aria-label", `Diagnosis ${label.toLowerCase()}`);
      const range = element("small", "muted"); range.id = `diagnosis-budget-${key}-range`;
      input.setAttribute("aria-describedby", range.id);
      input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
      field.append(input, range); fields.append(field); this.budgetFields[key] = input; this.budgetRanges[key] = range;
    }
    advanced.append(fields);
    this.history = element("select"); this.history.setAttribute("aria-label", "Diagnosis history");
    this.history.append(option("No diagnoses yet", ""));
    this.history.addEventListener("change", () => this.select(this.history.value));
    this.status = element("div", "dashboard-toolbar diagnosis-status"); this.status.setAttribute("role", "status");
    this.workspacePanel = element("section", "diagnosis-workspace");
    this.report = element("section", "diagnosis-report");
    this.trace = element("details", "diagnosis-trace"); this.trace.append(element("summary", "", "Investigation activity"));
    this.feedRoot = element("div", "diagnosis-feed"); this.trace.append(this.feedRoot);
    this.trace.addEventListener("toggle", () => { if (this.trace.open) void this.loadEvents(); });
    this.root.append(toolbar, this.question, advanced, this.notice, this.history, this.status, this.workspacePanel, this.report, this.trace);
    const generation = this.generation;
    try {
      if (!api.diagnoses) throw new Error("Session diagnosis is unavailable on this connection");
      const data = await api.diagnoses(sessionId, this.abort.signal);
      if (generation !== this.generation || this.abort.signal.aborted) return;
      for (const [index, run] of data.runs.entries()) this.run.append(option(`Run ${index + 1} · ${run}`, run));
      for (const model of data.models) this.model.append(option(model, model));
      this.model.value = data.models.includes(data.default_model) ? data.default_model : "";
      this.supportsWorktree = data.default_mode === "worktree";
      this.mode.hidden = this.cleanup.hidden = !this.supportsWorktree;
      for (const [key, input] of Object.entries(this.budgetFields)) {
        const maximum = (data.budget_limits || legacyBudgetLimits)[key];
        if (maximum) input.max = String(maximum);
        this.budgetRanges[key].textContent = maximum ? `1–${number(maximum)}` : "Integer ≥ 1";
      }
      this.setBudgets(data.budgets);
      this.jobs = data.jobs; this.renderHistory(); this.start.disabled = false;
      if (data.source_error) { this.error(new Error(data.source_error)); this.start.disabled = true; }
      if (data.jobs.length) await this.select(data.jobs[0].id);
      else this.report.append(element("p", "muted", this.supportsWorktree ? "Source · Trace · Configuration · Assembly · Isolated verification" : "Read-only investigation · Restart the service to enable worktree verification"));
    } catch (error) { if (generation === this.generation) this.error(error); }
  }
  error(error) {
    if (this.abort.signal.aborted) return;
    this.notice.textContent = error.message; this.notice.hidden = false;
  }
  setBudgets(budgets) { for (const [key, value] of Object.entries(budgets || {})) if (this.budgetFields[key]) this.budgetFields[key].value = value; }
  budgets() {
    const result = {}, errors = [];
    let firstInvalid;
    for (const [key, input] of Object.entries(this.budgetFields)) {
      const value = Number(input.value), maximum = input.max ? Number(input.max) : Infinity;
      result[key] = value;
      if (!Number.isInteger(value) || value < 1 || value > maximum) {
        input.setAttribute("aria-invalid", "true"); firstInvalid ||= input;
        const received = input.value ? (Number.isFinite(value) ? number(value) : input.value) : "empty";
        errors.push(`${budgetLabels[key]} (${key}): received ${received}; ${Number.isFinite(maximum) ? `allowed integer range is 1–${number(maximum)}` : "must be an integer ≥ 1"}`);
      } else input.removeAttribute("aria-invalid");
    }
    if (errors.length) {
      this.budgetPanel.open = true; firstInvalid.focus();
      throw new Error(`Invalid diagnosis budgets: ${errors.join("; ")}`);
    }
    return result;
  }
  renderHistory() {
    this.history.replaceChildren(...this.jobs.map(job => option(historyLabel(job), job.id)));
    if (this.job) this.history.value = this.job.id;
  }
  async command(action) {
    if (this.busy) return;
    this.busy = true; this.start.disabled = this.stop.disabled = this.resume.disabled = true;
    this.notice.hidden = true; this.commandError = null;
    const generation = this.generation;
    try {
      const job = action === "start" ? await this.api.startDiagnosis(this.sessionId, {
        request_id: globalThis.crypto.randomUUID(), run_id: this.run.value, model: this.model.value || null,
        question: this.question.value, budgets: this.budgets(),
        ...(this.supportsWorktree ? { mode: this.mode.value, cleanup: this.cleanup.value } : {}),
      }, this.abort.signal) : await this.api.diagnosisCommand(this.sessionId, this.job.id, action,
        action === "resume" ? { budgets: this.budgets() } : action === "merge" ? { patch_sha256: this.reviewedSha } : {}, this.abort.signal);
      if (generation !== this.generation || this.abort.signal.aborted) return;
      this.jobs = [job, ...this.jobs.filter(existing => existing.id !== job.id)];
      this.renderHistory(); await this.select(job.id);
    } catch (error) {
      if (generation === this.generation) { this.commandError = error.message; this.error(error); }
    }
    finally {
      if (generation === this.generation) this.busy = false;
      if (generation === this.generation && !this.abort.signal.aborted) {
        this.start.disabled = this.stop.disabled = false;
        this.resume.disabled = !resumable.has(this.job?.state);
      }
    }
  }
  async reviewChanges() {
    const generation = this.generation, id = this.job.id;
    try {
      const data = await this.api.diagnosisChanges(this.sessionId, id, this.abort.signal);
      if (generation !== this.generation || this.selected !== id || this.abort.signal.aborted) return;
      this.dialog?.remove();
      const dialog = element("dialog", "trajectory-evidence"); this.dialog = dialog;
      this.reviewedSha = data.workspace.patch_sha256;
      dialog.append(element("h3", "", "Diagnostic changes"), element("p", "", `Target: ${data.workspace.target_branch} · ${data.workspace.files.length} files`),
        element("p", "muted", "Merge applies this patch to the original working directory. It does not stage, commit, or restart the service."),
        element("pre", "evidence-text", data.patch || "No changes"), button("Close", () => dialog.remove()));
      if (data.workspace.files.length && data.workspace.state !== "merged") {
        dialog.append(button("Merge reviewed changes", () => { dialog.remove(); void this.command("merge"); }));
      }
      this.root.append(dialog); dialog.showModal();
    } catch (error) { if (generation === this.generation) this.error(error); }
  }
  renderWorkspace(job) {
    const workspace = job.workspace;
    const key = JSON.stringify([workspace, job.workspace_error, job.state]);
    if (this.workspaceKey === key) return;
    this.workspaceKey = key; this.workspacePanel.replaceChildren();
    if (!workspace) return;
    this.workspacePanel.append(element("h3", "", "Verification workspace"));
    const bar = element("div", "dashboard-toolbar");
    bar.append(element("span", "metric-chip", workspace.state), element("code", "", workspace.branch),
      element("span", "metric-chip", workspace.cleanup === "delete" ? "Auto-delete · patch retained" : "Keep for review"));
    const stopped = ["completed", "failed", "cancelled"].includes(job.state);
    if (workspace.patch_sha256) bar.append(button("Review patch", () => this.reviewChanges()));
    if (stopped && workspace.state === "review") bar.append(button("Delete worktree", () => this.command("discard")));
    this.workspacePanel.append(bar, element("p", "muted", workspace.path));
    if (workspace.files?.length) this.workspacePanel.append(element("p", "", `${workspace.files.length} changed files · ${workspace.patch_bytes.toLocaleString()} bytes`));
    if (job.workspace_error) this.workspacePanel.append(element("p", "notice", job.workspace_error));
    if (workspace.excluded?.length) {
      const excluded = element("details"); excluded.append(element("summary", "", `${workspace.excluded.length} files excluded from baseline`),
        element("pre", "evidence-text", workspace.excluded.join("\n"))); this.workspacePanel.append(excluded);
    }
    if (workspace.checks?.length) {
      const table = element("table", "diagnosis-checks"), heading = element("tr");
      for (const text of ["Check", "Result", "Exit", "Seconds", "Tested patch"]) heading.append(element("th", "", text));
      table.append(heading);
      for (const check of workspace.checks) {
        const row = element("tr"), cell = element("td"), details = element("details");
        details.append(element("summary", "", check.argv.join(" ")), element("pre", "evidence-text", check.output)); cell.append(details);
        row.append(cell, element("td", "", check.status), element("td", "", String(check.exit_code ?? "—")),
          element("td", "", String(check.seconds)), element("td", "", check.patch_sha256.slice(0, 12) + (workspace.patch_sha256 ? (workspace.patch_sha256 === check.patch_sha256 ? " · final patch" : " · earlier patch") : "")));
        table.append(row);
      }
      this.workspacePanel.append(table);
    }
  }
  async select(id) {
    clearTimeout(this.timer);
    this.selected = id; this.after = 0; this.job = null; this.reportKey = null;
    this.feed?.destroy();
    this.feed = new FeedView(this.feedRoot, {
      loadArtifact: digest => this.api.diagnosisArtifact(this.sessionId, id, digest, this.abort.signal),
    });
    await this.poll(id, true);
  }
  async poll(id, initial = false) {
    const generation = this.generation;
    try {
      const job = await this.api.diagnosis(this.sessionId, id, this.abort.signal);
      if (generation !== this.generation || this.selected !== id || this.abort.signal.aborted) return;
      this.job = job; this.history.value = id;
      const choice = [...this.history.options].find(option => option.value === id);
      if (choice) choice.textContent = historyLabel(job);
      this.history.title = historyLabel(job);
      if (initial) this.setBudgets(job.budgets);
      this.render(job);
      if (this.trace.open) await this.loadEvents();
      if (active.has(job.state) || (job.workspace?.state === "active" && ["completed", "failed", "cancelled"].includes(job.state))) this.timer = setTimeout(() => this.poll(id), 1000);
    } catch (error) { if (generation === this.generation) this.error(error); }
  }
  async loadEvents() {
    if (!this.job || this.loadingEvents) return;
    this.loadingEvents = true;
    const generation = this.generation, id = this.job.id;
    try {
      // Bounded batches preserve responsiveness; continue until the durable cursor is caught up.
      for (let page = 0; page < 10; page++) {
        const result = await this.api.diagnosisEvents(this.sessionId, id, this.after, this.abort.signal);
        if (generation !== this.generation || this.selected !== id || this.abort.signal.aborted) return;
        for (const event of result.events) this.feed.append(event);
        this.after = result.next_after;
        if (result.events.length < 200) break;
        if (page === 9 && this.trace.open) this.eventTimer = setTimeout(() => this.loadEvents(), 20);
      }
    } catch (error) { if (generation === this.generation) this.error(error); }
    finally { if (generation === this.generation) this.loadingEvents = false; }
  }
  async evidence(ref) {
    const generation = this.generation, id = this.job.id;
    try {
      const page = await this.api.diagnosisEvidence(this.sessionId, id, ref, this.abort.signal);
      if (generation !== this.generation || this.selected !== id || this.abort.signal.aborted) return;
      this.dialog?.remove();
      const dialog = element("dialog", "trajectory-evidence"); this.dialog = dialog;
      dialog.append(element("h3", "", ref), button("Close", () => dialog.remove()), element("pre", "evidence-text", page.content));
      this.root.append(dialog); dialog.showModal();
    } catch (error) { this.error(error); }
  }
  refs(ids) {
    const node = element("div", "diagnosis-refs");
    for (const ref of ids) node.append(button(ref, () => this.evidence(ref)));
    return node;
  }
  updateCursor(cursor) {
    if (this.job && cursor > this.job.source_cursor && !this.job.stale) {
      this.job.stale = true;
      this.render(this.job);
    }
  }
  render(job) {
    this.renderWorkspace(job);
    const c = job.counters || {};
    this.status.replaceChildren(...[
      job.state.replaceAll("_", " "), `${c.seconds || 0}s`, `${c.model_calls || 0} model calls`,
      `${c.tool_calls || 0} tool calls`, `${number(c.tokens || 0)} / ${number(job.budgets.max_tokens)} tokens`, `Snapshot #${job.source_cursor}`,
      ...(c.calls_without_reported_usage ? [`${c.calls_without_reported_usage} calls without reported usage`] : []),
      ...(c.unfinished_model_calls ? [`${c.unfinished_model_calls} unfinished model calls`] : []),
      ...(job.stale ? ["New events available"] : []),
    ].map(text => element("span", "metric-chip", text)));
    const budget = job.wrap_up?.token_budget || job.token_budget;
    if (budget) {
      const threshold = element("span", "metric-chip", `Token wrap-up at ${number(budget.wrap_up_at)}`);
      threshold.title = `Reserves ${number(budget.reserve_tokens)} tokens; last model call: ${number(budget.last_call_tokens)}. `
        + "Recomputed after model calls. Threshold = budget − min(80% of budget, max(15% of budget, 2,048, 2 × last call)). "
        + "Crossing it stops investigation, generates a partial summary if possible, then pauses. Summary tokens add to the displayed total.";
      this.status.append(threshold, element("span", "metric-chip", `${number(budget.reserve_tokens)} reserved`));
    }
    this.status.append(element("span", "diagnosis-activity", job.current_activity || ""));
    this.stop.hidden = !active.has(job.state); this.resume.disabled = this.busy || !resumable.has(job.state);
    const error = this.commandError || job.error;
    this.notice.hidden = !error;
    if (error) this.notice.textContent = error;
    const key = JSON.stringify([job.state, job.report, job.partial_report]);
    if (this.reportKey === key) return;
    this.reportKey = key; this.report.replaceChildren();
    if (!job.report) {
      this.report.append(element("p", "muted", active.has(job.state) ? "Investigating…" : "No accepted report. Review investigation activity."));
      if (job.partial_report) {
        const partial = element("details"); partial.append(element("summary", "", "Partial report · not accepted"), renderMarkdown(job.partial_report));
        this.report.append(partial);
      }
      return;
    }
    const report = job.report;
    const conclusion = element("div", "diagnosis-conclusion");
    conclusion.append(element("span", "metric-chip", report.status), element("p", "", report.summary)); this.report.append(conclusion);
    const table = element("table", "diagnosis-findings");
    const head = element("tr"); for (const label of ["Finding", "Layer", "Confidence", "Cause & evidence", "Recommendation & validation"]) head.append(element("th", "", label));
    const thead = element("thead"); thead.append(head); table.append(thead); const body = element("tbody");
    for (const finding of report.findings) {
      const row = element("tr"), cause = element("td"), recommendation = element("td");
      cause.append(element("p", "", finding.cause), this.refs(finding.evidence_ids));
      recommendation.append(element("p", "", finding.recommendation), element("strong", "", job.mode === "worktree" ? "Validation" : "Proposed validation · not executed"), element("p", "", finding.validation), element("p", "muted", `Risk: ${finding.risk}`));
      row.append(element("td", "", finding.title), element("td", "", finding.layer), element("td", "", finding.confidence), cause, recommendation); body.append(row);
    }
    table.append(body); const scroll = element("div", "diagnosis-table"); scroll.append(table); this.report.append(scroll);
    if (report.causal_chain?.length) {
      const chain = element("section", "diagnosis-chain"); chain.append(element("h3", "", "Causal chain"));
      for (const link of report.causal_chain) {
        const edge = element("div", `diagnosis-link ${link.relation}`);
        edge.append(element("span", "", link.from), element("span", "", `→ ${link.relation} →`), element("span", "", link.to), this.refs(link.evidence_ids)); chain.append(edge);
      }
      this.report.append(chain);
    }
    const coverage = element("div", "diagnosis-coverage");
    for (const key of ["checked", "missing"]) {
      const section = element("section"); section.append(element("h3", "", key === "checked" ? "Checked" : "Evidence gaps"));
      const list = element("ul"); for (const item of report.coverage[key]) list.append(element("li", "", item)); section.append(list); coverage.append(section);
    }
    const full = element("details"); full.append(element("summary", "", "Full report"), renderMarkdown(job.markdown));
    this.report.append(coverage, full);
  }
}
