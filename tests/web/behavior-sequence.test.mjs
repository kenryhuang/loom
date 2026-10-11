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
  assert.match(root.textContent, /3 tools · 2 model calls · 30 known tokens/);
  assert.match(root.textContent, /includes waits/);
  assert.match(root.textContent, /unreviewed progress/);
  assert.match(root.textContent, /Investigate startup/);
  assert.match(root.textContent, /justified change/);
  root.replaceChildren();
  renderBehaviorSequence(root, undefined);
  renderProgressTimeline(root, {}, {});
  assert.match(root.textContent, /No cited progress landmarks/);
});
