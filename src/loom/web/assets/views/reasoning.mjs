import {element} from "../markdown.mjs";
import {badge, cards, disclosure, distribution, label, number, panel, seconds, table} from "./dashboard.mjs";

const button = (text, action) => {
  const b = element("button", "quiet", text); b.type = "button"; b.addEventListener("click", action); return b;
};
const svg = (name, attrs = {}) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
};
function choice(root, title, values, selected, change) {
  const wrapper = element("label", "", title), select = element("select"); select.setAttribute("aria-label", title);
  for (const [value, text] of values) { const option = element("option", "", text); option.value = value; select.append(option); }
  if (values.some(([value]) => value === selected)) select.value = selected;
  select.addEventListener("change", () => change(select.value)); wrapper.append(select); root.append(wrapper); return select.value;
}

// Each phase remains distinct: collection and observation are not the same node.
export function processGroups(nodes) {
  const rows = nodes.map(n => ({...n, members: [n], refs: n.evidence_refs || [], callIds: new Set(n.tool_call_id ? [n.tool_call_id] : [])}));
  return {rows, owners: new Map(rows.map(n => [n.id, n.id]))};
}

const relationLabel = r => ({emits: "chooses action", selects: "chooses action", returns: "returns result",
  next_round: "next reasoning round", continues_after_failure: "continues after rejection / failure", continues: "proceeds to",
  ends: "ends with", omitted_phases: "via hidden phases"}[r.kind] || label(r.kind));

// A bounded rendering still includes the outcome and an explicit node for omitted phases.
export function boundedGraph(nodes, relations, limit) {
  if (nodes.length <= limit) return {nodes, relations};
  const end = nodes.findLast(n => n.is_endpoint) || nodes.at(-1);
  const prefix = nodes.slice(0, Math.max(1, limit - 2)).filter(n => n.id !== end.id);
  const retained = new Set([...prefix.map(n => n.id), end.id]), hidden = nodes.filter(n => !retained.has(n.id));
  const gap = {...hidden[0], id: `${end.id}:hidden`, kind: "omitted", phase: "continuation", role: "process", column: 1,
    label: `${hidden.length} phases collapsed`, status: "expand_to_inspect", evidence_refs: [], is_endpoint: false, terminal: false,
    round_id: null, content: "Expand the graph to inspect these recorded phases and their relationships."};
  const hiddenIds = new Set(hidden.map(n => n.id)), edges = new Map();
  for (const r of relations) {
    const source = hiddenIds.has(r.source) ? gap.id : r.source, target = hiddenIds.has(r.target) ? gap.id : r.target;
    if (source === target) continue;
    const collapsed = source !== r.source || target !== r.target;
    const edge = {...r, source, target, ...(collapsed ? {kind: "omitted_phases", layer: "flow", basis: "deterministic"} : {})};
    edges.set(`${source}:${target}:${edge.kind}`, edge);
  }
  return {nodes: [...prefix, gap, end], relations: [...edges.values()]};
}

function missingReason(p, job) {
  if (p.validation_diagnostics?.length && !p.occurrences?.length) return "All proposed reasoning links were rejected. Review diagnostics and run a new evaluation.";
  if (!p.coverage?.reviewed_segments) return "Awaiting Deep evaluation: recorded actions are available; evidence use and conclusions have not been assessed.";
  if (job?.state === "budget_exhausted" || p.coverage?.status === "incomplete") return "Partial review: saved links are shown; unreviewed rounds may contain further evidence and conclusions.";
  return "No supported endpoint identified in the reviewed evidence.";
}

