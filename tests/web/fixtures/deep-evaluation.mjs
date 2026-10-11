export const pointer = {line_number: 14, field_path: "output.content", start: 4, end: 20};
export function behaviorResult() {
  const dimensions = Object.fromEntries(["intent_alignment", "plan_quality", "progress_effectiveness", "investigation_efficiency", "adaptation_recovery"]
    .map(d => [d, {attempted: 2, supported: 1, unknown: 1, pending: 1, unselected: 2}]));
  return {
    schema_version: "loom.evaluation.bundle.v3", base: {outcomes: []},
    facts: {goal_revisions: [], segments: [{id: "segment-a", boundary_key: ["run", "trace", 3]}],
      plan_revisions: [{id: "plan-a", nodes: [{id: "inspect", description: "Inspect the startup path"}]}]},
    proposals: [{id: "proposal-a", surface: "planner", state: "proposed", readiness: "needs_validation", hypothesis: "Test the blocking hypothesis before repeating inspection.",
      predicted_endpoint: "Time to supported cause", validation: "Paired startup cases", preserve: ["Check the final startup behavior"], supporting_refs: [pointer]}],
    semantic: {coverage: {status: "incomplete", source_segments: 5, attempted_segments: 2, supported_segments: 1,
      pending_segments: ["pending"], unselected_segments: ["other1", "other2"], dimensions,
      limitations: ["Sampled observations only"], selection_budget: {budget_limited: true, selected_segments: 3, requested_max_segments: 8}},
      summary: "Repeated inspection delayed a decisive experiment. A controlled startup test ruled out the network.",
      diagnoses: [{id: "finding-a", segment_id: "segment-a", dimension: "investigation_efficiency", epistemic_status: "inferred",
        observation: "The same startup file was read before testing the suspected blocker.", mechanism: "Inspection repeated without narrowing the hypothesis.",
        pattern_type: "repeated_unchanged_result", alternatives: ["Context may have been truncated"], supporting_refs: [pointer], counterevidence_refs: []}],
      assessments: [{segment_id: "segment-a", dimension: "investigation_efficiency", status: "ineffective", rationale: "No new evidence between reads", evidence_refs: [pointer]},
        {segment_id: "segment-a", dimension: "plan_quality", status: "unknown", rationale: "Insufficient evidence", evidence_refs: []}],
      progress: [{id: "progress-a", step_number: 3, kind: "hypothesis_eliminated", label: "A controlled startup test excluded network delay.",
        since_previous_progress: {status: "measured", tool_calls: 3, model_calls: 2, known_tokens: 1500, elapsed_ms: 22000, unmeasured_model_calls: 0}, evidence_refs: [pointer]}],
      plan_assessments: [{segment_id: "segment-a", plan_snapshot_id: "plan-a", node_id: "inspect", status: "justified_change", rationale: "New evidence warranted a startup experiment", evidence_refs: [pointer]}],
      efficiency: {cost_to_discriminative_evidence: {status: "measured", known_tokens: 1500, model_calls: 2, tool_calls: 3, elapsed_ms: 22000, basis: "Unique calls; elapsed includes waits",
        observations: [{subject_id: "problem", start_line: 2, end_line: 14, known_tokens: 1500, model_calls: 2, tool_calls: 3, evidence_refs: [pointer]}]},
        causal_recovery_cost: {status: "unknown", basis: "No validated recovery endpoint"}},
      behavior_graph: {nodes: [{id: "problem", kind: "problem", label: "Startup delay", evidence_refs: [pointer]},
        {id: "test", kind: "evidence", label: "Controlled startup result", evidence_refs: [pointer]}],
        edges: [{from: "test", to: "problem", kind: "tests", basis: "inferred"}]},
      preserved_behaviors: [{behavior: "Keep the controlled experiment", evidence_refs: [pointer]}]},
  };
}
