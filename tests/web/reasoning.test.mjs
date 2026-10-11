import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {ReasoningView, processGroups, boundedGraph, groupReasoningCycles} from "../../src/loom/web/assets/views/reasoning.mjs";

export function fixture() {
  const scoped = {episode_id: "ep", goal_revision_id: "goal", role: "solver"};
  const cost = {reads: 2, read_successes: 2, mixed_calls: 0, unknown_calls: 1, model_calls: 3, tool_calls: 3,
    total_tokens: 150, missing_total_tokens: 1, prompt_tokens: 100, completion_tokens: 50, tool_duration_ms: 120,
    missing_tool_duration: 1, wall_ms: 1300};
  const metric = {mean: 2, n: 1, median: 2, p90: 2, partial_windows: 1, open_windows: 1, open_reads: 0};
  const claim = {...scoped, id: "claim", kind: "claim", label: "Configuration explains the failure <script>bad()</script>",
    line: 8, status: "supported", question_id: "goal", observation_ids: ["tool"], action_ids: [],
    evidence_refs: [{line_number: 8, field_path: "response.content"}], basis: "inferred"};
  const result = {schema_version: "loom.reasoning-process.v1", scopes: [{id: "goal", ...scoped, label: "Investigate", run_id: "run", costs_by_role: {solver: cost}}],
    questions: [{id: "goal", ...scoped, label: "Investigate", endpoint: {kind: "cause", status: "inferred", cost},
      cycles: {closed: 1, open: 0}, adoption_lags: []}], occurrences: [claim],
    metrics: {goal: {evidence: metric, judgment: metric}},
    coverage: {reviewed_segments: 1, source_segments: 3, status: "incomplete"},
    nodes: [{...scoped, id: "tool:request", kind: "collection", line: 3, tool_call_id: "tool", round_id: "r1", classification: "read",
      label: "read_file: config.json", status: "success", evidence_refs: [{line_number: 3}]},
    {...scoped, id: "tool:result", kind: "observation", line: 4, tool_call_id: "tool", round_id: "r1", classification: "read",
      label: "read_file: config.json", status: "success", evidence_refs: [{line_number: 4}]}, claim],
    relations: [{source: "tool:result", target: "claim", kind: "supports", basis: "inferred"}],
    windows: [{scope_id: "goal", series: "evidence", start_line: 1, end_line: 8, status: "closed", cost},
      {scope_id: "goal", series: "evidence", start_line: 8, end_line: 9, status: "open", cost: {...cost, reads: 0}}],
    validation_diagnostics: []};
  result.graph = {nodes: result.nodes.map(n => ({...n, phase: n.kind === "claim" ? "conclusion" : n.kind,
    column: n.kind === "claim" ? 0 : n.kind === "collection" ? 1 : 2, question_ids: ["goal"]})), relations: result.relations};
  return result;
}

test("reasoning page renders graph, partial metrics and safe clickable source evidence", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), refs = [], actions = [];
  const view = new ReasoningView(root, {baseProcess: fixture(), evidence: r => refs.push(r),
    onEvaluate: () => actions.push("deep"), onRefresh: () => actions.push("refresh")});
  assert.match(root.textContent, /1 \/ 3 segments reviewed/);
  assert.match(root.textContent, /Known reads \/ evidence/);
  assert.match(root.textContent, /1 closed windows/);
  assert.equal(root.querySelectorAll(".reasoning-node").length, 3);
  assert.equal(root.querySelectorAll(".reasoning-edge.inferred").length, 1);
  assert.equal(root.querySelector("script"), null);
  root.querySelectorAll(".reasoning-node")[2].click();
  assert.match(root.querySelector(".reasoning-selection").textContent, /Configuration explains the failure/);
  [...root.querySelectorAll("button")].find(n => n.textContent.startsWith("Evidence 8")).click();
  assert.deepEqual(refs, [{line_number: 8, field_path: "response.content"}]);
  [...root.querySelectorAll("button")].find(n => n.textContent === "Review in Deep evaluation").click();
  assert.deepEqual(actions, ["deep"]);
  const role = root.querySelector('[aria-label="Highlight role"]'); role.value = "verification";
  role.dispatchEvent(new dom.window.Event("change"));
  assert.equal(root.querySelectorAll(".reasoning-node").length, 3);
  assert.equal(root.querySelectorAll(".reasoning-context-node").length, 3);
  assert.match(root.textContent, /Known reads \/ evidence/); // semantic cards retain explicit solver/goal scope
  view.update({semantic: {}}, {id: "historical"});
  assert.match(root.textContent, /Historical evaluation · process not assessed/);
  dom.window.close();
});

test("grouping keeps parallel physical calls distinct in counts and both ends navigable", () => {
  const p = fixture();
  p.nodes.push({...p.nodes[0], id: "tool2:request", tool_call_id: "tool2", line: 5});
  const {rows, owners} = processGroups(p.nodes);
  assert.equal(rows[0].callIds.size, 1);
  assert.equal(rows[0].members.length, 1);
  assert.notEqual(owners.get("tool:result"), owners.get("tool2:request"));
  assert.equal(rows[0].label, "read_file: config.json");
});