export class ReasoningView {
  constructor(root, {baseProcess, evidence, onEvaluate, onRefresh}) {
    Object.assign(this, {root, baseProcess, evidence, onEvaluate, onRefresh});
    this.limit = 200; this.role = "all"; this.update(null);
  }
  update(data, job) {
    const signature = JSON.stringify([data, this.baseProcess, job?.state, job?.stage]);
    if (signature === this.signature) return;
    this.signature = signature;
    this.process = data?.semantic?.reasoning_process?.graph ? data.semantic.reasoning_process : this.baseProcess;
    this.job = job;
    this.legacy = !!data && !data.semantic?.reasoning_process?.graph;
    this.render();
  }
  refs(root, refs = []) {
    const unique = [...new Map(refs.map(r => [JSON.stringify(r), r])).values()];
    const actions = element("div", "trajectory-actions");
    for (const ref of unique) actions.append(button(`Evidence ${ref.line_number}${ref.field_path ? ` · ${ref.field_path}` : ""}`, () => this.evidence(ref)));
    root.append(actions);
  }
  render() {
    const p = this.process;
    this.root.replaceChildren();
    const toolbar = element("div", "dashboard-toolbar reasoning-toolbar"); this.root.append(toolbar);
    if (!p?.scopes?.length) {
      toolbar.append(button("Refresh analysis", this.onRefresh));
      this.root.append(element("p", "empty-state", "No reasoning activity in this snapshot. Refresh analysis to build the activity ledger.")); return;
    }
    this.scope = choice(toolbar, "Goal / run", p.scopes.map(s => [s.id, `${s.label} · ${s.run_id || "Unscoped"}`]),
      this.scope || p.scopes.at(-1).id, v => {this.scope = v; this.question = ""; this.render();});
    const scope = p.scopes.find(s => s.id === this.scope);
    const questions = p.questions.filter(q => q.episode_id === scope.episode_id && q.goal_revision_id === scope.goal_revision_id);
    this.question = choice(toolbar, "Question", [["", "All questions"], ...questions.map(q => [q.id, q.label])], this.question,
      v => {this.question = v; this.render();});
    this.role = choice(toolbar, "Highlight role", [["all", "All roles"], ["solver", "Solver"], ["verification", "Verification"]], this.role,
      v => {this.role = v; this.render();});
    toolbar.append(button("Review in Deep evaluation", this.onEvaluate), button("Refresh analysis", this.onRefresh), button("Export graph", () => {
      const blob = new Blob([JSON.stringify(p, null, 2)], {type: "application/json"}), url = URL.createObjectURL(blob);
      const a = element("a"); a.href = url; a.download = "reasoning-process.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    const root = element("div", "trace-dashboard reasoning-dashboard"); this.root.append(root);
    const overview = panel(root, "Investigation overview", true), coverage = p.coverage || {};
    overview.firstChild.append(badge(`${coverage.reviewed_segments || 0} / ${coverage.source_segments || 0} segments reviewed`),
      badge(this.legacy ? "Historical evaluation · process not assessed" : label(coverage.status)));
    const evidence = p.metrics[scope.id]?.evidence || {}, judgment = p.metrics[scope.id]?.judgment || {};
    const selected = this.question ? questions.filter(q => q.id === this.question) : questions;
    const endpoint = selected.length === 1 ? selected[0].endpoint : null;
    const lags = selected.flatMap(q => q.adoption_lags || []);
    const costs = scope.costs_by_role || {}, solver = costs.solver || {};
    const occurrences = (p.occurrences || []).filter(r => selected.some(q => q.id === r.question_id));
    const observations = new Set(occurrences.flatMap(r => r.observation_ids || []));
    cards(overview, [["Solver calls", number(solver.model_calls)], ["Tool calls", number(solver.tool_calls)],
      ["Linked observations", number(observations.size)], ["Reviewed judgments", number(occurrences.length)]]);
    if (this.job?.state) overview.firstChild.append(badge(`Evaluation · ${label(this.job.state)}`));
    if (!occurrences.length || ["incomplete", "not_evaluated"].includes(coverage.status))
      overview.append(element("p", "reasoning-notice", missingReason(p, this.job)));
    const unavailable = !occurrences.length ? "No accepted reasoning links" : "No supported conclusion identified";
    table(overview, ["Efficiency measure", "Value", "Coverage"], [
      ["Known reads / evidence", evidence.n ? `${evidence.partial_windows ? "≥ " : ""}${number(evidence.mean)}` : "Not measured",
        evidence.n ? `${evidence.n} closed windows${evidence.partial_windows ? " · partial read coverage" : ""}` : "No reviewed evidence window closed"],
      [endpoint?.kind === "cause" ? "Solver calls → supported cause" : "Solver calls → supported conclusion",
        endpoint?.cost ? number(endpoint.cost.model_calls) : "Not measured", endpoint?.cost ? "Cited endpoint" : selected.length > 1 ? "Select one question" : unavailable],
      ["Adoption lag · solver calls", lags.length ? number(lags.reduce((n, r) => n + r.cost.model_calls, 0) / lags.length) : "Not measured",
        lags.length ? `${lags.length} recorded deliveries` : "Exact delivery → adoption link unavailable"],
    ]);
    if (solver.mixed_calls || solver.unknown_calls) overview.firstChild.append(
      badge(`${solver.mixed_calls || 0} mixed / ${solver.unknown_calls || 0} unknown calls`));
    table(disclosure(overview, "Accounting & review coverage"), ["Measurement", "Recorded value"], [
      ["Known solver reads", solver.reads], ["Mixed / unknown solver calls", `${solver.mixed_calls || 0} / ${solver.unknown_calls || 0}`],
      ["Reads / substantive judgment", `${number(judgment.mean)} · n=${judgment.n || 0}`],
      ["Evidence windows · median / p90", `${number(evidence.median)} / ${number(evidence.p90)}`],
      ["Partial evidence windows", evidence.partial_windows], ["Reads in open / censored windows", evidence.open_reads],
      ["Scope", "Read averages cover reviewed closed goal windows. Question filter changes the graph and endpoints."],
      ["Cost ownership", "Request-owned calls; final reported tokens; wall span includes waits."],
    ]);
    const graphPanel = panel(root, "Evidence & decision process", true);
    const projected = p.graph;
    const eligible = (projected?.nodes || []).filter(n => n.episode_id === scope.episode_id && n.goal_revision_id === scope.goal_revision_id);
    const visible = boundedGraph(eligible, projected?.relations || [], this.limit);
    const {rows: shown, owners} = processGroups(visible.nodes), positions = new Map();
    const outcome = eligible.findLast(n => n.is_endpoint);
    const outcomeStrip = element("div", "reasoning-outcome-summary");
    if (outcome) outcomeStrip.append(badge(outcome.terminal ? "Outcome" : "Current boundary"), element("strong", "", outcome.label),
      button("Jump to outcome", () => graph.querySelector('[data-outcome="true"]')?.scrollIntoView({block: "center", behavior: "smooth"})));
    else for (const q of selected) {
      const question = element("div", "reasoning-question"); question.append(badge("Question"), element("strong", "", q.label)); outcomeStrip.append(question);
    }
    graphPanel.append(outcomeStrip);
    if (!projected) graphPanel.append(element("p", "reasoning-notice", "This saved analysis predates the process graph. Refresh analysis, then run Deep evaluation to review relationships."));
    const legend = element("div", "reasoning-legend");
    legend.append(element("span", "", "━ Execution flow"), element("span", "", "┄ Evidence / reasoning relationship"),
      element("span", "", "Flow arrows show progression; support arrows show evidence")); graphPanel.append(legend);
    const scroll = element("div", "reasoning-graph-scroll"), graph = element("div", "reasoning-graph");
    scroll.setAttribute("aria-label", "Reasoning process graph"); scroll.tabIndex = 0; scroll.append(graph); graphPanel.append(scroll);
    // Preserve source order even when a tool's return encloses verification phases.
    let rowIndex = -1, previous = null;
    for (const row of shown) {
      const sameRound = row.round_id && row.round_id === previous?.round_id;
      if (!sameRound || (row.column ?? 0) <= (previous?.column ?? 0) || row.is_endpoint) rowIndex++;
      row.layoutRow = rowIndex;
      previous = row;
    }
    rowIndex++;
    // Leave at least 200px between cards for relationship labels; use spare panel width.
    const width = Math.max(1260, scroll.clientWidth), laneSpacing = (width - 300) / 2;
    const height = Math.max(100, rowIndex * 185 + 45);
    graph.style.width = `${width}px`; graph.style.height = `${height}px`;
    ["Judgment / hypothesis / conclusion", "Action / verification / outcome", "Observation / report"].forEach((name, col) => {
      const title = element("div", "reasoning-column", name); title.style.left = `${col * laneSpacing + 10}px`; graph.append(title);
    });
    const edges = svg("svg", {width, height, "aria-hidden": "true"}); graph.append(edges);
    const defs = svg("defs"), marker = svg("marker", {id: "reasoning-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse"});
    marker.append(svg("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "currentColor"})); defs.append(marker); edges.append(defs);
    const detail = panel(root, "Selected evidence", true); detail.classList.add("reasoning-selection");
    const inspect = row => {
      detail.replaceChildren(element("h3", "", label(row.phase)), element("p", "reasoning-content", row.label));
      table(detail, ["Field", "Value"], [["Source line", row.line], ["Status", label(row.status)],
        ["Basis", row.interpretation_basis || row.basis], ["Rationale", row.rationale || (row.status === "recorded_statement" ? "Agent statement; evidential support has not been assessed." : "Recorded source")]]);
      if (row.content && row.content !== row.label) detail.append(element("pre", "reasoning-content", row.content));
      const related = (projected?.relations || []).filter(r => r.source === row.id || r.target === row.id);
      if (related.length) table(detail, ["Relationship", "Linked phase"], related.map(r => {
        const incoming = r.target === row.id, other = projected.nodes.find(n => n.id === (incoming ? r.source : r.target));
        return [`${incoming ? "←" : "→"} ${relationLabel(r)}`, other?.label || "Source outside selection"];
      }));
      if (row.verification_results) table(detail, ["Check", "Status", "Finding"],
        row.verification_results.map(r => [r.criterion_id || r.event_type || "Check", label(r.status), r.label]));
      if (row.is_endpoint) table(detail, ["Outcome", "Recorded status"], [["Terminal", row.terminal ? "Yes" : "No"], ["Report formed", row.report_formed ? "Yes" : "Not recorded"]]);
      this.refs(detail, row.refs); this.selected = row.id;
    };
    for (const row of shown) {
      const x = (row.column ?? 0) * laneSpacing + 10, y = row.layoutRow * 185 + 40;
      positions.set(row.id, {x, y});
      const node = button("", () => inspect(row)); node.className = `reasoning-node ${row.kind === "claim" ? "reasoning-claim" : ""} ${row.is_endpoint ? "reasoning-outcome" : ""} ${row.kind === "question" ? "reasoning-question" : ""}`;
      if (row.is_endpoint) node.dataset.outcome = "true";
      const highlighted = (this.role === "all" || row.role === this.role || row.role === "process")
        && (!this.question || row.question_ids?.includes(this.question) || row.role === "process");
      node.classList.toggle("reasoning-context-node", !highlighted);
      node.style.left = `${x}px`; node.style.top = `${y}px`;
      node.append(badge(label(row.phase)), element("strong", "", row.label),
        element("small", "muted", `${label(row.status || row.interpretation_basis)} · L${row.line}`));
      node.title = row.label; graph.append(node);
    }
    const drawn = new Set();
    for (const rel of visible.relations) {
      const from = owners.get(rel.source), to = owners.get(rel.target), a = positions.get(from), b = positions.get(to);
      const key = `${from}:${to}:${rel.kind}`;
      if (!a || !b || from === to || drawn.has(key)) continue; drawn.add(key);
      const across = b.x > a.x;
      const sx = across ? a.x + 280 : a.x + 140, sy = across ? a.y + 70 : a.y + 150;
      const tx = across ? b.x : b.x + 140, ty = across ? b.y + 70 : b.y;
      const path = svg("path", {d: across ? `M ${sx} ${sy} C ${sx + 25} ${sy}, ${tx - 25} ${ty}, ${tx} ${ty}`
        : `M ${sx} ${sy} C ${sx} ${sy + 30}, ${tx} ${ty - 30}, ${tx} ${ty}`,
        class: rel.basis === "inferred" ? "reasoning-edge inferred" : "reasoning-edge", "marker-end": "url(#reasoning-arrow)"});
      const title = svg("title"); title.textContent = `${relationLabel(rel)} · ${rel.layer || rel.basis}${rel.meaning ? " · " + rel.meaning : ""}`; path.append(title); edges.append(path);
      const text = svg("text", {x: (sx + tx) / 2, y: (sy + ty) / 2 - 7, class: "reasoning-edge-label"});
      text.textContent = relationLabel(rel); edges.append(text);
    }
    if (!shown.length && projected) graphPanel.append(element("p", "empty-state", "No linked process phases for this selection."));
    if (eligible.length > this.limit) graphPanel.append(button(`Show next ${Math.min(200, eligible.length - this.limit)} phases`, () => {this.limit += 200; this.render();}));
    const prior = shown.find(r => r.id === this.selected); if (prior) inspect(prior);
    else detail.append(element("p", "muted", "Select a phase to read its full content and source evidence."));
    table(disclosure(graphPanel, `Process ledger · ${shown.length} phases`), ["Line", "Phase", "Content", "Evidence"],
      shown.map(r => [r.line, label(r.phase), r.label, button("Inspect", () => inspect(r))]));
    const measure = solver.unknown_calls || solver.mixed_calls ? "tool_calls" : "reads";
    const intervals = panel(root, measure === "reads" ? "Known reads between evidence milestones" : "Tool calls between evidence milestones", true);
    const windows = p.windows.filter(w => w.scope_id === scope.id && w.series === "evidence");
    const max = Math.max(1, ...windows.map(w => w.cost[measure] || 0));
    for (const w of windows) {
      const bar = element("div", "reasoning-window-bar"), meter = element("meter"); meter.min = 0; meter.max = max; meter.value = w.cost[measure] || 0;
      meter.setAttribute("aria-label", `Lines ${w.start_line}–${w.end_line}: ${w.cost[measure] || 0} ${label(measure)}, ${w.status}`);
      bar.append(element("span", "", `L${w.start_line}–${w.end_line}`), meter, element("strong", "", number(w.cost[measure])), badge(w.status)); intervals.append(bar);
    }
    table(disclosure(intervals, "Window ledger"), ["Lines", "Status", "Known reads", "Mixed / unknown", "Solver calls", "Tokens", "Active / wall span"],
      windows.map(w => [`${w.start_line}–${w.end_line}`, w.status, w.cost.reads, `${w.cost.mixed_calls || 0} / ${w.cost.unknown_calls || 0}`,
        w.cost.model_calls, `${number(w.cost.total_tokens)}${w.cost.missing_total_tokens ? " + unknown" : ""}`, `${seconds(w.cost.active_runtime_ms)} / ${seconds(w.cost.wall_ms)}`]));
    const endpoints = panel(root, "Questions & endpoints", true);
    table(endpoints, ["Question", "Endpoint", "State", "Solver calls / steps / reads", "Cycles · closed / open"], selected.map(q => [q.label,
      q.endpoint.kind, q.endpoint.status, q.endpoint.cost ? `${q.endpoint.cost.model_calls} / ${q.endpoint.cost.steps ?? "—"} / ${q.endpoint.cost.reads}` : "—",
      `${q.cycles.closed} / ${q.cycles.open}`]));
    const cycles = p.cycles?.filter(c => selected.some(q => q.id === c.question_id)) || [];
    table(disclosure(endpoints, `Investigation cycles · ${cycles.length}`), ["Lines", "State", "Selected actions"],
      cycles.map(c => [`${c.start_line}–${c.end_line ?? "…"}`, c.status, c.action_ids.length]));
    const timing = panel(root, "Recorded cost by role", true);
    distribution(timing, "Reported tokens", Object.entries(costs).map(([role, c]) => [role, c.total_tokens]));
    table(timing, ["Role", "Model calls", "Tool calls", "Input / output tokens", "Missing total usage", "Tool service time"],
      Object.entries(costs).map(([role, c]) => [role, c.model_calls, c.tool_calls, `${number(c.prompt_tokens)}${c.missing_prompt_tokens ? " + unknown" : ""} / ${number(c.completion_tokens)}${c.missing_completion_tokens ? " + unknown" : ""}`,
        c.missing_total_tokens, `${seconds(c.tool_duration_ms)}${c.missing_tool_duration ? ` · ${c.missing_tool_duration} unknown` : ""}`]));
    if (p.validation_diagnostics?.length) table(disclosure(root, `Unaccepted semantic entries · ${p.validation_diagnostics.length}`), ["Field", "Reason"],
      p.validation_diagnostics.map(r => [r.field, r.reason]));
  }
}
