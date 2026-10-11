import { element } from "../markdown.mjs";
import { table, badge, cards, disclosure } from "./dashboard.mjs";

const labels = {
  resource_observed: "File content observed", tool_result_observed: "New recorded tool result",
  artifact_changed: "File version changed", plan_snapshot: "Plan state recorded", verification_result: "Verification recorded",
  information_gain: "New evidence", hypothesis_eliminated: "Hypothesis eliminated", decision: "Key decision",
  artifact_change: "Useful artifact change", blocker_resolution: "Blocker resolved", verification: "Verification",
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

function costLabel(cost) {
  if (!cost || cost.status === "unknown") return "Interval cost unavailable";
  return `${cost.tool_calls ?? 0} tools · ${cost.model_calls ?? 0} model calls · ${cost.known_tokens ?? 0} known tokens`
    + (cost.elapsed_ms == null ? " · time unavailable" : ` · ${(cost.elapsed_ms / 1000).toFixed(1)}s elapsed (includes waits)`)
    + (cost.unmeasured_model_calls ? ` · ${cost.unmeasured_model_calls} calls with missing usage` : "");
}

export function renderBehaviorSequence(root, sequence, actions = {}) {
  if (!sequence) return;
  const outer = section(root, "Behavior facts · all runs");
  const plans = sequence.plan_execution || [], repetitions = sequence.repetitions || [], signals = sequence.signals || [];
  const windows = (sequence.candidate_windows || []).filter(w => w.pattern_type === "low_progress_window");
  cards(outer, [["Plan items", String(plans.length)], ["Repeated observations", String(repetitions.length)],
    ["Low-progress candidates", String(windows.length)], ["Activity records", String(signals.length)]]);
  const evidenceCell = (values, title = "Evidence") => {
    const node = element("div"), detail = disclosure(node, title); refs(detail, values, actions); return node;
  };
  if (plans.length) {
    const plan = disclosure(outer, `Plan execution · ${plans.length} items`);
    table(plan, ["Plan item", "Declared status", "Steps", "Model / tool calls", "History"], plans.map(row => {
      const description = element("div", "", row.description || "Description not recorded");
      if (row.dependencies?.length) description.append(element("small", "muted", `Depends on: ${row.dependencies.join(", ")}`));
      return [description, badge(row.declared_status), row.steps.map(s => s.step_number).join(", ") || "—",
        `${row.round_ids.length} / ${row.tool_ids.length}`, evidenceCell(row.evidence_refs, `${row.revisions.length} snapshots`)];
    }));
  }
  if (repetitions.length) {
    const repeats = disclosure(outer, `Repeated resource observations · ${repetitions.length}`);
    table(repeats, ["Resource", "Lines", "Status", "Evidence"], repetitions.map(row => {
      const detail = evidenceCell(row.evidence_refs);
      detail.firstChild.append(element("p", "muted", row.basis));
      for (const limitation of row.limitations || []) detail.firstChild.append(element("p", "notice", limitation));
      return [row.path, `${row.first_line} → ${row.line}`, badge(row.status.replaceAll("_", " ")), detail];
    }));
  }
  if (windows.length) {
    const candidates = disclosure(outer, `Low-progress candidates · ${windows.length}`);
    table(candidates, ["Window", "Signal", "Evidence"], windows.map(row =>
      [`${row.tool_calls} tool calls`, row.basis, evidenceCell(row.evidence_refs)]));
  }
  if (signals.length) {
    const activity = disclosure(outer, `Activity timeline · ${signals.length}`);
    table(activity, ["Line", "Step", "Activity", "Detail", "Evidence"], signals.map(row =>
      [row.line, row.step_number, labels[row.kind] || row.kind, row.label, evidenceCell(row.evidence_refs)]));
  }
  const notes = disclosure(outer, "Scope & measurement notes");
  notes.append(element("p", "muted", "All runs. Recorded activity and candidate windows are not judgments of useful progress or waste."));
  for (const limitation of sequence.limitations || []) notes.append(element("p", "muted", limitation));
}

export function renderProgressTimeline(root, semantic, facts, actions = {}) {
  const progress = section(root, `Evidence-backed progress · ${semantic.progress?.length || 0} landmarks`, true);
  progress.append(element("p", "muted", "Semantic judgments from selected segments. Gaps may contain unreviewed progress; interval costs are not estimated savings."));
  if (!semantic.progress?.length) progress.append(element("p", "muted", "No cited progress landmarks were saved in this evaluation."));
  for (const row of semantic.progress || []) {
    const item = element("article", "evaluation-finding");
    item.append(element("strong", "", `Step ${row.step_number ?? "?"} · ${labels[row.kind] || row.kind}: ${row.label}`),
      element("p", "muted", `Since previous selected landmark: ${costLabel(row.since_previous_progress)}`));
    refs(item, row.evidence_refs, actions);
    progress.append(item);
  }
  const plans = section(root, `Plan adherence · ${semantic.plan_assessments?.length || 0} assessments`, true);
  for (const row of semantic.plan_assessments || []) {
    const snapshot = (facts.plan_revisions || []).find(p => p.id === row.plan_snapshot_id);
    const node = snapshot?.nodes.find(n => n.id === row.node_id);
    const item = element("article", "evaluation-finding");
    item.append(element("strong", "", node?.description || row.node_id),
      element("p", "", `${row.status.replaceAll("_", " ")} · ${row.rationale}`));
    refs(item, row.evidence_refs, actions);
    plans.append(item);
  }
  if (!semantic.plan_assessments?.length) plans.append(element("p", "muted", "Plan adherence has not been assessed in this result."));
}
