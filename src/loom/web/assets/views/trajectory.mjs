import { element } from "../markdown.mjs";

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
      } catch { /* Plain text or an incomplete evidence page. */ }
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
          field.append(element("dt", "", Array.isArray(value) ? `[${key}]` : key), body);
          root.append(field);
        }
        return root;
      }
    }
    return element("pre", "evidence-text", typeof value === "string" ? value : JSON.stringify(value, null, 2));
  };
  return render(text);
}

export class TrajectoryView {
  constructor(root, { onError = () => {} }) {
    this.root = root;
    this.onError = onError;
  }
  close() {
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
    this.snapshotLabel();
  }
  snapshotLabel() {
    if (!this.job || !this.status) return;
    const stale = this.cursor > this.job.source_cursor;
    this.status.textContent =
      this.job.state === "completed"
        ? `Snapshot through event ${this.job.source_cursor}${stale ? " · New events available — refresh analysis to include them." : " · Up to date."}`
        : `Analyzing snapshot through event ${this.job.source_cursor}…`;
  }
  async open(api, sessionId, cursor = 0) {
    this.close();
    this.api = api;
    this.sessionId = sessionId;
    this.cursor = cursor;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    this.root.hidden = false;
    const heading = element("div", "trajectory-heading");
    const title = element("div");
    title.append(
      element("h2", "", "Trace analysis"),
      element("p", "muted small", "Recorded calls, context and execution evidence."),
    );
    this.refresh = button("Refresh analysis", () =>
      this.open(api, sessionId, this.cursor),
    );
    this.refresh.disabled = true;
    heading.append(title, this.refresh);
    this.status = element("p", "muted small", "Preparing analysis…");
    this.status.setAttribute("role", "status");
    this.body = element("div", "trajectory-body");
    this.root.append(heading, this.status, this.body);
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
      this.onError(error);
    } finally {
      if (!signal.aborted) this.refresh.disabled = false;
    }
  }
  render() {
    const data = this.analysis;
    this.body.replaceChildren();
    const cards = element("div", "trajectory-metrics");
    for (const [label, value] of [
      ["Model calls", data.rounds.length],
      ["Tool calls", data.tools.length],
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
    this.body.append(
      cards,
      element(
        "p",
        "muted small",
        "Recorded facts · Semantic evaluation has not run. Task completion is not independently verified.",
      ),
    );
    const missing = data.rounds.filter(
      (row) => row.usage.total_tokens == null,
    ).length;
    if (missing)
      this.body.append(
        element(
          "p",
          "notice",
          `${missing} model calls have no measured total usage. Token totals include only reported values.`,
        ),
      );
    if (
      data.coverage.missing_artifacts.length ||
      data.coverage.unscoped_model_events
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
    this.body.append(tasks);
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
    this.runFilter = element("select");
    this.runFilter.setAttribute("aria-label", "Filter trajectory by run");
    this.runFilter.append(option("All runs", ""));
    for (const [index, id] of data.run_ids.entries())
      this.runFilter.append(option(`Run ${index + 1} · ${id}`, id));
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
    filters.append(this.runFilter, this.statusFilter, this.search);
    this.timeline = element("div", "trajectory-timeline");
    this.body.append(filters, this.timeline);
    this.renderRounds();
    const unlinked = data.tools.filter((tool) => !tool.round_id);
    if (unlinked.length) {
      const section = element("details", "trajectory-section");
      section.append(
        element(
          "summary",
          "",
          `Tools without an established model-call link · ${unlinked.length}`,
        ),
      );
      for (const tool of unlinked) section.append(this.tool(tool));
      this.body.append(section);
    }
  }
  renderRounds() {
    this.timeline.replaceChildren();
    const data = this.analysis,
      query = this.search.value.trim().toLowerCase();
    const maxTokens = Math.max(
      1,
      ...data.rounds.map((row) => row.usage.total_tokens || 0),
    );
    let shown = 0;
    for (const [index, round] of data.rounds.entries()) {
      const tools = data.tools.filter((tool) => tool.round_id === round.id);
      if (this.runFilter.value && round.run_id !== this.runFilter.value)
        continue;
      if (
        this.statusFilter.value === "failed" &&
        round.status !== "failed" &&
        !tools.some((tool) => tool.status === "failed")
      )
        continue;
      if (this.statusFilter.value === "partial" && round.status !== "partial")
        continue;
      if (
        query &&
        !`${round.model} ${round.summary} ${round.llm_call_id} ${tools.map((tool) => tool.tool_id).join(" ")}`
          .toLowerCase()
          .includes(query)
      )
        continue;
      shown++;
      const node = element("details", "trajectory-round");
      node.dataset.roundId = round.id;
      const summary = element("summary"),
        title = element("div", "trajectory-call-title");
      title.append(
        element("strong", "", `${index + 1}. ${round.model || "Model call"}`),
        element(
          "span",
          "muted small",
          preview(round.summary) || "No response content recorded",
        ),
      );
      const stats = element("div", "trajectory-call-stats");
      stats.append(
        element(
          "span",
          "small",
          `${number(round.usage.prompt_tokens)} in / ${number(round.usage.completion_tokens)} out`,
        ),
      );
      const meter = element("meter");
      meter.max = maxTokens;
      meter.value = round.usage.total_tokens || 0;
      meter.setAttribute("aria-label", `Call ${index + 1} recorded tokens`);
      stats.append(meter);
      const status = element(
        "span",
        `status ${round.status === "failed" ? "failed" : ""}`,
        round.status,
      );
      summary.append(title, stats, status);
      const body = element("div", "trajectory-round-detail");
      body.append(
        element(
          "p",
          "muted small",
          `Event ${round.seq} · ${round.at || "Time unknown"} · ${round.duration_ms == null ? "Duration unknown" : `${number(round.duration_ms)} ms`} · ${round.run_id}`,
        ),
      );
      const evidence = element("div", "trajectory-actions");
      if (round.request_ref)
        evidence.append(this.evidenceButton("Request", round.request_ref));
      if (round.response_ref)
        evidence.append(this.evidenceButton("Response", round.response_ref));
      for (const ref of round.evidence_refs)
        if (
          !round.request_ref ||
          ref.line_number !== round.request_ref.line_number
        )
          if (
            !round.response_ref ||
            ref.line_number !== round.response_ref.line_number
          )
            evidence.append(this.evidenceButton("Failure evidence", ref));
      body.append(
        evidence,
        element(
          "p",
          "small",
          `Context: ${number(round.context.message_count)} messages · ${round.context_changes.added} added · ${round.context_changes.retained} retained · ${round.context_changes.removed} removed`,
        ),
      );
      for (const tool of tools) body.append(this.tool(tool));
      const extra = element("div");
      body.append(extra);
      let loaded = false;
      node.addEventListener("toggle", async () => {
        if (!node.open || loaded) return;
        loaded = true;
        const signal = this.abort.signal;
        extra.textContent = "Loading context and verification evidence…";
        try {
          const detail = await this.api.trajectoryRound(
            this.sessionId,
            this.job.id,
            round.id,
            signal,
          );
          if (signal.aborted) return;
          extra.replaceChildren();
          for (const [label, rows] of [
            ["Context changes", detail.context_deltas],
            ["Recorded verification evidence", detail.verification_evidence],
          ]) {
            const disclosure = element("details", "trajectory-section");
            disclosure.append(
              element("summary", "", label),
              element("pre", "", JSON.stringify(rows, null, 2)),
            );
            extra.append(disclosure);
          }
        } catch (error) {
          if (signal.aborted) return;
          extra.textContent = `Could not load call details: ${error.message}. Close and reopen to retry.`;
          loaded = false;
          this.onError(error);
        }
      });
      node.append(summary, body);
      this.timeline.append(node);
    }
    if (!shown)
      this.timeline.append(
        element(
          "p",
          "empty-state",
          data.rounds.length
            ? "No calls match these filters."
            : "No model calls have been recorded in this session yet.",
        ),
      );
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
        element("p", "muted small", `Event ${tool.seq} · ${tool.at || "Time unknown"}`),
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
  async showEvidence(ref, label = "Recorded evidence") {
    this.dialog?.remove();
    const dialog = element("dialog", "trajectory-evidence");
    this.dialog = dialog;
    const title = element("h2", "", label),
      status = element("p", "muted small"),
      content = element("div", "evidence-content");
    let offset = 0,
      raw = "",
      rawMode = false;
    const renderContent = () => {
      content.replaceChildren(rawMode ? element("pre", "evidence-text", raw) : readableEvidence(raw));
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
        );
        if (signal.aborted || this.dialog !== dialog) return;
        title.textContent = `${label} · Event ${page.seq} · ${page.event_type}`;
        raw += page.content;
        renderContent();
        offset = page.returned_ref.end;
        status.textContent = `${number(offset)} characters shown${page.truncated ? " · More evidence available" : " · Complete"}`;
        more.hidden = !page.truncated;
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
