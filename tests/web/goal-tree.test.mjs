import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {goalTree, filterGoalTree, goalEntries, callSummary} from '../../src/loom/web/assets/views/goal-tree.mjs';
import {IncrementalGraphView, durationLabel} from '../../src/loom/web/assets/views/incremental-graph.mjs';

const fixture = () => JSON.parse(readFileSync(new URL('./fixtures/incremental-graph.json', import.meta.url)));
const open = (node, dom) => {node.open = true; node.dispatchEvent(new dom.window.Event('toggle'));};
const named = (root, title) => [...root.querySelectorAll('details')].find(d => d.querySelector('summary').textContent === title);

test('goal nesting exposes its basis separately from chronological spans, including old reports', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), parent = data.goals[1], child = {...parent, id: 'nested', parent_goal_id: parent.id,
    objective: 'Resolve one component', root_relation: 'Resolve the update ordering responsible for stale state'};
  data.goals.push(child);
  const stage = {...data.stages[0], id: 'nested-stage', goal_id: child.id};
  data.stages.push(stage); data.record_assessments[2].stage_ids = [stage.id];
  const root = document.querySelector('main'), view = new IncrementalGraphView(root);
  view.update(data);
  let branch = root.querySelector('[data-goal-id="nested"]');
  assert.doesNotMatch(branch.querySelector('summary').textContent, /Nesting unreviewed/);
  assert.match(branch.querySelector('summary').textContent, /L\d+–\d+/);
  open(named(branch, 'Goal details'), dom);
  assert.match(branch.textContent, /Resolve the update ordering.*Inferred from cited intent/s);
  delete child.root_relation;
  view.update(data, 'legacy');
  branch = root.querySelector('[data-goal-id="nested"]');
  assert.match(branch.querySelector('summary').textContent, /Nesting unreviewed/);
  assert.equal(view.tree.byGoal.get('nested').parent_goal_id, parent.id);
  dom.window.close();
});

test('stable stages merge across nonadjacent occurrences without changing paid judgments or costs', () => {
  const data = fixture(), original = JSON.stringify(data), tree = goalTree(data);
  const goal = tree.goals.find(g => g.parent_goal_id);
  const assessed = goal.stages.find(s => s.basis === 'assessed_stage');
  assert.equal(assessed.actions.length, 3);
  assert.equal(assessed.first_line, 1);
  assert.equal(assessed.last_line, 8);
  assert.deepEqual(assessed.visits.map(v => [v.first_line, v.last_line]), [[1, 2], [5, 8]]);
  const inferred = goal.stages.find(s => s.basis === 'default_rule');
  assert.equal(inferred.actions.length, 1);
  assert.equal(inferred.actions[0].assessment.status, 'unknown');
  assert.equal(inferred.actions[0].attribution.card_id, data.extraction.cards[0].id);
  assert.equal(tree.roots[0].cost.calls, 4);
  assert.equal(tree.roots[0].cost.model, 2);
  assert.equal(tree.roots[0].cost.tool, 2);
  assert.equal(JSON.stringify(data), original);
});

test('shared actions count once per branch; selected goals retain ancestors and descendants, not siblings', () => {
  const data = fixture(), root = data.goals[0], g1 = data.goals[1];
  const g2 = {...g1, id: 'g2', objective: 'Second result'};
  const g3 = {...g1, id: 'g3', parent_goal_id: g1.id, objective: 'Nested result'};
  data.goals.push(g2, g3);
  data.stages.push({...data.stages[0], id: 's2', goal_id: g2.id}, {...data.stages[0], id: 's3', goal_id: g3.id});
  data.record_assessments[0].stage_ids.push('s2');
  data.record_assessments[2].stage_ids = ['s3'];
  data.cost_ledger.atoms.forEach(a => {a.duration_ms = 100;});
  const tree = goalTree(data);
  assert.equal(tree.byGoal.get(g2.id).stages[0].actions[0].shared, true);
  assert.equal(tree.byGoal.get(root.id).cost.duration, 400);
  assert.equal(tree.byGoal.get(g1.id).cost.calls, 3);
  assert.equal(tree.byGoal.get(g2.id).cost.calls, 1);
  const filtered = filterGoalTree(tree, g1.id);
  assert.equal(filtered[0].id, root.id);
  assert.deepEqual(filtered[0].children.map(g => g.id), [g1.id]);
  assert.equal(filtered[0].children[0].children[0].id, g3.id);
  const unresolved = filterGoalTree(tree, 'unassigned');
  assert.equal(unresolved[0].cost.calls, 1);
  assert.ok(unresolved[0].stages.every(s => s.actions.every(a => !a.assessment.stage_ids.length)));
});

