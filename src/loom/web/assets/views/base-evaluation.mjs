import { element } from "../markdown.mjs";
import { number, seconds, label, badge, panel, cards, table, disclosure, distribution } from "./dashboard.mjs";

const percent = value => value == null ? "—" : `${number(value * 100)}%`;
function grouping(header, ariaLabel, choices, update) {
  const select = element("select"); select.setAttribute("aria-label", ariaLabel);
  for (const [value, title] of choices) { const option = element("option", "", title); option.value = value; select.append(option); }
  select.addEventListener("change", () => update(select.value)); header.append(select); update(select.value);
}

export function renderBaseStatistics(root, base, actions = {}) {
  if (!base.statistics) return false;
  const scope = actions.scope || element("select"), content = element("div", "trace-dashboard");
  if (!actions.scope) {
    scope.setAttribute("aria-label", "Base evaluation scope");
    const all = element("option", "", "All runs"); all.value = ""; scope.append(all);
    for (const [index, id] of Object.keys(base.statistics.by_run || {}).entries()) {
      const option = element("option", "", `Run ${index + 1} · ${id}`); option.value = id; scope.append(option);
    }
    const toolbar = element("div", "dashboard-toolbar"); toolbar.append(scope); root.append(toolbar);
  }
  const render = () => {
    content.replaceChildren();
    const statistics = scope.value ? base.statistics.by_run[scope.value] : base.statistics;
    if (!statistics) { content.append(element("p", "empty-state", "No statistics for this run")); return; }
    renderStatistics(content, {...base, statistics, execution_status: statistics.execution_status || base.execution_status}, actions);
  };
  scope.addEventListener("change", render); root.append(content); render(); return true;
}

