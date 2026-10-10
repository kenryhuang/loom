import { element } from "../markdown.mjs";

const number = value => value == null ? "—" : Number(value).toLocaleString(undefined, {maximumFractionDigits: 1});
const seconds = value => value == null ? "—" : `${number(value / 1000)} s`;
const percent = value => value == null ? "—" : `${number(value * 100)}%`;
const label = value => String(value || "unknown").replaceAll("_", " ");
function section(root, title) {
  const node = element("section", "trajectory-section");
  node.append(element("h3", "", title));
  root.append(node);
  return node;
}
function cards(root, values) {
  const grid = element("div", "trajectory-metrics");
  for (const [title, value] of values) {
    const card = element("div", "trajectory-metric");
    card.append(element("small", "muted", title), element("strong", "", value));
    grid.append(card);
  }
  root.append(grid);
}
function table(root, headers, rows) {
  const container = element("div", "base-table-scroll"), node = element("table", "base-metrics-table");
  const head = element("thead"), tr = element("tr"), body = element("tbody");
  for (const name of headers) tr.append(element("th", "", name));
  head.append(tr);
  for (const values of rows) {
    const row = element("tr");
    for (const value of values) {
      const cell = element("td");
      if (value?.nodeType) cell.append(value);
      else cell.textContent = value == null ? "—" : String(value);
      row.append(cell);
    }
    body.append(row);
  }
  node.append(head, body); container.append(node); root.append(container);
}
function details(root, title) {
  const node = element("details"); node.append(element("summary", "", title)); root.append(node); return node;
}
function callTable(root, rows, dimension) {
  table(root, [dimension, "Calls", "Success", "Failed", "Cancelled", "Incomplete", "Unknown", "Success rate", "Cumulative time", "Avg / P95", "Max", "Timed calls"],
    rows.map(r => [label(r.name), number(r.calls), number(r.success), number(r.failed), number(r.cancelled), number(r.incomplete), number(r.unknown),
      percent(r.success_rate), seconds(r.duration.total_ms), `${seconds(r.duration.average_ms)} / ${seconds(r.duration.p95_ms)}`,
      seconds(r.duration.max_ms), `${r.duration.measured}/${r.calls}`]));
}

export function renderBaseStatistics(root, base, actions = {}) {
  if (!base.statistics) return false;
  const scope = element("select"), content = element("div");
  scope.setAttribute("aria-label", "Base evaluation scope");
  const all = element("option", "", "All recorded executions"); all.value = ""; scope.append(all);
  for (const id of Object.keys(base.statistics.by_run || {})) {
    const option = element("option", "", `Run · ${id}`); option.value = id; scope.append(option);
  }
  const render = () => {
    content.replaceChildren();
    const statistics = scope.value ? base.statistics.by_run[scope.value] : base.statistics;
    renderStatistics(content, {...base, statistics, execution_status: statistics.execution_status || base.execution_status}, actions);
  };
  scope.addEventListener("change", render); root.append(scope, content); render();
  return true;
}

