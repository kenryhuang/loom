import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { renderBehaviorSequence, renderProgressTimeline } from "../../src/loom/web/assets/views/behavior-sequence.mjs";

test("behavior facts show plan text, repetition caveats and source links without claiming progress", () => {
  const dom = new JSDOM("<main></main>");
  globalThis.document = dom.window.document;
  const root = document.querySelector("main"), reads = [], ref = { line_number: 4 };
  renderBehaviorSequence(root, {
    plan_execution: [{description: "Find the blocking statement", declared_status: "completed", round_ids: ["r"], tool_ids: ["t"],
      steps: [{step_number: 3}], revisions: ["p"], evidence_refs: [ref]}],
    repetitions: [{path: "<script>evil()</script>", first_line: 4, line: 9, status: "needs_context", basis: "Same content",
      limitations: ["Only a truncated range was returned"], evidence_refs: [ref]}],
    signals: [{kind: "resource_observed", line: 4, step_number: 3, label: "a.txt", evidence_refs: [ref]}],
    candidate_windows: [{pattern_type: "low_progress_window", tool_calls: 10, basis: "No recognizable new result", evidence_refs: [ref]}],
  }, {evidence: r => reads.push(r)});
  assert.match(root.textContent, /Find the blocking statement/);
  assert.match(root.textContent, /truncated range/);
  assert.match(root.textContent, /not judgments/);
  assert.match(root.textContent, /10 tool calls/);
  assert.equal(root.querySelector("script"), null);
  root.querySelector("button").click();
  assert.deepEqual(reads, [ref]);
});

test("semantic landmarks show measured interval cost, plan judgments, and incomplete coverage", () => {
  const dom = new JSDOM("<main></main>");
  globalThis.document = dom.window.document;
  const root = document.querySelector("main");
  renderProgressTimeline(root, {
    progress: [{kind: "hypothesis_eliminated", label: "Network excluded by controlled test", step_number: 4,
      since_previous_progress: {status: "partial", tool_calls: 3, model_calls: 2, known_tokens: 30, unmeasured_model_calls: 1, elapsed_ms: 5000}}],
    plan_assessments: [{plan_snapshot_id: "p", node_id: "n", status: "justified_change", rationale: "New evidence changed the investigation"}],
  }, {plan_revisions: [{id: "p", nodes: [{id: "n", description: "Investigate startup"}]}]});
  assert.deepEqual([...root.querySelectorAll("table tbody tr")[0].cells].map(cell => cell.textContent), ["3", "2", "30", "5 s", "1"]);
  assert.match(root.textContent, /includes waits/);
  assert.match(root.textContent, /unreviewed progress/);
  assert.match(root.textContent, /Investigate startup/);
  assert.match(root.textContent, /justified change/);
  root.replaceChildren();
  renderBehaviorSequence(root, undefined);
  renderProgressTimeline(root, {}, {});
  assert.match(root.textContent, /No cited progress landmarks/);
});

test("computed coverage, milestones and repeats render independently of deep evaluation", () => {
  const dom = new JSDOM("<main></main>"); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), reads = [], ref = {line_number: 20};
  renderBehaviorSequence(root, {
    plan_execution: [], repetitions: [{path: "shell_execute output", kind: "same_tool_result", first_line: 5, line: 20,
      status: "needs_context", basis: "Identical actual output", evidence_refs: [ref],
      cost: {known_tool_ms: 1200, associated_known_tokens: 30}}],
    observation_coverage: {solver_tool_calls: 4, completed_tool_calls: 4, comparable_result_calls: 4, resource_identity_calls: 0},
    repetition_cost: {tool_calls: 1, known_tool_ms: 1200, unmeasured_tool_calls: 0, associated_known_tokens: 30},
    observations: [{id: "verified", kind: "verification_result", line: 20, label: "Acceptance passed", status: "passed",
      assurance: "model_judgment", goal_binding: "matched", evidence_refs: [ref],
      since_previous_observation: {tool_calls: 1, known_tokens: 15}}],
    milestones: [{kind: "first_acceptance_pass", observation_id: "verified", line: 20,
      cost: {tool_calls: 4, model_calls: 4, known_tokens: 60, elapsed_ms: 5000}}],
    plan_bindings: [{node_id: "verify", status: "bound", unmet_dependencies: ["inspect"], evidence_refs: [ref]}],
    plan_summary: {bound: 1, dependency_violations: 1, calls_after_declared_completion: 0, nodes_without_recorded_calls: 1},
  }, {evidence: r => reads.push(r)});
  assert.match(root.textContent, /4 \/ 4/); assert.match(root.textContent, /0 \/ 4/);
  assert.match(root.textContent, /Recorded milestones/); assert.match(root.textContent, /5 s/);
  assert.match(root.textContent, /model judgment · matched/);
  assert.match(root.textContent, /Dependencies not recorded complete1/);
  assert.match(root.textContent, /Identical returned result/);
  root.querySelector("button").click(); assert.deepEqual(reads, [ref]); dom.window.close();
});

test("deep judgments link to deterministic facts and preserve unsupported status", () => {
  const dom = new JSDOM("<main></main>"); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), reads = [], ref = {line_number: 20};
  renderProgressTimeline(root, {fact_assessments: [{fact_id: "repeat", status: "unknown", rationale: "Earlier endpoint not read", evidence_refs: [ref]}]},
    {sequence: {observations: [{id: "repeat", kind: "same_tool_result", label: "shell_execute output"}]}}, {evidence: r => reads.push(r)});
  assert.match(root.textContent, /Identical returned result · shell_execute output/);
  assert.match(root.textContent, /Earlier endpoint not read/);
  assert.match(root.textContent, /unknown/);
  root.querySelector("button").click(); assert.deepEqual(reads, [ref]); dom.window.close();
});