function renderStatistics(root, base, {artifact} = {}) {
  const {basic, tools, models, timing, quality, outputs} = base.statistics;
  const info = panel(root, "Basic info", true);
  info.firstChild.append(badge(label(base.execution_status), base.execution_status), badge(`${number(basic.runs)} runs`));
  cards(info, [["Raw events", number(basic.events.raw)], ["Analyzed events", number(basic.events.analyzed)],
    ["Steps", number(basic.steps)], ["Model calls", number(models.calls)], ["Tool calls", number(tools.calls)],
    ["Input tokens", number(models.prompt_tokens.known)], ["Output tokens", number(models.completion_tokens.known)]]);
  table(disclosure(info, "Event breakdown"), ["Type", "Count"], [
    ...Object.entries(basic.events.by_type), ...Object.entries(basic.step_states).map(([k, v]) => [`Steps · ${label(k)}`, v]),
    ["Run attempts", basic.run_attempts], ["Step attempts", basic.step_attempts], ["Goal changes", basic.goal_changes]]);

  const acceptance = panel(root, "Acceptance", true), state = basic.acceptance?.state;
  acceptance.classList.add("dashboard-acceptance");
  acceptance.firstChild.append(badge(label(state?.state || "not_recorded"), state?.state));
  if (state) {
    const criteria = state.plan?.criteria || [], results = new Map((state.results || []).map(r => [r.criterion_id, r]));
    const passed = criteria.filter(c => results.get(c.id)?.status === "passed").length;
    const summary = element("div", "acceptance-overview");
    const ring = element("div", "acceptance-ring", criteria.length ? percent(passed / criteria.length) : "—");
    ring.style.setProperty("--accepted", `${criteria.length ? passed / criteria.length * 100 : 0}%`);
    ring.setAttribute("role", "img"); ring.setAttribute("aria-label", `${passed} of ${criteria.length} acceptance checks passed`);
    summary.append(ring); cards(summary, [["Checks passed", `${passed} / ${criteria.length}`], ["Verification rounds", number(state.attempts || 0)]]);
    acceptance.append(summary);
    table(acceptance, ["Condition", "Status", "Verifier", "Evidence"], criteria.map(c => {
      const r = results.get(c.id), proof = element("div");
      if (artifact && r?.artifact?.sha256) {
        const button = element("button", "quiet", "View evidence"); button.addEventListener("click", () => artifact(r.artifact.sha256)); proof.append(button);
      }
      const condition = element("div", "acceptance-condition", c.description);
      if (r?.reason && r.status !== "passed") condition.append(element("p", "muted small", r.reason));
      return [condition, badge(label(r?.status || "pending"), r?.status), label(c.verifier), proof];
    }));
    for (const text of state.plan?.unresolved_requirements || []) acceptance.append(element("p", "notice", text));
    if (["failed", "blocked"].includes(state.state) && state.reason) acceptance.append(element("p", "notice", state.reason));
  } else acceptance.append(element("p", "empty-state", "No checks recorded"));

  const toolSection = panel(root, "Tool calls", true), toolTable = element("div"); toolSection.append(toolTable);
  grouping(toolSection.firstChild, "Group tool statistics", [["by_tool", "By tool"], ["by_family", "By category"], ["by_role", "By purpose"]], key => {
    toolTable.replaceChildren();
    table(toolTable, ["Tool / group", "Calls", "Success", "Failed", "Cancelled", "Incomplete", "Unknown", "Success rate", "Total time", "Avg / P95", "Max", "Timed"],
      tools[key].map(r => [label(r.name), number(r.calls), number(r.success), r.failed ? badge(number(r.failed), "failed") : "0",
        number(r.cancelled), number(r.incomplete), number(r.unknown), percent(r.success_rate), seconds(r.duration.total_ms),
        `${seconds(r.duration.average_ms)} / ${seconds(r.duration.p95_ms)}`, seconds(r.duration.max_ms), `${r.duration.measured}/${r.calls}`]));
  });

  const time = panel(root, "Runtime cost", true);
  cards(time, [["Run time · sum", seconds(timing.wall_ms)], ["Session span", seconds(timing.session_span_ms)]]);
  const runtimeCharts = element("div", "runtime-charts"); time.append(runtimeCharts);
  distribution(runtimeCharts, "Lifecycle", Object.entries(timing.states_ms).map(([k, v]) => [label(k), v]), seconds);
  distribution(runtimeCharts, "Running activity", Object.entries(timing.running_activity_ms).map(([k, v]) => [label(k), v]), seconds);
  table(disclosure(time, "Lifecycle timeline"), ["Run", "State", "From", "To", "Time"], timing.intervals.map(i =>
    [i.run_id, label(i.state), new Date(i.start_ms).toLocaleString(), new Date(i.end_ms).toLocaleString(), seconds(i.end_ms - i.start_ms)]));
  if (timing.limitations?.length) {
    const notes = disclosure(time, "Measurement notes"); for (const text of timing.limitations) notes.append(element("p", "muted small", text));
  }

  const model = panel(root, "Model & token cost", true);
  const measuredCell = (value, count, calls, definition) => {
    const cell = element("span", "", value);
    cell.title = `${definition} · ${count || 0}/${calls} calls measured`;
    return cell;
  };
  table(model, ["Model", "Calls", "TTFT · avg / P95", "TPS · avg / P95", "Duration · avg / P95"], models.by_model.map(r =>
    [r.name, number(r.calls),
      measuredCell(`${seconds(r.ttft?.average_ms)} / ${seconds(r.ttft?.p95_ms)}`, r.ttft?.measured, r.calls, "Request → first output (including reasoning and tool calls)"),
      measuredCell(`${number(r.tps?.average)} / ${number(r.tps?.p95)}`, r.tps?.measured, r.calls, "Output tokens / seconds from first output to completion; arithmetic mean of measured calls"),
      measuredCell(`${seconds(r.duration.average_ms)} / ${seconds(r.duration.p95_ms)}`, r.duration.measured, r.calls, "Request → response completion")]));
  table(model, ["Model", "Token in · total / max", "Token out · total / max"], models.by_model.map(r =>
    [r.name, ...[r.prompt_tokens, r.completion_tokens].map(t => measuredCell(
      `${number(t.measured_calls ? t.known : null)} / ${number(t.max)}`, t.measured_calls, r.calls, "Provider-reported tokens; total includes measured calls only"))]));

  const output = panel(root, "Outputs");
  output.firstChild.append(badge(`${outputs.workspace_files.length} files`), badge(`${outputs.artifacts.length} artifacts`));
  const files = element("ul", "dashboard-file-list");
  for (const path of outputs.workspace_files) files.append(element("li", "", path)); output.append(files);
  if (!outputs.workspace_files.length && !outputs.artifacts.length) output.append(element("p", "empty-state", "No outputs recorded"));
  if (outputs.artifacts.length) {
    const artifacts = disclosure(output, "Artifacts");
    for (const ref of outputs.artifacts) {
      const node = element(artifact ? "button" : "p", "quiet dashboard-artifact", `${label(ref.kind)} · ${ref.relative_path || ref.local_path || ""}`);
      if (artifact) node.addEventListener("click", () => artifact(ref.sha256)); artifacts.append(node);
    }
  }
  const completeness = panel(root, "Data quality");
  const rows = [["Missing token usage", quality.missing_usage_calls], ["Missing call duration", quality.missing_call_durations],
    ["Missing TTFT", quality.missing_ttft_calls || 0], ["Missing TPS", quality.missing_tps_calls || 0],
    ["Missing call starts", quality.calls_without_start], ["Missing call endings", quality.calls_without_terminal],
    ["Missing artifacts", quality.missing_artifacts.length], ["Unscoped events", quality.unscoped_call_events],
    ["Duplicate records", quality.duplicate_records], ["Missing timestamps", timing.missing_timestamp_events], ["Clock reversals", timing.clock_reversals]];
  const issues = rows.filter(([, count]) => count > 0);
  completeness.firstChild.append(badge(`${issues.length} gaps`, issues.length ? "" : "passed"));
  if (issues.length) table(completeness, ["Missing / incomplete", "Count"], issues);
  else completeness.append(element("p", "empty-state", "No recorded gaps"));
  table(disclosure(completeness, "All coverage metrics"), ["Metric", "Count"], [...rows,
    ...Object.entries(basic.interruptions || {}).map(([k, v]) => [label(k), v]),
    ...Object.entries(quality.usage_statuses).map(([k, v]) => [`Usage · ${label(k)}`, v])]);
  if (quality.unavailable?.length) table(disclosure(completeness, "Unavailable measurements"), ["Metric"], quality.unavailable.map(v => [v]));
  return true;
}
