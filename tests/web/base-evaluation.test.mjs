import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {renderBaseStatistics} from "../../src/loom/web/assets/views/base-evaluation.mjs";

const duration = {measured: 2, missing: 1, total_ms: 8000, average_ms: 4000, p95_ms: 6000, max_ms: 6000};
const tokens = {known: 15, measured_calls: 2, missing_calls: 1, average: 7.5, max: 10};
const counts = {calls: 3, success: 1, failed: 1, cancelled: 0, incomplete: 1, unknown: 0, success_rate: .5, duration,
  failure_reasons: {timeout: 1}, truncated_outputs: 1, explicit_retries: 0, repeated_inputs: 1,
  prompt_tokens: tokens, completion_tokens: {...tokens, known: 5}, total_tokens: {...tokens, known: 20}};
function statistics() {
  return {scope: "source_snapshot", execution_status: "completed", basic: {runs: 1, run_attempts: 2, steps: 1, step_attempts: 2,
    goal_changes: 0, events: {raw: 100, analyzed: 10, by_type: {"llm.requested": 3}}, step_states: {completed: 1},
    acceptance: {state: {state: "passed", reason: "All checks passed", attempts: 1,
      plan: {criteria: [{id: "doc", description: "Analysis document exists", verifier: "artifact"}]},
      results: [{criterion_id: "doc", status: "passed", reason: "File found", artifact: {sha256: "proof"}}]}}},
    tools: {...counts, by_tool: [{...counts, name: "shell_execute"}], by_family: [{...counts, name: "command"}], by_role: [{...counts, name: "solver"}]},
    models: {...counts, by_model: [{...counts, name: "model"}], by_role: [{...counts, name: "verification"}], by_stage: [{...counts, name: "verify"}]},
    timing: {wall_ms: 20000, session_span_ms: 20000, states_ms: {running: 10000, paused: 10000, queued: 0, waiting_input: 0},
      running_activity_ms: {model_only: 4000, tool_only: 3000, overlap: 2000, unattributed: 1000}, intervals: [],
      limitations: ["Call durations can overlap."], missing_timestamp_events: 1, clock_reversals: 0},
    quality: {success_rate_basis: "success / (success + failed)", token_basis: "provider_reported", retry_basis: "Explicit retries only",
      missing_usage_calls: 1, missing_call_durations: 1, missing_artifacts: [], unscoped_call_events: 0, duplicate_records: 0, usage_statuses: {partial: 1}, unavailable: ["time to first token"]},
    calls: {tools: [{tool: "shell_execute", status: "failed", failure_reason: "timeout", duration_ms: 6000, role: "solver", seq: 5, evidence_refs: [{line_number: 5}]}]},
    outputs: {workspace_files: ["report.md"], artifacts: [{kind: "report", relative_path: "report.json", sha256: "report"}]}};
}

test("base evaluation renders five factual sections, timing overlap, costs and evidence links", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), opened = [], refs = [];
  const s = statistics();
  assert.ok(renderBaseStatistics(root, {statistics: s}, {artifact: id => opened.push(id), evidence: ref => refs.push(ref)}));
  assert.equal(root.querySelectorAll("h3").length, 5);
  assert.match(root.textContent, /Raw events100/);
  assert.match(root.textContent, /Analyzed events10/);
  assert.match(root.textContent, /Acceptance: passed · 1\/1/);
  assert.match(root.textContent, /Analysis document exists/);
  assert.doesNotMatch(root.textContent, /unverified|goal coverage: unknown/);
  const bars = [...root.querySelectorAll("progress")];
  assert.equal(bars.reduce((sum, bar) => sum + bar.value, 0), 10000);
  assert.equal(bars.find(b => b.getAttribute("aria-label") === "overlap").value, 2000);
  [...root.querySelectorAll("button")].find(b => b.textContent === "View evidence").click();
  [...root.querySelectorAll("button")].find(b => b.textContent === "Event 5").click();
  assert.deepEqual(opened, ["proof"]); assert.deepEqual(refs, [{line_number: 5}]);
  const grouping = root.querySelector('[aria-label="Group tool statistics"]');
  grouping.value = "by_family"; grouping.dispatchEvent(new dom.window.Event("change"));
  assert.match(root.textContent, /command/);
  dom.window.close();
});

test("scope switching uses backend aggregates and unavailable measurements never become invented zero rates", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), s = statistics(), other = statistics();
  other.scope = "run"; other.basic.events.raw = 7; other.basic.events.analyzed = 5; other.basic.acceptance = null;
  other.tools.by_tool = [{...counts, name: "custom <script>bad()</script>", success_rate: null, duration: {...duration, average_ms: null}}];
  s.by_run = {other};
  renderBaseStatistics(root, {statistics: s});
  const scope = root.querySelector('[aria-label="Base evaluation scope"]');
  scope.value = "other"; scope.dispatchEvent(new dom.window.Event("change"));
  assert.match(root.textContent, /Raw events7/);
  assert.match(root.textContent, /selected run/);
  assert.match(root.textContent, /no checks recorded/);
  assert.equal(root.querySelector("script"), null);
  assert.match(root.textContent, /—/);
  dom.window.close();
});
