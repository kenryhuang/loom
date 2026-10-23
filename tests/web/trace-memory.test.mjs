import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {IncrementalGraphView} from '../../src/loom/web/assets/views/incremental-graph.mjs';
import {EvaluationView} from '../../src/loom/web/assets/views/evaluation.mjs';

const fixture = () => JSON.parse(readFileSync(new URL('./fixtures/incremental-graph.json', import.meta.url)));
const click = (root, text) => [...root.querySelectorAll('button')].find(b => b.textContent === text).click();

test('memory tab renders all dimensions, safe text, precise evidence and persistent disclosures', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data = fixture(), root = document.querySelector('main'), selected = [];
  const stage = data.stages[0], ref = data.extraction.cards[0].excerpts[0]?.ref;
  const pointer = ref ? data.extraction.registry[ref].ref : {line_number: 1, field_path: 'output.stdout'};
  const common = {id: 'fact1', title: '<img src=x onerror=alert(1)>', summary: 'macOS host', scope: 'This workspace only', confidence: 'medium',
    goal_ids: [stage.goal_id], stage_ids: [stage.id], action_ids: ['action1'], evidence_refs: [pointer], citations: [{quote: 'Darwin arm64'}]};
  data.memory = {status: 'partial', coverage: {completed_windows: 1, total_windows: 2, evidence_chars: 200, truncated_slices: 1, errors: []},
    contributions: [{stage_id: stage.id, goal_id: stage.goal_id, rating: 'key', partial: true, slices: [{rationale: 'Resolved blocker', key_action_ids: ['action1'], evidence_refs: [pointer]}]}],
    facts: [{...common, basis: 'observed', category: 'environment', revalidate_when: 'Host changes'}],
    lessons: [{...common, id: 'lesson1', title: 'Unsupported grep flag', trigger: 'grep -P', failed_approach: 'Repeated unsupported flags',
      failure_reason: 'Unsupported option', better_approach: 'Use rg', validation: 'proposed'}],
    workflows: [{...common, id: 'workflow1', title: 'Check capabilities first', validation: 'proposed', preconditions: 'macOS',
      steps: ['Check tools', 'Search once', 'Verify'], avoid: 'Repeated failed probes', expected_benefit: 'Fewer probes', preserve: 'Verification'}],
    limitations: ['Run-scoped memory; revalidate before reuse.']};
  const view = new IncrementalGraphView(root, {evidence: r => selected.push(r)}); view.update(data, 'memory-job'); click(root, 'Memory');
  for (const heading of ['Stage contributions', 'Key facts · 1', 'Failure lessons · 1', 'Efficient workflows · 1']) assert.ok(root.textContent.includes(heading));
  assert.match(root.textContent, /Partial memory/);
  assert.match(root.textContent, /Proposed · untested/);
  assert.equal(root.querySelector('img'), null);
  const details = root.querySelector('[data-key="memory:fact1"]'); details.open = true; details.dispatchEvent(new dom.window.Event('toggle'));
  assert.match(details.textContent, /This workspace only/); assert.match(details.textContent, /Darwin arm64/);
  details.querySelector('.action-utility-refs button').click(); assert.deepEqual(selected, [pointer]);
  view.update(data, 'memory-job'); assert.ok(root.querySelector('[data-key="memory:fact1"]').open);
  const tab = root.querySelector('[role=tab][aria-selected=true]');
  tab.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  assert.equal(root.querySelector('[role=tab][aria-selected=true]').textContent, 'Report');
  dom.window.close();
});

test('old evaluations explicitly show memory not extracted', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector('main'), view = new IncrementalGraphView(root);
  view.update(fixture()); click(root, 'Memory');
  assert.match(root.textContent, /Memory was not extracted/);
  assert.equal(root.querySelector('.memory-card'), null);
  dom.window.close();
});

test('memory opt-in is sent only for supported graph jobs and resets for a new evaluation', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const controller = new AbortController(), options = [], root = document.querySelector('main');
  const catalog = {supports_delete: true, supports_memory: true, memory_default: true, jobs: [], models: [{id: 'test', label: 'test'}],
    default_model: 'test', defaults: {max_calls: 24}, bounds: {max_calls: [1, 500]}, analysis_versions: ['goal_graph_v1', 'v3'],
    default_analysis_version: 'goal_graph_v1', run_ids: ['run']};
  const api = {evaluations: async () => catalog, startEvaluation: async (_s, _a, value) => {options.push(value); return {id: 'new'};}};
  const view = new EvaluationView(root, {api, sessionId: 's', analysisId: 'a', signal: controller.signal, evidence: () => {}});
  await view.open(); view.follow = () => {};
  assert.ok(view.memory.checked); assert.equal(view.memoryLabel.hidden, false);
  await view.run(); assert.equal(options[0].extract_memory, true);
  view.memory.checked = false; await view.run(); assert.equal(options[1].extract_memory, false);
  view.version.value = 'v3'; view.version.dispatchEvent(new dom.window.Event('change'));
  assert.equal(view.memoryLabel.hidden, true); await view.run(); assert.equal('extract_memory' in options[2], false);
  view.version.value = 'goal_graph_v1'; view.version.dispatchEvent(new dom.window.Event('change')); assert.ok(view.memory.checked);
  controller.abort(); dom.window.close();
});
