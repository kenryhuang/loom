import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {EvaluationView} from "../../src/loom/web/assets/views/evaluation.mjs";
import {behaviorResult, pointer} from "./fixtures/deep-evaluation.mjs";

function setup() {
  const dom = new JSDOM("<main></main>"); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), abort = new AbortController(), reads = [];
  const view = new EvaluationView(root, {signal: abort.signal, evidence: ref => reads.push(ref)});
  view.results = root; return {dom, root, abort, reads, view};
}

test("dashboard separates supported, unknown and unreviewed coverage and preserves cited findings", () => {
  const {dom, root, abort, reads, view} = setup();
  const data = behaviorResult(); data.semantic.summary += " <script>unsafe()</script>";
  view.render(data);
  const chart = root.querySelector('[aria-label^="Segment coverage:"]');
  assert.match(chart.getAttribute("aria-label"), /Supported 1, Evidence insufficient 1, Pending 1, Unselected 2/);
  assert.equal([...chart.children].reduce((sum, n) => sum + parseFloat(n.style.width), 0), 100);
  assert.equal(root.querySelectorAll(".deep-dimension meter").length, 5);
  assert.equal(root.querySelector("script"), null);
  assert.match(root.textContent, /Not tested/);
  assert.match(root.textContent, /Inspect the startup path/);
  assert.match(root.textContent, /Controlled startup resulttestsStartup delay/);
  const finding = root.querySelector('[data-detail="finding-finding-a"]');
  assert.equal(finding.open, false); finding.open = true; finding.querySelector("button").click();
  assert.deepEqual(reads, [pointer]);
  abort.abort(); dom.window.close();
});

test("assessment filters and expanded evidence survive live result refresh", () => {
  const {dom, root, abort, view} = setup();
  const data = behaviorResult(); view.render(data);
  const assessments = root.querySelector('[data-detail="assessments"]'); assessments.open = true;
  const status = root.querySelector('[data-deep-filter="status"]'); status.value = "ineffective";
  status.dispatchEvent(new dom.window.Event("change"));
  assert.equal(assessments.querySelectorAll("tbody tr").length, 1);
  const proof = root.querySelector('[data-detail="assessment-segment-a-investigation_efficiency"]'); proof.open = true;
  view.render(data); assert.equal(root.querySelector('[data-detail="assessments"]'), assessments);
  data.semantic.summary = "Updated synthesis"; view.render(data);
  assert.equal(root.querySelector('[data-detail="assessments"]').open, true);
  assert.equal(root.querySelector('[data-deep-filter="status"]').value, "ineffective");
  assert.equal(root.querySelector('[data-detail="assessment-segment-a-investigation_efficiency"]').open, true);
  assert.equal(root.querySelector('[data-detail="assessments"]').querySelectorAll("tbody tr").length, 1);
  abort.abort(); dom.window.close();
});

test("budget gauges retain overruns and unknown usage, and polling preserves the activity disclosure", () => {
  const {dom, root, abort, view} = setup(); view.progress = root;
  const job = {id: "budget", state: "budget_exhausted", analysis_version: "v3", scan_complete: true,
    selected_segments: 3, reviewed_segments: 1, settings: {max_calls: 4, max_tokens: 100, max_seconds: 60},
    usage: {calls: 3, total_tokens: 125, unreported_calls: 1}, elapsed_seconds: 70, progress: [{elapsed_seconds: 10, message: "Saved focus"}]};
  view.renderProgress(job);
  assert.equal(root.querySelectorAll(".exhausted").length, 2);
  assert.match(root.textContent, /usage unknown/);
  assert.match(root.querySelector('[aria-label^="Reported tokens:"]').getAttribute("aria-label"), /125 \/ 100/);
  assert.equal(root.querySelector('[aria-label^="Reported tokens:"] span').style.width, "100%");
  assert.equal(root.querySelector("details").open, false);
  root.querySelector("details").open = true; view.renderProgress(job);
  assert.equal(root.querySelector("details").open, true);
  assert.equal(root.querySelectorAll('button').length, 0);
  abort.abort(); dom.window.close();
});


test("repeated plan judgments share one item while retaining each rationale and citation", () => {
  const {dom, root, abort, reads, view} = setup();
  const data = behaviorResult();
  data.semantic.plan_assessments.push({...data.semantic.plan_assessments[0], status: "followed", rationale: "A later check followed the plan"});
  view.render(data);
  const plans = [...root.querySelectorAll("section")].find(s => s.querySelector("h3")?.textContent === "Plan adherence");
  assert.equal(plans.querySelectorAll("tbody tr").length, 1);
  assert.match(plans.textContent, /justified change \/ followed/);
  assert.match(plans.textContent, /New evidence warranted/);
  assert.match(plans.textContent, /A later check followed the plan/);
  const details = plans.querySelector("details"); details.open = true;
  const buttons = [...details.querySelectorAll("button")]; assert.equal(buttons.length, 2);
  buttons.forEach(button => button.click()); assert.deepEqual(reads, [pointer, pointer]);
  abort.abort(); dom.window.close();
});

test("runtime acceptance remains passed when independent verification receipts are absent", () => {
  const {dom, root, abort, view} = setup(), data = behaviorResult();
  const latest = {objective: "Check orbital speed"}; data.base.outcomes = [latest];
  latest.episode_id = "ep"; latest.status = "unverified"; latest.verification_count = 0;
  data.base.runtime_acceptance = {episode_id: "ep", line: 999, assurance: "model_judgment", state: {
    state: "passed", plan: {criteria: [{id: "speed", description: "Compare orbital speed"}]},
    results: [{criterion_id: "speed", status: "passed", reason: "Computed orbital values agree"}]}};
  view.render(data);
  const outcome = root.querySelector('[data-detail="task-outcome"]');
  assert.match(outcome.textContent, /Runtime acceptancePassed · 1\/1 checks/i);
  assert.match(outcome.textContent, /Compare orbital speed/);
  assert.match(outcome.textContent, /No independent receipts/);
  assert.doesNotMatch(outcome.textContent, /Not assessed|No goal acceptance checks/);
  data.base.runtime_acceptance.episode_id = "another-episode"; view.render(data);
  assert.match(root.querySelector('[data-detail="task-outcome"]').textContent, /Not recorded for this goal/);
  abort.abort(); dom.window.close();
});

test("historical unknown judgments expose missing answers and rejected citations", () => {
  const {dom, root, abort, view} = setup(), data = behaviorResult();
  data.semantic.assessments = [
    {segment_id: "segment-a", dimension: "intent_alignment", status: "unknown", rationale: "Not assessed", evidence_refs: []},
    {segment_id: "segment-a", dimension: "plan_quality", status: "unknown", rationale: "Evidence exceeds delivered source ranges", evidence_refs: []},
    {segment_id: "segment-a", dimension: "progress_effectiveness", status: "unknown", rationale: "Evidence exceeds delivered source ranges", evidence_refs: [pointer]},
  ];
  view.render(data);
  const reasons = root.querySelector('[data-detail="unknown-reasons"]');
  assert.match(reasons.textContent, /Evaluator omitted judgment1/);
  assert.match(reasons.textContent, /No citations1/);
  assert.match(reasons.textContent, /Citation outside read range1/);
  abort.abort(); dom.window.close();
});
