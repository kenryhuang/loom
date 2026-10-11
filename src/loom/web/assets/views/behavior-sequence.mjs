import { element } from "../markdown.mjs";
import { table, badge, cards, disclosure, resultPanel, seconds, number, label } from "./dashboard.mjs";

const labels = {
  resource_observed: "File content observed", tool_result_observed: "New recorded tool result",
  artifact_changed: "File version changed", plan_snapshot: "Plan state recorded", verification_result: "Verification recorded",
  information_gain: "New evidence", hypothesis_eliminated: "Hypothesis eliminated", decision: "Key decision",
  artifact_change: "Useful artifact change", blocker_resolution: "Blocker resolved", verification: "Verification",
  decision_recorded: "Recorded decision / control action", plan_binding: "Execution plan binding",
  same_tool_result: "Identical returned result", same_resource_content: "Identical resource content",
  no_new_result_window: "No new recorded result",
  first_content: "First solver tool result", first_decision: "First recorded decision / control action",
  first_verification_pass: "First passed check", first_acceptance_pass: "Acceptance passed",
};

function refs(parent, values, actions) {
  if (!actions.evidence) return;
  for (const ref of values || []) {
    const button = element("button", "secondary small", `Evidence · line ${ref.line_number}`);
    button.addEventListener("click", () => actions.evidence(ref));
    parent.append(button);
  }
}

function section(root, title, open = false) {
  const node = element("details", "trajectory-section");
  node.open = open;
  node.append(element("summary", "", title));
  root.append(node);
  return node;
}