test("base-only process and historical snapshots do not fabricate semantic endpoints", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main");
  const view = new ReasoningView(root, {evidence() {}, onEvaluate() {}, onRefresh() {}});
  assert.match(root.textContent, /Refresh analysis/);
  const p = fixture(); p.coverage = {reviewed_segments: 0, source_segments: 3, status: "not_evaluated"};
  p.questions[0].endpoint = {kind: "conclusion", status: "not_reviewed", cost: null};
  p.metrics.goal.evidence = {mean: null, n: 0, open_windows: 1};
  view.baseProcess = p; view.update(null);
  assert.match(root.textContent, /0 \/ 3 segments reviewed/);
  assert.match(root.textContent, /Not measured/);
  assert.match(root.textContent, /Awaiting Deep evaluation/);
  dom.window.close();
});

test("rejected semantic links are visible and event adjacency never becomes an arrow", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), p = fixture();
  p.occurrences = []; p.graph.relations = [];
  p.validation_diagnostics = [{field: "reasoning_transitions[0]", reason: "Future result"}];
  const view = new ReasoningView(root, {baseProcess: p, evidence() {}, onEvaluate() {}, onRefresh() {}});
  view.update({semantic: {reasoning_process: p}}, {state: "budget_exhausted"});
  assert.match(root.textContent, /All proposed reasoning links were rejected/);
  assert.match(root.textContent, /Budget exhausted/i);
  assert.equal(root.querySelectorAll(".reasoning-edge").length, 0);
  assert.match(root.querySelector(".reasoning-question").textContent, /Investigate/);
  assert.match(root.textContent, /Exact delivery → adoption link unavailable/);
  assert.doesNotMatch(root.textContent, /next in time/);
  dom.window.close();
});

test("complete flow keeps the final outcome and connections when phases are collapsed", () => {
  const nodes = Array.from({length: 10}, (_, i) => ({id: `n${i}`, line: i, role: "solver", kind: "action", phase: "action", column: 1}));
  nodes[0].kind = "question"; nodes[9] = {...nodes[9], kind: "terminal", is_endpoint: true, terminal: true};
  const edges = nodes.slice(1).map((n, i) => ({source: nodes[i].id, target: n.id, kind: "continues", layer: "flow"}));
  const limited = boundedGraph(nodes, edges, 5);
  assert.equal(limited.nodes.at(-1).id, "n9");
  assert.equal(limited.nodes.filter(n => n.kind === "omitted").length, 1);
  const seen = new Set([limited.nodes[0].id]);
  for (let i = 0; i < limited.nodes.length; i++) for (const r of limited.relations) if (seen.has(r.source)) seen.add(r.target);
  assert.equal(seen.size, limited.nodes.length);
  assert.match(limited.nodes.find(n => n.kind === "omitted").label, /6 phases collapsed/);
});

test("outcome stays visible and role highlighting never breaks the complete graph", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), p = fixture(), scoped = {episode_id: "ep", goal_revision_id: "goal", role: "process"};
  p.graph.nodes.unshift({...scoped, id: "start", kind: "question", phase: "question", line: 1, label: "Investigate", column: 1});
  p.graph.nodes.push({...scoped, id: "end", kind: "terminal", phase: "outcome", line: 10, label: "Task completed · report formed",
    is_endpoint: true, terminal: true, report_formed: true, column: 1, status: "completed"});
  p.graph.relations.push({source: "start", target: "tool:request", kind: "continues", layer: "flow"},
    {source: "claim", target: "end", kind: "ends", layer: "flow"});
  new ReasoningView(root, {baseProcess: p, evidence() {}, onEvaluate() {}, onRefresh() {}});
  assert.match(root.querySelector(".reasoning-outcome-summary").textContent, /Task completed · report formed/);
  assert.equal(root.querySelectorAll('.reasoning-node[data-outcome="true"]').length, 1);
  root.querySelector('.reasoning-node[data-outcome="true"]').click();
  assert.match(root.querySelector('.reasoning-selection').textContent, /TerminalYes/);
  const role = root.querySelector('[aria-label="Highlight role"]'); role.value = "verification";
  role.dispatchEvent(new dom.window.Event("change"));
  assert.equal(root.querySelectorAll(".reasoning-node").length, 5);
  assert.equal(root.querySelectorAll('.reasoning-outcome.reasoning-context-node').length, 0);
  dom.window.close();
});

