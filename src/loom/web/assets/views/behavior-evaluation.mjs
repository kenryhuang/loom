import { element } from "../markdown.mjs";

export const BEHAVIOR_DIMENSIONS = {
  intent_alignment: "Intent alignment",
  plan_quality: "Plan quality",
  progress_effectiveness: "Progress effectiveness",
  investigation_efficiency: "Investigation efficiency",
  adaptation_recovery: "Adaptation and recovery",
};

const executionLabel = (value) =>
  ({
    completed: "Completed",
    running: "Running",
    failed: "Failed",
    stopped: "Stopped",
    suspended: "Waiting to resume",
    paused: "Paused",
    queued: "Queued",
    not_started: "Not started",
  })[value] || "Not recorded";

const acceptanceLabel = (outcome) =>
  ({
    achieved: "Verified",
    not_achieved: "Requirements not met",
    partially_verified: "Partly verified",
  })[outcome.status] ||
  (outcome.verification_count ? "Needs verification" : "Not assessed");

export function renderBaseEvaluation(root, base, goals = [], plans = []) {
  const section = element("section", "trajectory-section");
  section.append(element("h3", "", "Task result"));
  const acceptance = base.runtime_acceptance?.state;
  if (acceptance) section.append(element("p", "", `Runtime acceptance: ${acceptance.state === "passed" ? "Passed" : acceptance.state}`),
    element("p", "muted", acceptance.reason), element("small", "muted", "Recorded acceptance checks include model judgments; independent goal verification is reported separately."));
  const outcomes = base.outcomes || [];
  const objective = (row) =>
    row.objective ||
    goals.find((g) => g.id === row.goal_revision_id)?.objective ||
    "Task goal was not recorded";
  function renderTask(parent, outcome) {
    parent.append(
      element("p", "", objective(outcome)),
      element(
        "p",
        "",
        `Execution: ${executionLabel(outcome.execution_status || base.execution_status)}`,
      ),
      element("p", "", `Goal acceptance: ${acceptanceLabel(outcome)}`),
      element(
        "p",
        "muted",
        outcome.explanation ||
          "No goal acceptance checks were recorded for this execution.",
      ),
    );
    if (outcome.criteria?.length) {
      const checks = element("details", "trajectory-section");
      checks.append(
        element(
          "summary",
          "",
          `Acceptance requirements · ${outcome.criteria.length}`,
        ),
      );
      for (const criterion of outcome.criteria) {
        const label =
          {
            supported: "Verified",
            contradicted: "Not met",
            not_applicable: "Not applicable",
          }[criterion.status] || "Awaiting verification";
        checks.append(element("p", "", `${label} · ${criterion.description}`));
        if (criterion.limitation)
          checks.append(element("p", "muted", criterion.limitation));
      }
      parent.append(checks);
    }
  }
  if (outcomes.length) renderTask(section, outcomes.at(-1));
  else
    section.append(
      element("p", "muted", "No task execution is recorded in this snapshot."),
    );
  if (outcomes.length > 1) {
    const history = element("details", "trajectory-section");
    history.append(
      element("summary", "", `Previous tasks · ${outcomes.length - 1}`),
    );
    for (const [index, outcome] of outcomes.slice(0, -1).entries()) {
      const row = element("details", "trajectory-section");
      row.append(
        element(
          "summary",
          "",
          `Task ${index + 1} · ${executionLabel(outcome.execution_status)} · ${objective(outcome).slice(0, 100)}`,
        ),
      );
      renderTask(row, outcome);
      history.append(row);
    }
    section.append(history);
  }
  const diagnostics = element("details", "trajectory-section");
  diagnostics.append(
    element("summary", "", "Analysis details · Goal and plan provenance"),
  );
  for (const outcome of outcomes)
    diagnostics.append(
      element(
        "p",
        "muted small",
        `${outcome.episode_id} · acceptance: ${outcome.status} · goal definition coverage: ${outcome.goal_coverage}`,
      ),
    );
  for (const goal of goals)
    diagnostics.append(
      element("p", "", `${goal.state} · ${goal.objective}`),
      element(
        "small",
        "muted",
        `Accepted at line ${goal.accepted_line}; applied: ${goal.applied_line ?? "not recorded"}; visible to model: ${goal.visible_line ?? "not recorded"} · ${goal.origin}`,
      ),
    );
  for (const plan of plans)
    diagnostics.append(
      element(
        "p",
        "",
        `${plan.kind} · ${plan.state} · evidenced completion: ${plan.evidenced_done}`,
      ),
    );
  section.append(diagnostics);
  root.append(section);
}