export function renderBehaviorSequence(root, sequence, actions = {}) {
  if (!sequence) return;
  const outer = section(root, "Behavior facts · all runs");
  const plans = sequence.plan_execution || [], repetitions = sequence.repetitions || [], signals = sequence.signals || [];
  const windows = (sequence.candidate_windows || []).filter(w => w.pattern_type === "low_progress_window");
  cards(outer, [["Plan items", String(plans.length)], ["Repeated observations", String(repetitions.length)],
    ["Low-progress candidates", String(windows.length)], ["Activity records", String(sequence.observations?.length ?? signals.length)]]);
  const evidenceCell = (values, title = "Evidence") => {
    const node = element("div"), detail = disclosure(node, title); refs(detail, values, actions); return node;
  };
  const coverage = sequence.observation_coverage, repeatCost = sequence.repetition_cost;
  if (coverage) {
    const measured = disclosure(outer, "Observation coverage & repeat cost");
    table(measured, ["Comparable results / completed tools", "Resource identity / tool calls", "Repeated calls", "Known tool time", "Missing tool durations", "Known associated tokens", "Missing model usage"],
      [[`${coverage.comparable_result_calls} / ${coverage.completed_tool_calls}`, `${coverage.resource_identity_calls} / ${coverage.solver_tool_calls}`,
        number(repeatCost?.tool_calls), seconds(repeatCost?.known_tool_ms), number(repeatCost?.unmeasured_tool_calls),
        number(repeatCost?.associated_known_tokens), number(repeatCost?.unmeasured_model_calls)]]);
    measured.append(element("p", "muted", "Resource coverage is independent of result comparison. Associated model usage is counted once per round; it is not an estimate of wasted tokens."));
  }
  if (sequence.milestones?.length) {
    const milestones = disclosure(outer, "Recorded milestones");
    table(milestones, ["Milestone", "Line / time", "Tools", "Model calls", "Known tokens", "Elapsed from goal", "Evidence"], sequence.milestones.map(row => {
      const fact = sequence.observations?.find(f => f.id === row.observation_id);
      return [labels[row.kind] || label(row.kind), `${row.line} · ${row.at || "Time unavailable"}`, number(row.cost?.tool_calls),
        number(row.cost?.model_calls), number(row.cost?.known_tokens), seconds(row.cost?.elapsed_ms), evidenceCell(fact?.evidence_refs)];
    }));
  }
  if (plans.length) {
    const plan = disclosure(outer, `Plan execution · ${plans.length} items`);
    table(plan, ["Plan item", "Declared status", "Recorded activity", "Steps", "Model / tool calls", "History"], plans.map(row => {
      const description = element("div", "", row.description || "Description not recorded");
      if (row.goal_revision_id === null) description.append(element("small", "muted", "Goal association unavailable at this snapshot"));
      if (row.dependencies?.length) description.append(element("small", "muted", `Depends on: ${row.dependencies.join(", ")}`));
      return [description, badge(row.declared_status), label(row.recorded_execution), row.steps.map(s => s.step_number).join(", ") || "—",
        `${row.round_ids.length} / ${row.tool_ids.length}`, evidenceCell(row.evidence_refs, `${row.revisions.length} snapshots`)];
    }));
  }
  if (sequence.plan_bindings?.length) {
    const bindings = disclosure(outer, "Plan bindings & dependency checks");
    const s = sequence.plan_summary || {};
    cards(bindings, [["Bound calls", number(s.bound || 0)], ["Dependencies not recorded complete", number(s.dependency_violations)],
      ["Calls after declared completion", number(s.calls_after_declared_completion)], ["Nodes without recorded calls", number(s.nodes_without_recorded_calls)]]);
    table(bindings, ["Node", "Binding", "Dependencies", "After declared completion", "Evidence"], sequence.plan_bindings.map(row =>
      [row.node_id || "—", label(row.status), row.unmet_dependencies?.length ? row.unmet_dependencies.join(", ") : label(row.dependency_status),
        row.activity_after_declared_completion ? "Yes" : "No", evidenceCell(row.evidence_refs)]));
  }
  if (repetitions.length) {
    const repeats = disclosure(outer, `Repeated content observations · ${repetitions.length}`);
    table(repeats, ["Resource / result", "Lines", "Scope", "Known tool time", "Associated tokens", "Evidence"], repetitions.map(row => {
      const detail = evidenceCell(row.evidence_refs);
      detail.firstChild.append(element("p", "muted", row.basis));
      for (const limitation of row.limitations || []) detail.firstChild.append(element("p", "notice", limitation));
      return [row.path, `${row.first_line} → ${row.line}`, badge(labels[row.kind] || label(row.status)),
        seconds(row.cost?.known_tool_ms), number(row.cost?.associated_known_tokens), detail];
    }));
  }
  if (windows.length) {
    const candidates = disclosure(outer, `Low-progress candidates · ${windows.length}`);
    table(candidates, ["Window", "Signal", "Evidence"], windows.map(row =>
      [`${row.tool_calls} tool calls`, row.basis, evidenceCell(row.evidence_refs)]));
  }
  const observations = sequence.observations || signals;
  if (observations.length) {
    const activity = disclosure(outer, `Activity timeline · ${observations.length}`);
    table(activity, ["Line", "Activity", "Detail", "Status / assurance / goal binding", "Tools / tokens since prior event", "Evidence"], observations.map(row =>
      [row.line, labels[row.kind] || row.kind, row.label, [row.status, row.assurance, row.goal_binding].filter(Boolean).map(label).join(" · ") || "—",
        row.since_previous_observation ? `${number(row.since_previous_observation.tool_calls)} / ${number(row.since_previous_observation.known_tokens)}` : "—",
        evidenceCell(row.evidence_refs)]));
  }
  const notes = disclosure(outer, "Scope & measurement notes");
  notes.append(element("p", "muted", "All runs. Recorded activity and candidate windows are not judgments of useful progress or waste."));
  for (const limitation of sequence.limitations || []) notes.append(element("p", "muted", limitation));
}