function renderStatistics(root, base, {evidence, artifact} = {}) {
  const stats = base.statistics;
  if (!stats) return false;
  const {basic, tools, models, timing, quality, outputs} = stats;
  const info = section(root, "1. Basic information");
  info.append(element("p", "muted", `Scope: ${stats.scope === "run" ? "selected run" : "all executions"} in this recorded source snapshot. Use Deep evaluation to assess intent, planning and progress.`));
  cards(info, [["Execution", label(base.execution_status)], ["Raw events", number(basic.events.raw)], ["Analyzed events", number(basic.events.analyzed)],
    ["Runs / start attempts", `${basic.runs} / ${basic.run_attempts}`], ["Logical steps / start attempts", `${basic.steps} / ${basic.step_attempts}`],
    ["Model calls", number(models.calls)], ["Tool calls", number(tools.calls)],
    ["Known input tokens", number(models.prompt_tokens.known)], ["Known output tokens", number(models.completion_tokens.known)],
    ["Known total tokens", number(models.total_tokens.known)]]);
  const state = basic.acceptance?.state;
  if (state) {
    const criteria = state.plan?.criteria || [], results = new Map((state.results || []).map(r => [r.criterion_id, r]));
    const passed = criteria.filter(c => results.get(c.id)?.status === "passed").length;
    info.append(element("h4", "", `Acceptance: ${label(state.state)} · ${passed}/${criteria.length} checks · ${state.attempts || 0} rounds`),
      element("p", "", state.reason));
    const checks = details(info, "Acceptance checks and evidence");
    for (const c of criteria) {
      const r = results.get(c.id), row = element("details");
      row.open = ["failed", "blocked"].includes(r?.status);
      row.append(element("summary", "", `${label(r?.status || "pending")} · ${c.description}`),
        element("p", "", r?.reason || "No result recorded"), element("small", "muted", `Method: ${label(c.verifier)} · ${label(r?.assurance || "recorded check")}`));
      if (artifact && r?.artifact?.sha256) {
        const button = element("button", "quiet", "View evidence"); button.addEventListener("click", () => artifact(r.artifact.sha256)); row.append(button);
      }
      checks.append(row);
    }
    for (const text of state.plan?.unresolved_requirements || []) checks.append(element("p", "notice", text));
  } else info.append(element("p", "muted", "Acceptance: no checks recorded for the latest execution."));
  table(details(info, "Event and step counts"), ["Type", "Count"], [
    ...Object.entries(basic.events.by_type), ...Object.entries(basic.step_states).map(([k, v]) => [`Steps ${k}`, v]), ["Goal changes", basic.goal_changes]]);

  const toolSection = section(root, "2. Tool calls");
  toolSection.append(element("p", "muted", quality.success_rate_basis));
  const grouping = element("select"), toolTable = element("div");
  grouping.setAttribute("aria-label", "Group tool statistics");
  for (const [value, title] of [["by_tool", "By tool"], ["by_family", "By category"], ["by_role", "By purpose"]]) {
    const option = element("option", "", title); option.value = value; grouping.append(option);
  }
  const update = () => {toolTable.replaceChildren(); callTable(toolTable, tools[grouping.value], grouping.selectedOptions[0].textContent);};
  grouping.addEventListener("change", update); toolSection.append(grouping, toolTable); update();
  toolSection.append(element("p", "", `Recorded output truncations: ${tools.truncated_outputs} · Explicit retries: ${tools.explicit_retries}`));
  toolSection.append(element("p", "muted", `Repeated calls with identical inputs: ${tools.repeated_inputs}. This does not establish wasted work or a retry.`));
  table(details(toolSection, "Failure reasons"), ["Reason", "Count"], Object.entries(tools.failure_reasons).map(([k, v]) => [label(k), v]));
  const calls = details(toolSection, "Call details · slowest first");
  const ordered = [...stats.calls.tools].sort((a, b) => (b.duration_ms ?? -1) - (a.duration_ms ?? -1));
  table(calls, ["Tool", "Result", "Reason", "Time", "Purpose", "Evidence"], ordered.map(c => {
    let link = `Event ${c.seq ?? "—"}`;
    if (evidence && c.evidence_refs.length) {link = element("button", "quiet", link); link.addEventListener("click", () => evidence(c.evidence_refs.at(-1)));}
    return [c.tool, label(c.status), label(c.failure_reason || "—"), seconds(c.duration_ms), label(c.role), link];
  }));

  const time = section(root, "3. Runtime cost");
  cards(time, [["Elapsed across runs (sum)", seconds(timing.wall_ms)], ["Session time span", seconds(timing.session_span_ms)],
    ["Recorded running", seconds(timing.states_ms.running)], ["Paused", seconds(timing.states_ms.paused)],
    ["Waiting for input", seconds(timing.states_ms.waiting_input)], ["Queued", seconds(timing.states_ms.queued)]]);
  table(time, ["Lifecycle", "Elapsed time"], Object.entries(timing.states_ms).map(([k, v]) => [label(k), seconds(v)]));
  const activity = element("div", "base-time-distribution");
  const total = timing.states_ms.running;
  for (const [kind, ms] of Object.entries(timing.running_activity_ms)) {
    const line = element("div");
    line.append(element("span", "", `${label(kind)} · ${seconds(ms)}`));
    const progress = element("progress"); progress.max = total || 1; progress.value = ms; progress.setAttribute("aria-label", label(kind));
    line.append(progress); activity.append(line);
  }
  time.append(element("h4", "", "Activity within recorded running time"), activity);
  for (const text of timing.limitations) time.append(element("p", "muted small", text));
  table(details(time, "Lifecycle timeline"), ["Run", "State", "From", "To", "Time"], timing.intervals.map(i =>
    [i.run_id, label(i.state), new Date(i.start_ms).toLocaleString(), new Date(i.end_ms).toLocaleString(), seconds(i.end_ms - i.start_ms)]));

  const model = section(root, "4. Model calls and token cost");
  callTable(model, models.by_model, "Model");
  for (const [title, rows] of [["By model", models.by_model], ["By purpose", models.by_role], ["By recorded stage", models.by_stage]])
    table(model, [title, "Calls", "Known input", "Known output", "Known total", "Usage coverage", "Cumulative time"], rows.map(r =>
      [label(r.name), r.calls, number(r.prompt_tokens.known), number(r.completion_tokens.known), number(r.total_tokens.known),
        `${r.total_tokens.measured_calls}/${r.calls}`, seconds(r.duration.total_ms)]));
  table(details(model, "Per-call token distribution"), ["Tokens", "Known total", "Average measured call", "Maximum", "Missing calls"],
    [["Input", models.prompt_tokens], ["Output", models.completion_tokens], ["Total", models.total_tokens]].map(([name, r]) =>
      [name, number(r.known), number(r.average), number(r.max), r.missing_calls]));
  model.append(element("p", "muted", quality.token_basis));

  const output = section(root, "5. Outputs and data completeness");
  for (const path of outputs.workspace_files) output.append(element("p", "", path));
  const artifacts = details(output, `Recorded artifacts · ${outputs.artifacts.length}`);
  for (const ref of outputs.artifacts) {
    const node = element(artifact ? "button" : "p", "quiet", `${ref.kind} · ${ref.relative_path || ref.local_path || ""}`);
    if (artifact) node.addEventListener("click", () => artifact(ref.sha256));
    artifacts.append(node);
  }
  table(output, ["Coverage", "Count"], [["Calls without total usage", quality.missing_usage_calls], ["Calls without duration", quality.missing_call_durations],
    ["Calls without start records", quality.calls_without_start], ["Calls without terminal records", quality.calls_without_terminal],
    ["Missing event artifacts", quality.missing_artifacts.length], ["Unscoped call events", quality.unscoped_call_events],
    ["Repeated source records", quality.duplicate_records], ["Events without a valid timestamp", timing.missing_timestamp_events], ["Clock reversals", timing.clock_reversals]]);
  table(details(output, "Interruptions and usage coverage"), ["Metric", "Count"], [
    ...Object.entries(basic.interruptions || {}).map(([k, v]) => [label(k), v]),
    ...Object.entries(quality.usage_statuses).map(([k, v]) => [`Usage ${label(k)}`, v])]);
  output.append(element("p", "muted", quality.retry_basis), element("p", "muted", `Unavailable measurements: ${quality.unavailable.join("; ")}`));
  return true;
}
