export function isAcceptanceRound(event) {
  return !!event.payload?.acceptance && [
    "acceptance.verifying", "verification.started", "verification.completed",
    "verification.batch.started", "verification.batch.completed", "verification.pages.planned",
    "verification.selection.started", "verification.selection.completed",
    "acceptance.gate.passed", "acceptance.gate.blocked",
    "visual.capture.started", "visual.capture.completed", "visual.capture.failed",
    "acceptance.budget.renewed",
    "acceptance.feedback.applied", "acceptance.feedback.resolved", "acceptance.feedback.stalled",
  ].includes(event.type);
}

export function acceptanceDescriptor(event) {
  const value = event.payload.acceptance;
  return {
    kind: "acceptance",
    key: value.feedback?.id ? `acceptance:${value.feedback.id}` :
      `acceptance:${JSON.stringify([event.run_id, value.goal_digest, value.revision, value.budget?.generation || 0, value.verification_sequence ?? value.attempts])}`,
    details: {...event.payload, progress_event: {
      type: event.type, stage: event.payload.stage, criterion_ids: event.payload.criterion_ids,
      page: event.payload.page, sources: event.payload.sources, selected: event.payload.selected,
    }},
  };
}

export function presentAcceptance({data}) {
  const value = data.acceptance;
  const results = new Map((value.results || []).map(row => [row.criterion_id, row]));
  const criteria = value.plan?.criteria || [];
  const passed = criteria.filter(c => results.get(c.id)?.status === "passed").length;
  const failed = ["blocked", "needs_repair"].includes(value.state) ||
    [...results.values()].some(r => ["failed", "blocked"].includes(r.status));
  const notRun = criteria.length > 0 && !criteria.some(c => results.has(c.id));
  const repairing = value.state === "needs_repair" && value.decision?.outcome === "repair" && value.feedback?.state === "applied";
  return {
    kind: "acceptance",
    title: value.state === "passed" ? "Acceptance passed" : repairing ? "Repairing acceptance gaps" : failed && notRun ? "Acceptance blocked before checks" : failed ? "Acceptance needs attention" : "Verifying acceptance",
    subject: notRun ? `${criteria.length} checks pending · No results yet` : `${passed}/${criteria.length} checks passed`,
    outcome: acceptanceProgressText(value, data.progress_event) || `Round ${value.attempts || 0}`,
    state: value.state === "passed" ? "completed" : repairing ? "running" : failed ? "failed" : "running",
    sections: [
      {label: "", value: value.reason},
      ...(acceptanceProgressText(value, data.progress_event) ? [{label: "Verification progress", value: acceptanceProgressText(value, data.progress_event)}] : []),
      ...(value.decision ? [{label: "Next action", value: `${value.decision.outcome} · ${value.decision.reason_code}`}]: []),
      ...(value.feedback ? [{label: "Repair", value: `${value.feedback.repair_round} / ${value.feedback.repair_limit} · ${value.feedback.state}`}]: []),
      ...(value.budget ? [{label: 'Acceptance budget', value: acceptanceBudgetText(value)}] : []),
      ...(value.input_budget ? [{label: 'Input budget', value:
        `~${value.input_budget.estimated_input_tokens.toLocaleString()} / ${value.input_budget.max_input_tokens.toLocaleString()} tokens · ${value.input_budget.context_window_percent}% context · ${value.input_budget.context_source}`}]: []),
      ...criteria.map(c => ({label: "", kind: "acceptance-check", value: {...c, ...results.get(c.id)}})),
      ...(value.plan?.unresolved_requirements || []).map(value => ({label: "Unresolved requirement", value})),
    ],
  };
}

export function acceptanceBudgetText(value) {
  const b = value?.budget;
  if (!b) return '';
  return `Model calls ${b.model_calls} / ${b.max_model_calls} · Time ${Math.ceil(b.elapsed_seconds)} / ${b.max_seconds}s · ${b.max_repairs == null ? `Failed candidates ${b.failed_attempts} / ${b.max_failed_attempts}` : `Repairs ${b.repair_rounds} / ${b.max_repairs}`} · Allocation ${b.generation + 1}`;
}


export function acceptanceProgressText(value, event = {}) {
  if (value.state === "passed") return "All required checks and goal review passed";
  const current = value.verification_progress?.current || event || {};
  const stage = current.stage || (current.type?.includes("selection") ? "select_evidence" : null);
  const ids = current.criterion_ids || [];
  const criteria = new Map((value.plan?.criteria || []).map(c => [c.id, c.description]));
  const target = ids.map(id => id === "goal" ? "Overall goal" : criteria.get(id) || id).join("; ");
  let label = stage === "select_evidence" ? "Selecting evidence" : stage === "verify_page" ? "Inspecting evidence" :
    stage === "verify" ? (ids.includes("goal") ? "Checking goal coverage and consistency" : "Judging requirements") : stage === "check" ? "Running check" : "";
  if (!label) return "";
  const saved = current.state === "saved" || current.type?.endsWith(".completed");
  if (current.page?.total) {
    label = saved ? `Evidence page ${current.page.index}/${current.page.total} saved; verdict pending` :
      current.state === "planned" ? `${current.page.index}/${current.page.total} evidence pages saved` :
      `Inspecting evidence page ${current.page.index}/${current.page.total}`;
  } else if (stage === "select_evidence" && current.sources != null) {
    label = saved && current.selected != null ? `Selected ${current.selected} of ${current.sources} sources; judgment pending` : `Selecting from ${current.sources} sources`;
  }
  const blocked = ["blocked", "needs_repair"].includes(value.state);
  return `${blocked ? "Stopped at: " : ""}${label}${target ? ` · ${target}` : ""}`;
}
