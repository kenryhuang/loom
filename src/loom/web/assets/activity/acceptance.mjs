export function isAcceptanceRound(event) {
  return !!event.payload?.acceptance && [
    "acceptance.verifying", "verification.started", "verification.completed",
    "acceptance.gate.passed", "acceptance.gate.blocked",
  ].includes(event.type);
}

export function acceptanceDescriptor(event) {
  const value = event.payload.acceptance;
  return {
    kind: "acceptance",
    key: `acceptance:${JSON.stringify([event.run_id, value.goal_digest, value.revision, value.attempts])}`,
    details: event.payload,
  };
}

export function presentAcceptance({data}) {
  const value = data.acceptance;
  const results = new Map((value.results || []).map(row => [row.criterion_id, row]));
  const criteria = value.plan?.criteria || [];
  const passed = criteria.filter(c => results.get(c.id)?.status === "passed").length;
  const failed = ["blocked", "needs_repair"].includes(value.state) ||
    [...results.values()].some(r => ["failed", "blocked"].includes(r.status));
  return {
    kind: "acceptance",
    title: value.state === "passed" ? "Acceptance passed" : failed ? "Acceptance needs attention" : "Verifying acceptance",
    subject: `${passed}/${criteria.length} checks passed`,
    outcome: `Round ${value.attempts || 0}`,
    state: failed ? "failed" : value.state === "passed" ? "completed" : "running",
    sections: [
      {label: "", value: value.reason},
      ...criteria.map(c => ({label: "", kind: "acceptance-check", value: {...c, ...results.get(c.id)}})),
      ...(value.plan?.unresolved_requirements || []).map(value => ({label: "Unresolved requirement", value})),
    ],
  };
}
