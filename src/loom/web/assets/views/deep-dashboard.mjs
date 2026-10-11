import { element } from "../markdown.mjs";
import { badge, cards, distribution, disclosure, label, number, panel, resultPanel, seconds, table } from "./dashboard.mjs";
import { renderProgressTimeline } from "./behavior-sequence.mjs";

function detail(root, title, key) {
  const node = disclosure(root, title); node.dataset.detail = key; return node;
}
function evidence(view, refs = [], title = "Evidence", key = "") {
  const cell = element("div");
  if (refs.length) view.refs(detail(cell, `${title} · ${refs.length}`, key), refs);
  return cell;
}
function fields(root, rows) { table(root, ["Field", "Detail"], rows.filter(([, value]) => value)); }
const unknownLabels = {missing_assessment: "Evaluator omitted judgment", invalid_assessment: "Invalid evaluator answer",
  missing_citation: "No citations", citation_outside_delivered_ranges: "Citation outside read range",
  evaluator_uncertain: "Insufficient evidence / not applicable", unspecified: "Reason not recorded"};
function unknownReason(row) {
  if (row.unknown_reason) return row.unknown_reason;
  if (row.rationale === "Not assessed") return "missing_assessment";
  if (row.rationale === "Evidence exceeds delivered source ranges") return row.evidence_refs?.length ? "citation_outside_delivered_ranges" : "missing_citation";
  return "evaluator_uncertain";
}
function select(root, title, choices, key, change) {
  const node = element("select"); node.setAttribute("aria-label", title); node.dataset.deepFilter = key;
  for (const [value, text] of choices) { const option = element("option", "", text); option.value = value; node.append(option); }
  node.addEventListener("change", change); root.append(node); return node;
}

