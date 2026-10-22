import { element } from "../markdown.mjs";

const states = {
  passed: { symbol: "✓", label: "Passed" },
  pending: { symbol: "○", label: "Pending" },
  blocked: { symbol: "⊘", label: "Blocked" },
  failed: { symbol: "×", label: "Failed" },
};

function statusIcon(symbol) {
  const icon = element("span", "acceptance-status-icon", symbol);
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

export function acceptanceChecklist(current, activeCheck, actions, expanded = new Set()) {
  const root = element("div", "acceptance-checks");
  const criteria = current.plan?.criteria || [];
  const results = new Map((current.results || []).map(result => [result.criterion_id, result]));
  const counts = { passed: 0, pending: 0, blocked: 0, failed: 0 };
  const list = element("ul", "acceptance-checklist");
  list.setAttribute("aria-label", "Acceptance checklist");
  // Keep list semantics in Safari when the visual list markers are removed.
  list.setAttribute("role", "list");
  for (const criterion of criteria) {
    const result = results.get(criterion.id);
    const status = result?.deferred ? "pending" : states[result?.status] ? result.status : "pending";
    counts[status]++;
    const checking = current.state === "verifying" && activeCheck === criterion.description && status === "pending";
    const label = result?.deferred ? "Pending review" : checking ? "Checking" : states[status].label;
    const item = element("li"), row = element("details", "acceptance-item");
    row.dataset.status = status;
    row.dataset.criterionId = criterion.id;
    row.open = expanded.has(criterion.id);
    if (checking) row.setAttribute("aria-current", "step");
    const summary = element("summary", "acceptance-item-heading");
    summary.setAttribute("aria-label", `${label}: ${criterion.description}`);
    const text = element("span", "acceptance-item-copy");
    text.append(element("span", "acceptance-item-description", criterion.description));
    const icon = statusIcon(checking ? "↻" : states[status].symbol);
    icon.title = label;
    summary.append(icon, text);
    row.append(summary);
    const detail = element("div", "acceptance-item-detail");
    if (result?.reason) detail.append(element("p", "acceptance-item-reason", result.reason));
    detail.append(element("small", "muted", `${criterion.verifier} · ${result?.assurance || "recorded check"}`));
    if (result?.artifact?.sha256) {
      const link = element("button", "output-link", "View verification evidence");
      link.type = "button";
      link.onclick = () => actions.artifact(result.artifact.sha256);
      detail.append(link);
    }
    row.append(detail); item.append(row); list.append(item);
  }
  if (criteria.length) {
    const progress = element("div", "acceptance-progress");
    progress.append(element("strong", "", `${counts.passed} of ${criteria.length} checks passed`));
    const bar = element("progress");
    bar.max = criteria.length; bar.value = counts.passed;
    bar.setAttribute("aria-label", "Acceptance checks passed");
    progress.append(bar);
    const legend = element("div", "acceptance-legend");
    for (const [status, count] of Object.entries(counts)) {
      if (!count) continue;
      const badge = element("span", "acceptance-count"); badge.dataset.status = status;
      badge.append(statusIcon(states[status].symbol), document.createTextNode(`${count} ${states[status].label.toLowerCase()}`));
      legend.append(badge);
    }
    progress.append(legend); root.append(progress, list);
  }
  return root;
}
