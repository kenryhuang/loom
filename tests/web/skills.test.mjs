import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {SkillsView} from '../../src/loom/web/assets/views/skills.mjs';

const data = () => ({workspace: '/project', catalog: {total_cases: 7, families: 3, uncertain_cases: 1}, models: [{id: 'test', label: 'Test'}],
  default_model: 'test', defaults: {max_calls: 9}, jobs: [], versions: [{id: 'v1', state: 'candidate', revision: 1, sources_available: true,
    evidence_state: 'proposed', coverage: {discovery_families: 3, condition_variations: 2}, uses: [], validations: [], limitations: ['Not executed'],
    contract: {title: '<img src=x onerror=alert(1)>', summary: 'Scoped method', trigger: 'HTTP errors', preconditions: ['Fresh connection'],
      claims: [{text: 'Compare connections', basis: 'observed_pattern', supporting_family_ids: ['a','b','c'], contradicting_family_ids: [], unknown: [], evidence_refs: []}],
      steps: [{instruction: 'Compare actual responses'}], gaps: [], blocking_issues: []}}]});
const flush = () => new Promise(resolve => setTimeout(resolve, 10));

test('failed induction shows why no Skill exists and measured repair limit', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const abort = new AbortController(), root = document.querySelector('main');
  const current = {...data(), versions: [], jobs: [{id: 'j1', query: '运动仿真', state: 'failed', stage: 'synthesis-repair-2',
    usage: {calls: 5, tokens: 75237}, settings: {max_calls: 12}, error: 'Method too long',
    candidate_diagnostic: {characters: 7700, maximum: 6000}}]};
  const view = new SkillsView(root, {api: {json: async () => current}, sessionId: 'one', signal: abort.signal});
  await view.open();
  assert.match(root.textContent, /尚未生成 Skill 版本/);
  assert.match(root.textContent, /7,700 字符/);
  assert.match(root.textContent, /6,000 字符上限/);
  assert.ok([...root.querySelectorAll('button')].some(b => b.textContent === '恢复'));
  abort.abort(); dom.window.close();
});

test('Skills renders deduped evidence, starts bounded job and never offers unvalidated activation', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const abort = new AbortController(), calls = [], root = document.querySelector('main');
  const api = {json: async (path, options) => {calls.push([path, options]); return data();}};
  const view = new SkillsView(root, {api, sessionId: 'sess/one', signal: abort.signal});
  await view.open('HTTP reuse');
  assert.match(root.textContent, /3 去重家族/); assert.match(root.textContent, /proposed/);
  assert.equal(root.querySelector('img'), null);
  assert.ok(![...root.querySelectorAll('button')].some(b => b.textContent === '发布为 active'));
  root.querySelector('form').dispatchEvent(new dom.window.Event('submit', {cancelable: true})); await flush();
  const request = calls.find(([path]) => path.includes('/jobs?'));
  assert.equal(request[0], '/v1/skills/jobs?session_id=sess%2Fone');
  assert.deepEqual(request[1].body, {query: 'HTTP reuse', model: 'test', settings: {max_calls: 9}});
  abort.abort(); dom.window.close();
});

test('Skills revocation hides method and export; trial action uses current revision', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const abort = new AbortController(), calls = [], root = document.querySelector('main');
  let current = data();
  const api = {json: async (path, options) => {calls.push([path, options]); return current;}};
  const view = new SkillsView(root, {api, sessionId: 'sess', signal: abort.signal}); await view.open();
  [...root.querySelectorAll('button')].find(b => b.textContent === '显式试用下一次 run').click(); await flush();
  const trial = calls.find(([path]) => path.includes('/trial?'));
  assert.deepEqual(trial[1].body, {revision: 1});
  current = {...current, versions: [{id: 'v1', state: 'suspended', sources_available: false}]}; await view.refresh();
  assert.match(root.textContent, /证据内容不再导出/);
  assert.ok(![...root.querySelectorAll('button')].some(b => b.textContent === '下载 Skill 包'));
  abort.abort(); dom.window.close();
});

test('late Skills response cannot overwrite an aborted page', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const abort = new AbortController(), root = document.querySelector('main'); let resolve;
  const view = new SkillsView(root, {api: {json: () => new Promise(r => {resolve = r;})}, sessionId: 'one', signal: abort.signal});
  const pending = view.open(); abort.abort(); root.textContent = 'another page'; resolve(data()); await pending;
  assert.equal(root.textContent, 'another page'); dom.window.close();
});