export function renderDeepDashboard(view, data, dimensions) {
  const {semantic, base = {}, facts = {}, proposals = []} = data, c = semantic.coverage;
  const root = element("div", "trace-dashboard deep-dashboard"); view.results.append(root);
  const segmentName = id => {
    const segment = facts.segments?.find(s => s.id === id);
    const index = facts.segments?.indexOf(segment);
    const step = segment?.boundary_key?.at(-1);
    return segment ? `Segment ${index + 1}${step != null ? ` · Step ${step}` : ""}` : id || "—";
  };
  const overview = panel(root, "Review overview", true);
  const reviewed = c.attempted_segments || 0, supported = c.supported_segments || 0;
  const pending = c.pending_segments?.length ?? Math.max(0, (c.selected_segments || 0) - reviewed);
  const unselected = c.unselected_segments?.length ?? Math.max(0, c.source_segments - reviewed - pending);
  overview.firstChild.append(badge(reviewed < c.source_segments ? "Sampled review" : "Full review"));
  cards(overview, [["Source segments", number(c.source_segments)], ["Reviewed", number(reviewed)],
    ["Evidence supported", number(supported)], ["Pending", number(pending)]]);
  distribution(overview, "Segment coverage", [["Supported", supported], ["Evidence insufficient", Math.max(0, reviewed - supported)],
    ["Pending", pending], ["Unselected", unselected]]);
  if (reviewed && !supported) overview.append(element("p", "deep-alert", "No evidence-supported judgments in the reviewed sample."));
  else if (!reviewed) overview.append(element("p", "deep-alert", "No focused review saved yet."));
  const limitations = [...new Set(c.limitations || [])];
  if (limitations.length || c.selection_budget?.budget_limited) {
    const notes = detail(overview, `Coverage notes · ${limitations.length + (c.selection_budget?.budget_limited ? 1 : 0)}`, "coverage-notes");
    if (c.selection_budget?.budget_limited) fields(notes, [["Budget-planned sample", `${c.selection_budget.selected_segments} / ${c.selection_budget.requested_max_segments} configured maximum`]]);
    const list = element("ul"); for (const text of limitations) list.append(element("li", "", text)); notes.append(list);
  }

  const summary = element("div", "deep-summary-block"), latest = base.outcomes?.at(-1);
  summary.append(element("h4", "", "Review summary")); overview.append(summary);
  summary.append(element("p", "deep-summary", semantic.summary || "No summary saved"));
  if (latest) {
    const goal = detail(summary, "Task outcome", "task-outcome");
    const revision = facts.goal_revisions?.find(g => g.id === latest.goal_revision_id);
    const recorded = base.runtime_acceptance;
    const acceptance = recorded?.episode_id === latest.episode_id && (!revision?.applied_line || recorded.line >= revision.applied_line) ? recorded : null;
    const state = acceptance?.state, results = state?.results || [];
    fields(goal, [["Goal", latest.objective || facts.goal_revisions?.find(g => g.id === latest.goal_revision_id)?.objective],
      ["Execution", label(latest.execution_status || base.execution_status)],
      ["Runtime acceptance", state ? `${label(state.state)} · ${results.filter(r => r.status === "passed").length}/${state.plan?.criteria?.length || 0} checks` : "Not recorded for this goal"],
      ["Assurance", acceptance ? label(acceptance.assurance || results[0]?.assurance || "unspecified") : null],
      ["Independent verification", ({achieved: "Verified", not_achieved: "Requirements not met", partially_verified: "Partly verified"})[latest.status] || (latest.verification_count ? "Evidence incomplete" : "No independent receipts")]]);
    if (state?.plan?.criteria?.length) table(goal, ["Acceptance condition", "Status", "Evidence basis"], state.plan.criteria.map(r => {
      const result = results.find(v => v.criterion_id === r.id);
      return [r.description, badge(label(result?.status || "pending"), result?.status), result?.reason];
    }));
    if (latest.verification_count) {
      const independent = detail(goal, "Independent verification details", "independent-verification");
      if (latest.explanation) independent.append(element("p", "", latest.explanation));
      if (latest.criteria?.length) table(independent, ["Condition", "Status", "Limitation"], latest.criteria.map(r => [r.description, label(r.status), r.limitation]));
    }
    if (base.outcomes.length > 1) table(detail(goal, "Previous task outcomes", "previous-outcomes"), ["Goal", "Execution", "Acceptance"],
      base.outcomes.slice(0, -1).map(row => [row.objective, label(row.execution_status), label(row.status)]));
  }
  const dimensionsPanel = panel(root, "Behavior dimensions", true);
  table(dimensionsPanel, ["Dimension", "Judgments", "Supported / reviewed", "Unreviewed"], Object.entries(dimensions).map(([id, title]) => {
    const row = c.dimensions?.[id] || {};
    const name = element("div", "deep-dimension", title);
    const bar = element("meter"); bar.max = row.attempted || 1; bar.value = row.supported || 0;
    bar.setAttribute("aria-label", `${title}: ${row.supported || 0} evidence supported out of ${row.attempted || 0} reviewed`);
    name.append(bar);
    const judgments = element("div", "deep-verdicts");
    const assessed = semantic.assessments.filter(a => a.dimension === id);
    for (const status of ["effective", "mixed", "ineffective", "unknown"]) {
      const count = assessed.filter(a => a.status === status).length;
      if (count) judgments.append(badge(`${label(status)} · ${count}`, status === "effective" ? "passed" : status === "ineffective" ? "failed" : ""));
    }
    if (!assessed.length) judgments.append(badge(row.supported ? "Not detailed" : `Unknown · ${number(row.unknown)}`));
    return [name, judgments, `${number(row.supported)} / ${number(row.attempted)}`, number((row.pending || 0) + (row.unselected ?? unselected))];
  }));
  const unknownCounts = {};
  for (const row of semantic.assessments.filter(a => a.status === "unknown")) {
    const reason = unknownReason(row); unknownCounts[reason] = (unknownCounts[reason] || 0) + 1;
  }
  if (Object.keys(unknownCounts).length) {
    const reasons = detail(dimensionsPanel, "Why judgments are unknown", "unknown-reasons");
    table(reasons, ["Reason", "Judgments"], Object.entries(unknownCounts).map(([reason, count]) => [unknownLabels[reason] || label(reason), number(count)]));
  }

  const findings = resultPanel(root, "Behavior findings", semantic.diagnoses.length, true, "findings");
  findings.firstChild.append(badge(number(semantic.diagnoses.length)));
  if (!semantic.diagnoses.length) findings.append(element("p", "empty-state", "No behavior findings saved"));
  for (const [i, row] of semantic.diagnoses.entries()) {
    const card = element("article", "deep-finding"), header = element("header", "deep-finding-heading");
    header.append(element("strong", "", (row.dimensions || [row.dimension]).map(d => dimensions[d] || label(d)).join(" / ")),
      badge(label(row.epistemic_status)));
    card.append(header, element("p", "deep-observation", row.observation));
    if (row.mechanism) card.append(element("p", "muted", row.mechanism));
    const more = detail(card, "Evidence & analysis", `finding-${row.id || i}`);
    fields(more, [["Pattern", label(row.pattern_type)], ["Segment", segmentName(row.segment_id)],
      ["Alternative explanations", row.alternatives?.join("; ")], ["Improvement hypothesis", row.improvement_hypothesis],
      ["Predicted endpoint", row.predicted_endpoint], ["Validation", row.validation], ["Preserve", row.preserve]]);
    view.refs(more, row.supporting_refs || []);
    if (row.counterevidence_refs?.length) { more.append(element("h4", "", "Counterevidence")); view.refs(more, row.counterevidence_refs); }
    findings.append(card);
  }
  renderProgressTimeline(root, semantic, facts, {evidence: ref => view.evidence(ref)});

  const metrics = Object.entries(semantic.efficiency || {});
  const measured = metrics.filter(([, row]) => row.status !== "unknown");
  const efficiency = resultPanel(root, "Reasoning efficiency", measured.length, true, "efficiency");
  efficiency.firstChild.append(badge(`${measured.length} / ${metrics.length} measured`));
  const metricRows = rows => rows.map(([name, row]) => {
    const source = element("div"), details = detail(source, "Endpoints & basis", `efficiency-${name}`);
    if (row.basis) details.append(element("p", "muted", row.basis));
    for (const [i, endpoint] of (row.observations || []).entries()) {
      const subject = semantic.behavior_graph?.nodes.find(n => n.id === endpoint.subject_id)?.label
        || facts.goal_revisions?.find(g => g.id === endpoint.subject_id)?.objective || "Endpoint";
      fields(details, [["Subject", subject], ["Lines", `${endpoint.start_line}–${endpoint.end_line}`],
        ["Tokens", number(endpoint.known_tokens)], ["Model / tool calls", `${number(endpoint.model_calls)} / ${number(endpoint.tool_calls)}`]]);
      details.append(evidence(view, endpoint.evidence_refs, "Evidence", `endpoint-${name}-${i}`));
    }
    return [label(name), badge(label(row.status)), number(row.known_tokens), number(row.model_calls), number(row.tool_calls), seconds(row.elapsed_ms), source];
  });
  const headers = ["Metric", "Status", "Tokens", "Model calls", "Tools", "Elapsed", "Source"];
  if (measured.length) table(efficiency, headers, metricRows(measured));
  else efficiency.append(element("p", "empty-state", "No measured endpoints"));
  const unknown = metrics.filter(([, row]) => row.status === "unknown");
  if (unknown.length) table(detail(efficiency, `Unmeasured metrics · ${unknown.length}`, "unmeasured"), headers, metricRows(unknown));

  const evolve = resultPanel(root, "Evolution hypotheses", proposals.length, true, "evolve");
  evolve.firstChild.append(badge(`${proposals.length} proposed`));
  if (proposals.length) evolve.firstChild.append(badge("Not tested"));
  if (!proposals.length) evolve.append(element("p", "empty-state", "No hypotheses proposed"));
  for (const [i, row] of proposals.entries()) {
    const card = element("article", "deep-finding"), header = element("header", "deep-finding-heading");
    header.append(element("strong", "", label(row.surface)), badge(label(row.state)), badge(label(row.readiness)));
    card.append(header, element("p", "deep-observation", row.hypothesis));
    const more = detail(card, "Validation & evidence", `proposal-${row.id || i}`);
    fields(more, [["Predicted endpoint", row.predicted_endpoint || "Unspecified"], ["Validation", row.validation || "Needs design"],
      ["Preserve", row.preserve?.join("; ")]]);
    view.refs(more, row.supporting_refs || []); evolve.append(card);
  }

  const assessments = detail(root, `Segment assessments · ${semantic.assessments.length}`, "assessments");
  assessments.classList.add("dashboard-panel", "dashboard-wide");
  const filters = element("div", "deep-assessment-filters"), list = element("div");
  let dimension, status;
  const update = () => {
    if (!dimension || !status) return;
    list.replaceChildren();
    const rows = semantic.assessments.filter(row => (!dimension.value || row.dimension === dimension.value) && (!status.value || row.status === status.value));
    table(list, ["Segment", "Dimension", "Judgment", "Rationale", "Evidence"], rows.map(row =>
      [segmentName(row.segment_id), dimensions[row.dimension] || label(row.dimension), badge(label(row.status)),
        row.status === "unknown" ? `${unknownLabels[unknownReason(row)] || label(unknownReason(row))}: ${row.rationale}` : row.rationale,
        evidence(view, row.evidence_refs, "Evidence", `assessment-${row.segment_id}-${row.dimension}`)]));
  };
  dimension = select(filters, "Filter assessment dimension", [["", "All dimensions"], ...Object.entries(dimensions)], "dimension", update);
  status = select(filters, "Filter assessment status", [["", "All judgments"], ...["effective", "ineffective", "mixed", "unknown"].map(v => [v, label(v)])], "status", update);
  assessments.append(filters, list); update();
  const graph = semantic.behavior_graph || {nodes: [], edges: []};
  if (graph.nodes.length) {
  const graphPanel = detail(root, `Behavior connections · ${graph.nodes.length} nodes`, "graph"); graphPanel.classList.add("dashboard-panel", "dashboard-wide");
  table(graphPanel, ["Kind", "Observation", "Evidence"], graph.nodes.map(node => [label(node.kind), node.label, evidence(view, node.evidence_refs, "Evidence", `node-${node.id}`)]));
  const names = new Map(graph.nodes.map(n => [n.id, n.label]));
  if (graph.edges.length) table(graphPanel, ["From", "Relationship", "To", "Basis"], graph.edges.map(e => [names.get(e.from) || e.from, label(e.kind), names.get(e.to) || e.to, label(e.basis)]));
  }
  if (semantic.preserved_behaviors.length) {
  const preserve = detail(root, `Behaviors to preserve · ${semantic.preserved_behaviors.length}`, "preserve"); preserve.classList.add("dashboard-panel", "dashboard-wide");
  table(preserve, ["Behavior", "Evidence"], semantic.preserved_behaviors.map((row, i) => [row.behavior, evidence(view, row.evidence_refs, "Evidence", `preserve-${i}`)]));
  }
}
