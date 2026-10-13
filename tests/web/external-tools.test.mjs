import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {ExternalToolsView, toolChoices, selectedTools} from '../../src/loom/web/assets/views/external-tools.mjs';
import {SessionSetupView} from '../../src/loom/web/assets/views/session-setup.mjs';

const tool = {id: 'ext_one', name: 'Find <script>bad()</script>', connector_name: 'Docs', description: 'Search indexed docs', kind: 'mcp', effect_kind: 'read_only', input_schema: {type: 'object'}};
const connection = {id: 'server', name: 'Docs', description: 'Docs MCP', kind: 'mcp', enabled: true, config: {transport: 'stdio', command: 'example'}, tools: [tool]};
const tick = () => new Promise(r => setTimeout(r, 0));

test('external tools management registers, discovers and edits without executing a tool', async () => {
  const dom = new JSDOM('<aside><button id="register" disabled>Register</button></aside><main></main>'); globalThis.document = dom.window.document;
  const calls = [], api = {async json(path, options) {calls.push([path, options?.body]); return path === '/v1/external-tools' && !options ? {connections: [connection], tools: [tool]} : connection;}};
  const root = document.querySelector('main'), registerButton = document.querySelector('#register');
  const view = new ExternalToolsView(root, {api: () => api, registerButton});
  await view.open(); assert.equal(root.querySelector('script'), null);
  assert.equal(registerButton.disabled, false);
  assert.ok(![...root.querySelectorAll('button')].some(b => b.textContent.includes('Register')));
  const search = root.querySelector('input[type="search"]');
  search.value = 'missing'; search.dispatchEvent(new dom.window.Event('input'));
  assert.match(root.textContent, /No matching connections/);
  search.value = 'indexed'; search.dispatchEvent(new dom.window.Event('input'));
  assert.equal(root.querySelectorAll('.external-connection-heading').length, 1);
  [...root.querySelectorAll('button')].find(b => b.textContent === 'Discover tools').click(); await tick();
  assert.ok(calls.some(([path]) => path.endsWith('/server/discover')));
  assert.equal(root.querySelector('input[type="search"]').value, 'indexed');
  registerButton.click();
  const form = root.querySelector('form'); form.elements.name.value = 'CLI'; form.elements.description.value = 'Echo';
  form.elements.kind.value = 'cli'; form.elements.kind.dispatchEvent(new dom.window.Event('change'));
  form.dispatchEvent(new dom.window.Event('submit', {cancelable: true})); await tick();
  const saved = calls.find(([path, body]) => path === '/v1/external-tools' && body);
  assert.equal(saved[1].kind, 'cli'); assert.equal(saved[1].config.command, 'rg');
  assert.ok(!calls.some(([path]) => path.includes('/call')));
  view.close(); assert.equal(registerButton.disabled, true);
  dom.window.close();
});

test('tool chooser selects actual IDs and escapes external descriptions', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector('main'); toolChoices(root, [tool], [tool.id], {[tool.id]: 'Relevant to task'});
  assert.deepEqual(selectedTools(root), [tool.id]); assert.equal(root.querySelector('script'), null);
  assert.match(root.textContent, /Relevant to task/); root.querySelector('input').checked = false;
  assert.deepEqual(selectedTools(root), []); dom.window.close();
});

test('new session recommendations select external tools and remain manually editable', async () => {
  const html = await readFile(new URL('../../src/loom/web/assets/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html); globalThis.document = dom.window.document;
  const root = document.querySelector('#create-page'); root.hidden = false;
  const template = {id: 'general', task_spec: {tools: {collections: ['task_control']}}};
  const catalog = {setup_recommendation: true, models: [{id: 'main'}], templates: [template], external_tools: [tool],
    tool_collections: [{id: 'task_control', label: 'Finish', description: 'Complete'}, {id: 'external_tools', label: 'External', description: 'Registered tools'}]};
  document.querySelector('#create-template').innerHTML = '<option value="general">General</option>';
  document.querySelector('#create-objective').value = 'Search documents';
  const api = {async json() {return {task_type: 'general', tool_collections: ['task_control', 'external_tools'], external_tools: [tool.id], external_tool_reasons: {[tool.id]: 'Use docs'}, rationale: 'Search', usage: {total_tokens: 1}};}};
  let view; view = new SessionSetupView(root, {api: () => api, templateChanged: () => view.template(template)});
  view.configure(catalog); view.template(template); await view.recommend();
  assert.deepEqual(selectedTools(root.querySelector('#create-external-tools')), [tool.id]);
  let payload = {knowledge_base_ids: []}; view.apply(payload, template); assert.deepEqual(payload.external_tool_ids, [tool.id]);
  const check = root.querySelector('#create-external-tools input'); check.checked = false; check.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
  payload = {knowledge_base_ids: []}; view.apply(payload, template); assert.deepEqual(payload.external_tool_ids, []);
  await view.recommend();
  root.querySelector('#create-collections input[value="external_tools"]').click();
  payload = {knowledge_base_ids: []}; view.apply(payload, template); assert.deepEqual(payload.external_tool_ids, []);
  dom.window.close();
});

test('paused session can update tools but active execution and recovery cannot', async () => {
  const {canUpdateTaskTools} = await import('../../src/loom/web/assets/state.mjs');
  const paused = {task: {state: 'paused'}, run: {state: 'suspended'}};
  assert.equal(canUpdateTaskTools(paused), true);
  assert.equal(canUpdateTaskTools({...paused, run: {state: 'running'}}), false);
  assert.equal(canUpdateTaskTools({...paused, task: {state: 'pausing'}}), false);
  assert.equal(canUpdateTaskTools({...paused, input_request: {state: 'pending', kind: 'recovery'}}), false);
  assert.equal(canUpdateTaskTools({...paused, workspace_blocked: ['process']}), false);
});