function roundFixture() {
  const p = fixture(), scope = {episode_id: 'ep', goal_revision_id: 'goal', role: 'solver', question_ids: ['goal']};
  const rounds = Array.from({length: 3}, (_, i) => {
    const round = {...scope, round_id: `round-${i}`}, call = `call-${i}`;
    return [
      {...round, id: `judgment-${i}`, kind: 'model_response', phase: 'judgment', column: 0, line: i * 3 + 2, label: `Decision ${i}`, evidence_refs: [{line_number: i * 3 + 2}]},
      {...round, id: `action-${i}`, kind: 'collection', phase: 'collect', column: 1, line: i * 3 + 3, label: `Read ${i}`, tool_call_id: call},
      {...round, id: `result-${i}`, kind: 'observation', phase: 'observation', column: 2, line: i * 3 + 4, label: `Result ${i}`, tool_call_id: call},
    ];
  }).flat();
  p.graph.nodes = [{...scope, id: 'start', kind: 'question', phase: 'question', column: 1, line: 1, label: 'Investigate'},
    ...rounds, {...scope, id: 'end', kind: 'terminal', phase: 'outcome', column: 1, line: 12, label: 'Task completed', is_endpoint: true, terminal: true}];
  p.graph.relations = p.graph.nodes.slice(1).map((n, i) => ({source: p.graph.nodes[i].id, target: n.id, kind: 'continues', layer: 'flow', basis: 'recorded'}));
  return p;
}

test('continuous complete rounds collapse with boundary edges and expand losslessly', () => {
  const p = roundFixture(), original = structuredClone(p.graph);
  const compact = groupReasoningCycles(p.graph.nodes, p.graph.relations);
  assert.equal(compact.nodes.length, 3);
  const block = compact.nodes[1];
  assert.equal(block.round_label, 'Rounds 1–3');
  assert.equal(block.round_count, 3);
  assert.equal(block.call_count, 3);
  assert.equal(block.members.length, 9);
  assert.deepEqual(compact.relations.map(r => [r.source, r.target]), [['start', block.id], [block.id, 'end']]);
  const open = groupReasoningCycles(p.graph.nodes, p.graph.relations, new Set([block.id]));
  assert.deepEqual(open.nodes.map(n => n.id), p.graph.nodes.map(n => n.id));
  assert.deepEqual(open.relations, p.graph.relations);
  assert.deepEqual(open.nodes.filter(n => n.column === 0).map(n => n.round_number), [1, 2, 3]);
  assert.deepEqual(p.graph, original);
});

test('parallel calls count once each and semantic links survive across folded boundaries', () => {
  const p = roundFixture(), extra = p.graph.nodes.slice(2, 4).map(n => ({...n, id: n.id + '-parallel', tool_call_id: 'parallel'}));
  p.graph.nodes.splice(4, 0, ...extra);
  p.graph.relations.push({source: 'result-0', target: 'end', kind: 'supports', layer: 'reasoning', basis: 'inferred'});
  const compact = groupReasoningCycles(p.graph.nodes, p.graph.relations), block = compact.nodes[1];
  assert.equal(block.round_count, 3); assert.equal(block.call_count, 4);
  assert.ok(compact.relations.some(r => r.source === block.id && r.target === 'end' && r.kind === 'supports' && r.layer === 'reasoning'));
  const open = groupReasoningCycles(p.graph.nodes, p.graph.relations, new Set([block.id]));
  assert.equal(open.nodes.length, p.graph.nodes.length);
});

test('verification, rejection, delivery and unfinished rounds are folding boundaries', () => {
  for (const barrier of [
    {kind: 'check', phase: 'verification'}, {kind: 'observation', phase: 'rejection'}, {kind: 'action', phase: 'delivery'},
  ]) {
    const p = roundFixture(); Object.assign(p.graph.nodes[5], barrier);
    assert.equal(groupReasoningCycles(p.graph.nodes, p.graph.relations).blocks.length, 0);
  }
  const incomplete = roundFixture(); incomplete.graph.nodes.splice(6, 1);
  assert.equal(groupReasoningCycles(incomplete.graph.nodes, incomplete.graph.relations).blocks.length, 0);
  const revised = roundFixture(); revised.graph.nodes.slice(4, 7).forEach(n => n.goal_revision_id = 'new-goal');
  assert.equal(groupReasoningCycles(revised.graph.nodes, revised.graph.relations).blocks.length, 0);
});

test('cycle block expands inline, exposes source details and collapses while retaining the outcome', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector('main'), refs = [];
  new ReasoningView(root, {baseProcess: roundFixture(), evidence: r => refs.push(r), onEvaluate() {}, onRefresh() {}});
  assert.equal(root.querySelectorAll('.reasoning-node').length, 3);
  const block = root.querySelector('.reasoning-cycle-block');
  assert.match(block.textContent, /Rounds 1–3 · 3 rounds/);
  assert.equal(block.getAttribute('aria-expanded'), 'false'); block.click();
  assert.equal(root.querySelectorAll('.reasoning-node').length, 11);
  assert.equal(root.querySelectorAll('.reasoning-cycle-frame').length, 1);
  assert.equal(root.querySelector('.reasoning-cycle-toggle').getAttribute('aria-expanded'), 'true');
  const judgment = [...root.querySelectorAll('.reasoning-node')].find(n => n.textContent.includes('Decision 0'));
  assert.match(judgment.textContent, /Round 1/); judgment.click();
  [...root.querySelectorAll('button')].find(n => n.textContent === 'Evidence 2').click();
  assert.deepEqual(refs, [{line_number: 2}]);
  root.querySelector('.reasoning-cycle-toggle').click();
  assert.equal(root.querySelectorAll('.reasoning-node').length, 3);
  assert.ok(root.querySelector('[data-outcome="true"]'));
  dom.window.close();
});