test('root requirements are properties, verification stays on root and explicit unrelated blocks inheritance', () => {
  const data = fixture(), root = data.goals[0], stage = data.stages[0];
  const requirement = {id: 'contract', kind: 'boundary', name: 'acceptance.plan.proposed', start_line: 0, end_line: 0,
    call_ids: [], refs: [], raw_refs: [], features: {}, excerpts: []};
  const receipt = {...requirement, id: 'receipt', name: 'verification.completed', start_line: 9, end_line: 9};
  data.extraction.cards.push(requirement, receipt);
  data.record_assessments.push({card_id: 'receipt', status: 'reviewed', stage_ids: [stage.id]});
  data.record_assessments[1].relation = 'unrelated';
  const tree = goalTree(data), projected = tree.byGoal.get(root.id);
  assert.equal(projected.requirements.length, 1);
  assert.equal(projected.stages.find(s => s.basis === 'framework_rule').actions[0].root_override, true);
  assert.equal(projected.stages.find(s => s.basis === 'default_rule').actions[0].card.id, data.extraction.cards[1].id);
  assert.equal(projected.stages.flatMap(s => s.card_ids).includes('contract'), false);
  assert.equal(projected.cost.calls, 4);
});

test('missing timing differs from zero and partial durations; repeated atom IDs never add cost', () => {
  const atoms = new Map([['a', {kind: 'model', duration_ms: 0}], ['b', {kind: 'tool', duration_ms: null}],
    ['c', {kind: 'tool', duration_ms: 1500}]]);
  assert.equal(durationLabel(callSummary([], atoms)), '—');
  assert.equal(durationLabel(callSummary(['b'], atoms)), 'Unknown (1 call)');
  assert.equal(durationLabel(callSummary(['a'], atoms)), '0 ms');
  assert.equal(durationLabel(callSummary(['a', 'b', 'c', 'c'], atoms)), '1.5 s + 1 unknown');
  assert.equal(callSummary(['a', 'b', 'c', 'c'], atoms).calls, 3);
});

test('time bars compare each level with its full parent and retain that scale when filtered', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), goal = data.goals[1];
  data.cost_ledger.atoms.forEach(a => {a.duration_ms = 1000;});
  data.goals.push({...goal, id: 'nested', parent_goal_id: goal.id, objective: 'Nested work'});
  data.stages.push({...data.stages[0], id: 'nested-stage', goal_id: 'nested'});
  data.record_assessments[2].stage_ids = ['nested-stage'];
  // Repeated references must not inflate either the duration or the percentage.
  data.extraction.cards[0].call_ids.push(data.extraction.cards[0].call_ids[0]);
  const root = document.querySelector('main'), view = new IncrementalGraphView(root); view.update(data);
  const summary = node => node.querySelector(':scope > summary');
  const width = node => summary(node).querySelector('.goal-tree-time-fill').style.width;
  assert.equal(width(root.querySelector('.goal-tree-goal')), '100%');
  const nested = root.querySelector('[data-goal-id="nested"]');
  assert.equal(width(nested), '25%');
  assert.match(summary(nested).textContent, /25% of G1/);
  const stage = root.querySelector(`[data-stage-id="${data.stages[0].id}"]`);
  assert.equal(width(stage), '50%');
  open(stage, dom);
  assert.equal(width(stage.querySelector('.goal-tree-action')), '50%');
  assert.match(summary(stage.querySelector('.goal-tree-action')).textContent, /1 s/);
  view.filter = 'unassigned'; view.render();
  const filteredStage = root.querySelector('.goal-tree-stage');
  assert.equal(width(filteredStage), '25%');
  open(filteredStage, dom);
  assert.equal(width(filteredStage.querySelector('.goal-tree-action')), '100%');
  dom.window.close();
});

