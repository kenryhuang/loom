import {SkillsView} from './skills.mjs';
import { renderBaseEvaluation } from "./behavior-evaluation.mjs";
import { renderBehaviorSequence } from "./behavior-sequence.mjs";
import { element } from "../markdown.mjs";
import { seconds, table, badge } from "./dashboard.mjs";
import { EvaluationView } from "./evaluation.mjs";
import { DiagnosisView } from "./diagnosis.mjs";
import { renderVerificationArtifact } from "./activity-row.mjs";

const number = (value) => (value == null ? "—" : value.toLocaleString());
const option = (label, value) => {
  const node = element("option", "", label);
  node.value = value;
  return node;
};
const button = (label, action) => {
  const node = element("button", "quiet", label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
};
function wait(signal) {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, 800);
    signal.addEventListener("abort", finish, { once: true });
    if (signal.aborted) finish();
  });
}
function preview(text) {
  try {
    const value = JSON.parse(
      text.replace(/^\s*```(?:json)?\s*\n|\n```\s*$/g, ""),
    );
    return value.reasoning || value.action?.description || value.report || text;
  } catch {
    return text;
  }
}

// Keep every field, and render embedded text without JSON escape sequences.
function readableEvidence(text) {
  let remaining = 400;
  const render = (value, depth = 0) => {
    if (typeof value === "string" && depth < 8) {
      const fenced = /^\s*```(?:json)?\s*\n([\s\S]*?)\n```\s*$/i.exec(value);
      try {
        const parsed = JSON.parse(fenced ? fenced[1] : value);
        if (parsed !== value) return render(parsed, depth + 1);
      } catch {
        /* Plain text or an incomplete evidence page. */
      }
    }
    if (value && typeof value === "object" && depth < 8 && remaining > 0) {
      const entries = Object.entries(value);
      if (entries.length && entries.length <= remaining) {
        remaining -= entries.length;
        const root = element("dl", "evidence-fields");
        for (const [key, child] of entries) {
          const field = element("div", "evidence-field");
          const body = element("dd");
          body.append(render(child, depth + 1));
          field.append(
            element("dt", "", Array.isArray(value) ? `[${key}]` : key),
            body,
          );
          root.append(field);
        }
        return root;
      }
    }
    return element(
      "pre",
      "evidence-text",
      typeof value === "string" ? value : JSON.stringify(value, null, 2),
    );
  };
  return render(text);
}