export function renderProgressTimeline(root, semantic, facts, actions = {}) {
  const observations = facts.sequence?.observations || [];
  const interpretations = [...(semantic.fact_assessments || [])].sort((a, b) =>
    (observations.find(f => f.id === a.fact_id)?.line || a.fact_line || 0) - (observations.find(f => f.id === b.fact_id)?.line || b.fact_line || 0));
  if (interpretations.length) {
    const block = resultPanel(root, "Behavior fact interpretations", interpretations.length, true, "fact-interpretations");
    table(block, ["Recorded fact", "Judgment", "Rationale", "Evidence"], interpretations.map(row => {
      const fact = observations.find(f => f.id === row.fact_id), proof = element("div");
      const more = disclosure(proof, "Source evidence"); more.dataset.detail = `fact-${row.fact_id}`;
      const acceptance = facts.acceptance_history?.filter(a => a.episode_id === fact?.episode_id && a.line <= fact.line).at(-1);
      const criterion = acceptance?.state?.plan?.criteria?.find(c => c.id === fact?.criterion_id);
      const name = fact?.kind === "verification_result" ? criterion?.description || fact.event_type || fact.label : fact?.label;
      if (fact && name !== fact.label) more.append(element("p", "", fact.label));
      refs(more, row.evidence_refs, actions);
      return [`${labels[fact?.kind || row.fact_kind] || "Observation"} · ${name || row.fact_label || row.fact_id}`,
        badge(label(row.status), row.status === "effective" ? "passed" : row.status === "ineffective" ? "failed" : ""), row.rationale, proof];
    }));
  }
  const wide = !(semantic.progress?.length && semantic.plan_assessments?.length);
  const progress = resultPanel(root, "Evidence-backed progress", semantic.progress?.length, wide, "progress");
  progress.firstChild.append(badge(`${semantic.progress?.length || 0} landmarks`));
  if (!semantic.progress?.length) progress.append(element("p", "empty-state", "No cited progress landmarks"));
  const timeline = element("ol", "deep-landmarks");
  for (const [index, row] of (semantic.progress || []).entries()) {
    const item = element("li"), header = element("header", "deep-finding-heading");
    header.append(badge(`Step ${row.step_number ?? "—"}`), element("strong", "", labels[row.kind] || row.kind));
    item.append(header, element("p", "", row.label));
    const more = disclosure(item, "Interval cost & evidence"); more.dataset.detail = `progress-${row.id || index}`;
    const cost = row.since_previous_progress;
    if (row.fact_ids?.length) table(more, ["Linked recorded fact", "Line"], row.fact_ids.map(id => {
      const fact = observations.find(f => f.id === id); return [fact?.label || id, fact?.line];
    }));
    if (cost && cost.status !== "unknown") table(more, ["Tools", "Model calls", "Known tokens", "Elapsed", "Missing usage"],
      [[number(cost.tool_calls), number(cost.model_calls), number(cost.known_tokens), seconds(cost.elapsed_ms), number(cost.unmeasured_model_calls)]]);
    else more.append(element("p", "muted", "Interval cost unavailable"));
    refs(more, row.evidence_refs, actions); timeline.append(item);
  }
  progress.append(timeline);
  const notes = disclosure(progress, "Measurement notes"); notes.dataset.detail = "progress-notes";
  notes.append(element("p", "muted", "Selected landmarks only; gaps may contain unreviewed progress. Elapsed time includes waits. Interval costs are not estimated savings."));
  const plans = resultPanel(root, "Plan adherence", semantic.plan_assessments?.length, wide, "plans");
  plans.firstChild.append(badge(`${semantic.plan_assessments?.length || 0} assessments`));
  if (!semantic.plan_assessments?.length) plans.append(element("p", "empty-state", "No plan assessments saved"));
  else {
    const groups = new Map();
    for (const row of semantic.plan_assessments) {
      const snapshot = (facts.plan_revisions || []).find(p => p.id === row.plan_snapshot_id);
      const node = snapshot?.nodes.find(n => n.id === row.node_id);
      const key = `${snapshot?.episode_id || ""}:${snapshot?.goal_revision_id || ""}:${snapshot?.plan_id || row.plan_snapshot_id}:${row.node_id}`;
      if (!groups.has(key)) groups.set(key, {description: node?.description || row.node_id, rows: []});
      groups.get(key).rows.push(row);
    }
    table(plans, ["Plan item", "Judgments", "Rationale & evidence"], [...groups].map(([key, group]) => {
      const more = element("div"), details = disclosure(more, `${group.rows.length} assessments`); details.dataset.detail = `plan-${key}`;
      for (const row of group.rows) {
        const segment = (facts.segments || []).find(s => s.id === row.segment_id);
        const step = segment?.boundary_key?.at(-1);
        details.append(badge(`${step != null ? `Step ${step} · ` : ""}${label(row.status)}`), element("p", "", row.rationale));
        const node = (facts.plan_revisions || []).find(p => p.id === row.plan_snapshot_id)?.nodes.find(n => n.id === row.node_id);
        if (node?.description && node.description !== group.description) details.append(element("p", "muted", node.description));
        refs(details, row.evidence_refs, actions);
      }
      const judgments = [...new Set(group.rows.map(r => label(r.status)))].join(" / ");
      return [group.description, badge(judgments), more];
    }));
  }
}