test('time bars distinguish measured zero, missing timing, partial totals and empty scopes', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const view = new IncrementalGraphView(document.querySelector('main'));
  const atoms = new Map([['zero', {duration_ms: 0}], ['timed', {duration_ms: 1000}], ['missing', {duration_ms: null}]]);
  const parent = callSummary(['zero', 'timed', 'missing'], atoms);
  const zero = view.metrics(callSummary(['zero'], atoms), parent, 'G1');
  assert.equal(zero.querySelector('.goal-tree-time-fill').style.width, '0%');
  assert.match(zero.textContent, /0 ms.*0% of G1 · known time only/);
  const unknown = view.metrics(callSummary(['missing'], atoms), parent, 'G1');
  assert.equal(unknown.querySelector('.goal-tree-time-fill'), null);
  assert.match(unknown.querySelector('[role="img"]').getAttribute('aria-label'), /Unknown.*— of G1/);
  const partial = view.metrics(parent, parent, 'run');
  assert.ok(partial.querySelector('.is-partial'));
  assert.match(partial.textContent, /1 s \+ 1 unknown.*100%.*known time only/);
  for (const cost of [callSummary([], atoms), callSummary(['zero'], atoms)]) {
    const node = view.metrics(cost, cost, 'run');
    assert.equal(node.querySelector('.goal-tree-time-fill'), null);
    assert.doesNotMatch(node.outerHTML, /NaN|Infinity/);
  }
  dom.window.close();
});

test('action sources are their own records, attribution anchors stay separate, and shell commands remain visible', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), root = document.querySelector('main'), refs = [];
  data.extraction.cards[1].features.command = {text: 'rg --files src', ref: data.extraction.cards[1].refs[0]};
  const view = new IncrementalGraphView(root, {evidence: ref => refs.push(ref)}); view.update(data, 'first');
  const inferred = [...root.querySelectorAll('.goal-tree-stage')].find(d => d.querySelector('summary').textContent.includes('Default rule'));
  open(inferred, dom); const action = inferred.querySelector('.goal-tree-action'); open(action, dom);
  assert.match(action.textContent, /Tool arguments.*rg --files src.*Tool result/s);
  assert.equal(action.querySelectorAll('h4').length, 2);
  const original = named(action, 'This action’s original records'); open(original, dom);
  for (const button of original.querySelectorAll('button')) button.click();
  assert.ok(refs.length);
  assert.ok(refs.every(r => r.line_number === 3 || r.line_number === 4));
  refs.length = 0;
  const anchor = named(action, 'Attribution rule · no factual evidence'); open(anchor, dom);
  for (const button of anchor.querySelectorAll('button')) button.click();
  assert.equal(refs.length, 0);
  assert.match(anchor.textContent, /not evidence/);
  const select = root.querySelector('select');
  select.value = data.goals[1].id; select.dispatchEvent(new dom.window.Event('change'));
  view.update(data, 'second');
  assert.equal(root.querySelector('select').value, 'all');
  assert.equal(root.querySelectorAll('[data-action-detail]').length, 0);
  dom.window.close();
});

test('keyboard tabs reach Acceptance and recorded completion does not imply accepted subgoals', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), root = document.querySelector('main');
  data.facts.events.push({type: 'run.completed', line: 8});
  data.facts.verification.push({type: 'verification.completed', line: 8, value: {status: 'failed', verification_id: 'v1', subject: {digest: 'candidate'}}, evidence_refs: []});
  const view = new IncrementalGraphView(root); view.update(data);
  assert.match(root.textContent, /Host: completed/);
  assert.doesNotMatch(root.textContent, /Host: accepted/);
  root.querySelector('[role=tab]').dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'ArrowRight'}));
  assert.equal(root.querySelector('[role=tab][aria-selected=true]').textContent, 'Acceptance');
  const receipt = named(root, 'verification.completed · L8'); open(receipt, dom);
  assert.match(receipt.textContent, /failed.*verification_id.*v1/s);
  assert.ok(named(receipt, 'subject'));
  dom.window.close();
});