export function renderBehaviorEvaluation(view, data) {
  const { semantic, base, facts, proposals } = data;
  renderBaseEvaluation(
    view.results,
    base,
    facts.goal_revisions,
    facts.plan_revisions,
  );
  const c = semantic.coverage;
  view.results.append(
    element("h3", "", "Behavior evaluation v3"),
    element(
      "p",
      "muted",
      `Pipeline: ${c.status} · Whole-source scan: ${c.scan_complete ? "completed" : "incomplete"} · Synthesis: ${c.synthesis_complete ? "completed" : "incomplete"}`,
    ),
    element(
      "p",
      "",
      `Segments: ${c.attempted_segments} attempted · ${c.supported_segments} supported · ${c.source_segments} in source. Unselected segments remain unknown.`,
    ),
  );
  if (c.status !== "complete") {
    view.results.append(element("p", "notice", semantic.diagnoses.length || c.attempted_segments
      ? "Partial behavior review: saved findings below are available now. You can resume later to extend coverage."
      : "No focused review has been saved yet. The scan alone does not establish behavior findings."));
  }
  view.results.append(element("p", "muted", `${c.pending_segments?.length || 0} selected segments pending · ${c.unselected_segments?.length ?? Math.max(0, c.source_segments - c.attempted_segments)} segments outside this review.`));
  if (semantic.summary) view.results.append(element("p", "", semantic.summary));
  for (const limit of c.limitations)
    view.results.append(element("p", "notice", limit));
  const dims = element("div", "evaluation-dimensions");
  for (const [id, label] of Object.entries(BEHAVIOR_DIMENSIONS)) {
    const coverage = c.dimensions[id];
    const card = element("div", "trajectory-metric");
    card.append(
      element("strong", "", label),
      element(
        "p",
        "",
        `${coverage.attempted} attempted · ${coverage.supported} supported · ${coverage.unknown} unknown · ${coverage.pending || 0} pending · ${coverage.unselected ?? c.source_segments} unselected`,
      ),
    );
    dims.append(card);
  }
  view.results.append(dims);
  const findings = view.section(
    `Behavior findings · ${semantic.diagnoses.length}`,
    true,
  );
  for (const d of semantic.diagnoses) {
    const row = element("article", "evaluation-finding");
    row.append(
      element(
        "h3",
        "",
        `${BEHAVIOR_DIMENSIONS[d.dimension]} · ${d.epistemic_status}`,
      ),
      element("p", "", d.observation),
      element("p", "", `Mechanism: ${d.mechanism}`),
      element("p", "muted", `Pattern: ${d.pattern_type} · ${d.segment_id}`),
    );
    view.refs(row, d.supporting_refs);
    if (d.counterevidence_refs.length) {
      row.append(element("strong", "", "Counterevidence"));
      view.refs(row, d.counterevidence_refs);
    }
    if (d.alternatives?.length)
      row.append(
        element(
          "p",
          "",
          `Alternative explanations: ${d.alternatives.join("; ")}`,
        ),
      );
    findings.append(row);
  }
  const efficiency = view.section(
    "Reasoning efficiency · measured endpoints",
    true,
  );
  for (const [name, metric] of Object.entries(semantic.efficiency || {}))
    efficiency.append(
      element(
        "p",
        "",
        `${name.replaceAll("_", " ")}: ${metric.status}${metric.known_tokens != null ? ` · ${metric.known_tokens} known tokens` : ""} · ${metric.basis}`,
      ),
    );
  const assessments = view.section(
    `Segment assessments · ${semantic.assessments.length}`,
    c.status !== "complete" && !semantic.diagnoses.length,
  );
  for (const a of semantic.assessments) {
    const row = element("article");
    row.append(
      element(
        "p",
        "",
        `${a.segment_id} · ${BEHAVIOR_DIMENSIONS[a.dimension]} · ${a.status}`,
      ),
      element("p", "muted", a.rationale),
    );
    view.refs(row, a.evidence_refs);
    assessments.append(row);
  }
  const graph = view.section("Problems, hypotheses, evidence and decisions");
  for (const node of semantic.behavior_graph.nodes) {
    graph.append(element("p", "", `${node.id} · ${node.kind}: ${node.label}`));
    view.refs(graph, node.evidence_refs);
  }
  for (const edge of semantic.behavior_graph.edges)
    graph.append(
      element(
        "p",
        "muted",
        `${edge.from} → ${edge.kind} → ${edge.to} (${edge.basis})`,
      ),
    );
  const preserve = view.section("Effective behaviors to preserve");
  for (const p of semantic.preserved_behaviors) {
    preserve.append(element("p", "", p.behavior));
    view.refs(preserve, p.evidence_refs);
  }
  const evolve = view.section(
    `Evolution hypotheses · ${proposals.length}`,
    true,
  );
  for (const p of proposals) {
    const row = element("article", "evaluation-finding");
    row.append(
      element("h3", "", `${p.surface} · ${p.state} · ${p.readiness}`),
      element("p", "", p.hypothesis),
      element(
        "p",
        "",
        `Predicted endpoint: ${p.predicted_endpoint || "unspecified"}`,
      ),
      element("p", "", `Validation: ${p.validation || "needs design"}`),
      element("p", "", `Preserve: ${p.preserve.join("; ")}`),
      element(
        "p",
        "muted",
        "Untested. Paired experiments must verify goal outcomes and preserved behaviors before comparing efficiency.",
      ),
    );
    view.refs(row, p.supporting_refs);
    evolve.append(row);
  }
}