export class TrajectoryView {
  constructor(root, { onError = () => {} }) {
    this.root = root;
    this.onError = onError;
  }
  close() {
    this.diagnosisView?.close();
    this.abort?.abort();
    this.dialog?.remove();
    this.dialog = null;
    this.sessionId = null;
    this.job = null;
    this.root.replaceChildren();
    this.root.hidden = true;
  }
  updateState(state) {
    if (state.session_id !== this.sessionId) return;
    this.cursor = state.event_cursor;
    this.diagnosisView?.updateCursor(this.cursor);
    this.snapshotLabel();
  }
  snapshotLabel() {
    if (!this.job || !this.status) return;
    const stale = this.cursor > this.job.source_cursor;
    this.status.textContent = this.job.state === "completed"
      ? (stale ? "New events available" : "") : "Analyzing…";
    this.status.title = `Snapshot through event ${this.job.source_cursor}`;
    this.refresh.title = this.status.title;
    this.status.hidden = this.selectedTab !== 0 || (this.job.state === "completed" && !stale);
  }
  async open(api, sessionId, cursor = 0) {
    const selectedRun = this.sessionId === sessionId ? this.runFilter?.value || "" : "";
    this.close();
    this.selectedRun = selectedRun;
    this.api = api;
    this.sessionId = sessionId;
    this.cursor = cursor;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    this.root.hidden = false;
    const heading = element("div", "trajectory-heading dashboard-toolbar");
    this.runFilter = element("select");
    this.runFilter.setAttribute("aria-label", "Filter trajectory by run");
    this.runFilter.append(option("All runs", ""));
    this.runFilter.disabled = true;
    this.refresh = button("Refresh analysis", () => this.open(api, sessionId, this.cursor));
    this.refresh.disabled = true;
    this.status = element("span", "dashboard-sync", "Preparing analysis…");
    this.status.setAttribute("role", "status");
    heading.append(this.runFilter, this.status, this.refresh);
    this.body = element("div", "trajectory-body");
    const tabs = element("div", "evaluation-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Evaluation views");
    this.deepPanel = element("section", "trajectory-evaluation");
    this.diagnosisPanel = element("section", "trajectory-diagnosis");
    this.skillsPanel = element('section', 'trajectory-skills');
    this.skillsView = new SkillsView(this.skillsPanel, {api, sessionId, signal});
    const panels = [this.body, this.deepPanel, this.diagnosisPanel, this.skillsPanel];
    this.tabs = ["Trace analysis", "Deep evaluation", "Session diagnosis", "Skills"].map((label, index) => {
      const tab = button(label, () => this.selectTab(index));
      tab.id = `evaluation-tab-${index}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", `evaluation-content-${index}`);
      panels[index].id = `evaluation-content-${index}`;
      panels[index].setAttribute("role", "tabpanel");
      panels[index].setAttribute("aria-labelledby", tab.id);
      panels[index].tabIndex = 0;
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          return;
        event.preventDefault();
        const next =
          event.key === "Home" ? 0 : event.key === "End" ? panels.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + panels.length) % panels.length;
        this.selectTab(next);
        this.tabs[next].focus();
      });
      tabs.append(tab);
      return tab;
    });
    this.deepPanel.append(
      element("p", "muted", "Preparing snapshot for evaluation…"),
    );
    this.root.append(element('p', 'analysis-session-id', `Session: ${sessionId}`), tabs, heading,
      this.body, this.deepPanel, this.diagnosisPanel, this.skillsPanel);
    this.diagnosisView = new DiagnosisView(this.diagnosisPanel);
    void this.diagnosisView.open(api, sessionId);
    this.traceHeading = heading;
    this.selectTab(this.selectedTab || 0);
    try {
      const started = await api.startTrajectory(sessionId, signal);
      if (signal.aborted) return;
      this.job = started;
      this.snapshotLabel();
      while (!signal.aborted) {
        const job = await api.trajectory(sessionId, this.job.id, signal);
        if (signal.aborted) return;
        this.job = job;
        if (job.state === "failed")
          throw new Error(job.error || "Analysis failed");
        if (job.state === "completed") {
          this.analysis = job.analysis;
          this.render();
          this.snapshotLabel();
          return;
        }
        await wait(signal);
      }
    } catch (error) {
      if (signal.aborted) return;
      this.status.textContent = "Analysis unavailable";
      this.body.replaceChildren(element("p", "notice error", error.message));
      this.deepPanel.replaceChildren(
        element("p", "notice error", error.message),
      );
      this.onError(error);
    } finally {
      if (!signal.aborted) this.refresh.disabled = false;
    }
  }
  selectTab(index) {
    this.selectedTab = index;
    this.body.hidden = index !== 0;
    this.deepPanel.hidden = index !== 1;
    this.diagnosisPanel.hidden = index !== 2;
    this.skillsPanel.hidden = index !== 3;
    if (index === 3 && !this.skillsView.data) void this.skillsView.open();
    this.traceHeading.hidden = index !== 0;
    this.status.hidden = index !== 0 || !this.status.textContent;
    this.tabs.forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
    });
  }
  render() {
    const data = this.analysis;
    this.body.replaceChildren();
    this.runFilter.replaceChildren(option("All runs", ""));
    const runIds = [...new Set([...data.run_ids, ...Object.keys(data.base?.statistics?.by_run || {})])];
    for (const [index, id] of runIds.entries()) this.runFilter.append(option(`Run ${index + 1} · ${id}`, id));
    if (runIds.includes(this.selectedRun)) this.runFilter.value = this.selectedRun;
    this.runFilter.disabled = false;
    if (this.api.evaluations) {
      const evaluation = element("section", "trajectory-evaluation");
      this.deepPanel.replaceChildren(evaluation);
      this.evaluationView = new EvaluationView(evaluation, {
        api: this.api,
        sessionId: this.sessionId,
        analysisId: this.job.id,
        signal: this.abort.signal,
        evidence: (ref) => this.showEvidence(ref),
        onError: this.onError,
        onInduce: query => {this.selectTab(3); this.skillsView.query = query; void this.skillsView.open(query);},
      });
      this.evaluationView.open();
    } else {
      this.deepPanel.textContent =
        "Deep evaluation is unavailable on this service.";
    }
    const cards = element("div", "trajectory-metrics");
    for (const [label, value] of [
      ["Model calls", data.rounds.length],
      ["Tool calls", data.tools.length],
      ...(data.base?.metrics
        ? [
            ["Steps", data.base.metrics.steps],
            [
              "Recorded duration (seconds)",
              data.base.metrics.duration_ms == null
                ? null
                : Math.round(data.base.metrics.duration_ms / 1000),
            ],
          ]
        : []),
      ["Recorded tokens", data.tokens.known_total_tokens],
      [
        "Tool failures",
        data.tools.filter((tool) => tool.status === "failed").length,
      ],
    ]) {
      const card = element("div", "trajectory-metric");
      card.append(
        element("small", "muted", label),
        element("strong", "", number(value)),
      );
      cards.append(card);
    }
    if (!data.base?.statistics) this.body.append(cards);
    if (data.base)
      renderBaseEvaluation(
        this.body,
        data.base,
        data.goal_revisions,
        data.plan_revisions,
        {evidence: ref => this.showEvidence(ref), artifact: digest => this.showArtifact(digest), scope: this.runFilter},
      );
    renderBehaviorSequence(this.body, data.sequence, {evidence: ref => this.showEvidence(ref)});
    const missing = data.rounds.filter(
      (row) => row.usage.total_tokens == null,
    ).length;
    if (missing && !data.base?.statistics)
      this.body.append(
        element(
          "p",
          "notice",
          `${missing} model calls have no measured total usage. Token totals include only reported values.`,
        ),
      );
    if (
      !data.base?.statistics && (data.coverage.missing_artifacts.length ||
      data.coverage.unscoped_model_events)
    )
      this.body.append(
        element(
          "p",
          "notice",
          `Incomplete evidence: ${data.coverage.missing_artifacts.length} unavailable event details; ${data.coverage.unscoped_model_events} model events could not be placed in the timeline.`,
        ),
      );
    const tasks = element("details", "trajectory-section");
    tasks.append(
      element(
        "summary",
        "",
        `Recorded task definitions · ${data.task_contracts.length}`,
      ),
    );
    for (const task of data.task_contracts)
      tasks.append(
        element("p", "", task.objective || "Task objective not recorded"),
      );
    if (data.task_contracts.length) this.body.append(tasks);
    if (data.failures.length) {
      const errors = element("details", "trajectory-section");
      errors.append(
        element(
          "summary",
          "",
          `Recorded errors and recovery · ${data.failures.length}`,
        ),
      );
      for (const failure of data.failures) {
        const row = element("div", "trajectory-error");
        row.append(
          element("p", "", `${failure.type} · ${failure.message}`),
          this.evidenceButton(`Event ${failure.seq}`, failure.ref),
        );
        errors.append(row);
      }
      this.body.append(errors);
    }
    const filters = element("div", "trajectory-filters");
    this.statusFilter = element("select");
    this.statusFilter.setAttribute("aria-label", "Filter trajectory by status");
    for (const [label, value] of [
      ["All calls", ""],
      ["With failures", "failed"],
      ["Incomplete calls", "partial"],
    ])
      this.statusFilter.append(option(label, value));
    this.search = element("input");
    this.search.placeholder = "Find a call or tool…";
    this.search.setAttribute("aria-label", "Search trajectory");
    for (const field of [this.runFilter, this.statusFilter, this.search])
      field.addEventListener("input", () => this.renderRounds());
    this.runFilter.addEventListener("change", () => this.renderRounds());
    const callSection = element("section", "dashboard-panel dashboard-calls");
    const callHeading = element("header", "dashboard-panel-heading");
    callHeading.append(element("h3", "", "Call explorer"));
    filters.append(this.statusFilter, this.search);
    this.timeline = element("div", "trajectory-timeline");
    callSection.append(callHeading, filters, this.timeline);
    this.body.append(callSection);
    this.renderRounds();
  }
  renderRounds() {
    this.timeline.replaceChildren();
    const data = this.analysis, query = this.search.value.trim().toLowerCase();
    const matches = (call, text) => {
      if (this.runFilter.value && call.run_id !== this.runFilter.value) return false;
      if (this.statusFilter.value && call.status !== this.statusFilter.value) return false;
      return !query || text.toLowerCase().includes(query);
    };
    const makeTable = (title, headers) => {
      const section = element("section", "call-explorer-group");
      section.append(element("h4", "", title));
      const node = table(section, headers, []);
      const body = node.querySelector("tbody"); body.replaceChildren();
      node.setAttribute("aria-label", title);
      this.timeline.append(section);
      return body;
    };
    const empty = (body, columns, text) => {
      if (body.children.length) return;
      const row = element("tr"), cell = element("td", "empty-state", text);
      cell.colSpan = columns; row.append(cell); body.append(row);
    };
    const tools = makeTable("Tool calls", ["Tool call", "Execute time", "Status"]);
    for (const tool of data.tools) {
      if (!matches(tool, `${tool.tool_id} ${tool.tool_call_id || ""} ${tool.output_excerpt || ""}`)) continue;
      this.callRow(tools, tool.id, [tool.tool_id, seconds(tool.duration_ms), this.callStatus(tool.status)], this.tool(tool));
    }
    empty(tools, 3, data.tools.length ? "No calls match these filters." : "No tool calls have been recorded in this session yet.");
    const models = makeTable("Model calls", ["Model", "TTFT", "TPS", "Token in", "Token out", "Duration", "Status"]);
    for (const round of data.rounds) {
      if (!matches(round, `${round.model} ${round.summary} ${round.llm_call_id}`)) continue;
      const {body, load} = this.modelDetail(round);
      this.callRow(models, round.id, [round.model || "Unknown model", seconds(round.ttft_ms),
        round.tps == null ? "—" : `${number(Math.round(round.tps * 10) / 10)} tok/s`,
        number(round.usage.prompt_tokens), number(round.usage.completion_tokens), seconds(round.duration_ms), this.callStatus(round.status)], body, load);
    }
    empty(models, 7, data.rounds.length ? "No calls match these filters." : "No model calls have been recorded in this session yet.");
  }
  callStatus(status) {
    const name = {complete: "Success", partial: "Incomplete", failed: "Failed", cancelled: "Cancelled", unknown: "Unknown"}[status] || status;
    return badge(name, status === "complete" ? "success" : status);
  }
  callRow(target, id, values, content, load) {
    const row = element("tr", "call-explorer-row"), detail = element("tr", "call-explorer-detail");
    const cell = element("td"); cell.colSpan = values.length;
    detail.hidden = true; cell.append(content); detail.append(cell);
    const toggle = element("button", "quiet call-explorer-toggle", `▸ ${values[0]}`);
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    row.dataset.callId = id;
    detail.id = `call-detail-${id}`;
    toggle.setAttribute("aria-controls", detail.id);
    toggle.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      toggle.setAttribute("aria-expanded", String(!detail.hidden));
      toggle.textContent = `${detail.hidden ? "▸" : "▾"} ${values[0]}`;
      if (!detail.hidden) load?.();
    });
    for (const [index, value] of values.entries()) {
      const cell = element("td");
      if (!index) cell.append(toggle);
      else if (value?.nodeType) cell.append(value);
      else cell.textContent = value;
      row.append(cell);
    }
    target.append(row, detail);
  }
  modelDetail(round) {
    const body = element("div", "trajectory-round-detail");
    body.append(element("p", "muted small", `Event ${round.seq} · ${round.at || "Time unknown"} · ${round.run_id} · ${round.llm_call_id}`));
    if (round.summary) body.append(element("p", "call-response-preview", preview(round.summary)));
    const evidence = element("div", "trajectory-actions");
    if (round.request_ref) evidence.append(this.evidenceButton("Request", round.request_ref));
    if (round.response_ref) evidence.append(this.evidenceButton("Response", round.response_ref));
    for (const ref of round.evidence_refs)
      if (ref.line_number !== round.request_ref?.line_number && ref.line_number !== round.response_ref?.line_number)
        evidence.append(this.evidenceButton("Failure evidence", ref));
    body.append(evidence, element("p", "small",
      `Context: ${number(round.context.message_count)} messages · ${round.context_changes.added} added · ${round.context_changes.retained} retained · ${round.context_changes.removed} removed`));
    const extra = element("div"); body.append(extra);
    let loaded = false;
    const load = async () => {
      if (loaded) return;
      loaded = true;
      const signal = this.abort.signal;
      extra.textContent = "Loading context and verification evidence…";
      try {
        const detail = await this.api.trajectoryRound(this.sessionId, this.job.id, round.id, signal);
        if (signal.aborted) return;
        extra.replaceChildren();
        for (const [label, rows] of [["Context changes", detail.context_deltas], ["Recorded verification evidence", detail.verification_evidence]]) {
          const disclosure = element("details", "trajectory-section");
          disclosure.append(element("summary", "", label), element("pre", "", JSON.stringify(rows, null, 2)));
          extra.append(disclosure);
        }
      } catch (error) {
        if (signal.aborted) return;
        extra.textContent = `Could not load call details: ${error.message}. Close and reopen to retry.`;
        loaded = false;
        this.onError(error);
      }
    };
    return {body, load};
  }
  tool(tool) {
    const node = element(
      "div",
      `trajectory-tool ${tool.status === "failed" ? "error" : ""}`,
    );
    node.append(
      element(
        "strong",
        "",
        `${tool.tool_id} · ${tool.status}${tool.exit_code == null ? "" : ` · exit ${tool.exit_code}`}`,
      ),
    );
    if (tool.seq)
      node.append(
        element(
          "p",
          "muted small",
          `Event ${tool.seq} · ${tool.at || "Time unknown"}`,
        ),
      );
    const actions = element("div", "trajectory-actions");
    if (tool.input_ref)
      actions.append(this.evidenceButton("Input", tool.input_ref));
    if (tool.raw_output_ref)
      actions.append(this.evidenceButton("Output", tool.raw_output_ref));
    node.append(actions, readableEvidence(tool.output_excerpt || ""));
    if (tool.injection_count)
      node.append(
        element(
          "small",
          "muted",
          `Result found in ${tool.injection_count} later context messages; semantic use is not assessed.`,
        ),
      );
    return node;
  }
  evidenceButton(label, ref) {
    return button(label, () => this.showEvidence(ref, label));
  }
  async showArtifact(digest) {
    this.dialog?.remove();
    const dialog = element("dialog", "trajectory-evidence"), content = element("div", "", "Loading artifact…");
    this.dialog = dialog;
    dialog.append(button("Close", () => dialog.close()), element("h2", "", "Recorded artifact"), content);
    dialog.addEventListener("close", () => dialog.remove(), {once: true});
    document.body.append(dialog); dialog.showModal();
    const signal = this.abort.signal;
    try {
      const value = await this.api.artifact(this.sessionId, digest, signal);
      if (!signal.aborted && this.dialog === dialog)
        content.replaceChildren(renderVerificationArtifact(value) || readableEvidence(JSON.stringify(value)));
    } catch (error) {
      if (!signal.aborted && this.dialog === dialog) content.textContent = error.message;
    }
  }
  async showEvidence(ref, label = "Recorded evidence") {
    this.dialog?.remove();
    const dialog = element("dialog", "trajectory-evidence");
    this.dialog = dialog;
    const title = element("h2", "", label),
      status = element("p", "muted small"),
      content = element("div", "evidence-content");
    let offset = ref.start || 0,
      raw = "",
      rawMode = false;
    const renderContent = () => {
      content.replaceChildren(
        rawMode ? element("pre", "evidence-text", raw) : readableEvidence(raw),
      );
    };
    const toggle = button("Show raw", () => {
      rawMode = !rawMode;
      toggle.textContent = rawMode ? "Show formatted" : "Show raw";
      toggle.setAttribute("aria-pressed", String(rawMode));
      renderContent();
    });
    toggle.setAttribute("aria-pressed", "false");
    const signal = this.abort.signal;
    const load = async () => {
      more.disabled = true;
      try {
        const page = await this.api.trajectoryEvidence(
          this.sessionId,
          this.job.id,
          ref,
          offset,
          signal,
          ref.end == null
            ? 8000
            : Math.max(1, Math.min(8000, ref.end - offset)),
        );
        if (signal.aborted || this.dialog !== dialog) return;
        title.textContent = `${label} · Event ${page.seq} · ${page.event_type}`;
        raw += page.content;
        renderContent();
        offset = page.returned_ref.end;
        const hasMore = page.truncated && (ref.end == null || offset < ref.end);
        status.textContent = `Characters ${ref.start || 0}–${number(offset)}${hasMore ? " · More evidence available" : ref.end == null ? " · Complete" : " · Cited range"}`;
        more.hidden = !hasMore;
      } catch (error) {
        if (!signal.aborted && this.dialog === dialog) {
          status.textContent = error.message;
          this.onError(error);
        }
      } finally {
        more.disabled = false;
      }
    };
    const more = button("Load more", load);
    dialog.append(
      button("Close", () => dialog.close()),
      title,
      status,
      toggle,
      content,
      more,
    );
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
    await load();
  }
}