test('saved default membership controls the tree and cannot become semantic evidence', () => {
  const data = fixture(), card = data.extraction.cards[1], root = data.goals[0];
  data.action_attributions = [{card_id: card.id, goal_id: root.id, stage_id: null,
    basis: 'default_rule', rule: 'current_root', anchor_card_id: null, evidence_refs: []}];
  const tree = goalTree(data);
  const item = tree.byGoal.get(root.id).stages.find(s => s.basis === 'default_rule').actions[0];
  assert.equal(item.card.id, card.id);
  assert.equal(item.attribution.rule, 'current_root');
  assert.equal(item.attribution.card_id, null);
  assert.deepEqual(item.attribution.refs, []);
  assert.ok(tree.unresolved.has(card.id));
  assert.equal(tree.roots[0].cost.calls, 4);
});

test('legacy fallback crosses per-step trace IDs but not parallel loop contexts', () => {
  const data = fixture(), [first, second] = data.extraction.cards;
  first.actor = {trace: 'step1', loop: 'main', role: 'solver'};
  second.actor = {trace: 'step2', loop: 'main', role: 'solver'};
  let tree = goalTree(data);
  assert.equal(tree.byGoal.get(data.goals[1].id).stages.find(s => s.basis === 'default_rule').actions[0].card.id, second.id);
  second.actor.loop = 'parallel-agent';
  tree = goalTree(data);
  const item = tree.roots[0].stages.find(s => s.basis === 'default_rule').actions[0];
  assert.equal(item.card.id, second.id);
  assert.equal(item.attribution.rule, 'current_root');
});

test('framework finish overrides a saved subgoal and is not an unresolved association', () => {
  const data = fixture(), card = data.extraction.cards[0], root = data.goals[0];
  card.kind = 'tool'; card.name = 'finish';
  assert.ok(data.record_assessments[0].stage_ids.length);
  const tree = goalTree(data), stage = tree.byGoal.get(root.id).stages.find(s => s.source_stage_id === 'recorded:delivery');
  assert.equal(stage.basis, 'framework_rule');
  assert.equal(stage.actions[0].framework_rule, true);
  assert.equal(stage.actions[0].root_override, true);
  assert.equal(tree.unresolved.has(card.id), false);
  assert.equal(tree.byGoal.get(data.goals[1].id).stages.some(s => s.card_ids.includes(card.id)), false);
});

test('only semantic stages receive S labels, framework and default groups cannot shift them', () => {
  const data = fixture(), first = data.extraction.cards[0];
  const initial = goalTree(data).stages.find(s => s.basis === 'assessed_stage').display_id;
  data.extraction.cards.push({...first, id: 'early-framework', kind: 'boundary', name: 'acceptance.plan.accepted',
    start_line: 0, end_line: 0, call_ids: []}, {...first, id: 'late-finish', kind: 'tool', name: 'finish',
    start_line: 99, end_line: 100, call_ids: []});
  const tree = goalTree(data);
  assert.equal(initial, 'S1');
  assert.equal(tree.stages.find(s => s.basis === 'assessed_stage').display_id, 'S1');
  assert.ok(tree.stages.filter(s => s.basis === 'framework_rule').every(s => s.display_id === 'Root'));
  assert.ok(tree.stages.filter(s => s.basis === 'default_rule').every(s => s.display_id === ''));
  data.stages[0].display_id = 'S7';
  assert.equal(goalTree(data).stages.find(s => s.basis === 'assessed_stage').display_id, 'S7'); // Server/export labels win.
  assert.equal(filterGoalTree(tree, data.goals[1].id)[0].children[0].stages.find(s => s.basis === 'assessed_stage').display_id, 'S1');
});

test('action detail uses actual metadata judgments and retains evidence-backed legacy fields', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector('main'), data = fixture();
  const review = data.record_assessments[0], card = data.extraction.cards[0];
  Object.assign(review, {purpose: 'Locate the source of the mismatch', delta: 'Found the relevant conversion',
    expected: 'unknown', expected_evidence_refs: [], uptake_card_ids: []});
  const view = new IncrementalGraphView(root); view.update(data, 'metadata-fields');
  let stage = root.querySelector('.goal-tree-stage'); open(stage, dom);
  let detail = stage.querySelector('.goal-tree-action'); open(detail, dom);
  assert.match(detail.textContent, /Locate the source of the mismatch/);
  assert.match(detail.textContent, /Found the relevant conversion/);
  assert.doesNotMatch(detail.textContent, /Expected result|Adopted by|Observed links/);
  const evidenceRef = data.extraction.registry[card.raw_refs[0]]?.ref || {line_number: card.start_line};
  Object.assign(review, {expected: 'Identify the conversion boundary', expected_evidence_refs: [evidenceRef],
    uptake_card_ids: [data.extraction.cards[1].id]});
  view.update(data, 'legacy-fields'); stage = root.querySelector('.goal-tree-stage'); open(stage, dom);
  detail = stage.querySelector('.goal-tree-action'); open(detail, dom);
  assert.match(detail.textContent, /Expected resultIdentify the conversion boundary/);
  assert.match(detail.textContent, /Adopted byA2/);
  dom.window.close();
});

test('child goals and direct stages interleave by actual first action, not goal-first rendering', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), root = document.querySelector('main'), g1 = data.goals[1];
  const g2 = {...g1, id: 'apply-fix', parent_goal_id: g1.id, objective: 'Apply fix', origin_line: 205};
  const g3 = {...g1, id: 'verify-fix', parent_goal_id: g2.id, objective: 'Verify fix', origin_line: 241};
  data.goals.push(g2, g3);
  const template = data.stages[0], call = data.extraction.cards[0], review = data.record_assessments[0];
  data.stages = []; data.extraction.cards = []; data.record_assessments = [];
  const rows = [['read',g1.id,141],['analyze',g1.id,150],['edit',g2.id,203],['more-edit',g2.id,248],['test',g3.id,257],['followup',g2.id,340]];
  for (const [id, gid, line] of rows) {
    data.stages.push({...template, id, goal_id: gid, title: id});
    data.extraction.cards.push({...call, id: `card:${id}`, start_line: line, end_line: line + 2, call_ids: []});
    data.record_assessments.push({...review, card_id: `card:${id}`, stage_ids: [id]});
  }
  const saved = JSON.stringify(data), tree = goalTree(data);
  assert.deepEqual(goalEntries(tree.byGoal.get(g1.id)).map(e => [e.kind,e.line]), [['stage',141],['stage',150],['goal',203]]);
  assert.deepEqual(goalEntries(tree.byGoal.get(g2.id)).map(e => [e.kind,e.line]), [['stage',203],['stage',248],['goal',257],['stage',340]]);
  const view = new IncrementalGraphView(root); view.update(data, 'chronological-tree');
  assert.deepEqual([...root.querySelectorAll('.goal-tree-stage')].map(n => n.dataset.stageId), rows.map(r => r[0]));
  const g1Node = [...root.querySelectorAll('.goal-tree-goal')].find(n => n.dataset.goalId === g1.id);
  const direct = g1Node.querySelector(':scope > .goal-tree-content > .goal-tree-children');
  assert.deepEqual([...direct.children].map(n => n.dataset.stageId || n.dataset.goalId), ['read','analyze','apply-fix']);
  const stage = direct.firstElementChild; open(stage, dom); view.update(data, 'chronological-tree');
  assert.ok(root.querySelector('[data-stage-id="read"]').open);
  const filtered = filterGoalTree(tree, g2.id)[0].children[0];
  assert.deepEqual(goalEntries(filtered).map(e => e.value.id), [g2.id]);
  assert.equal(JSON.stringify(data), saved); // Sorting cannot silently change saved parent relationships.
  dom.window.close();
});
