import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {SettingsView} from '../../src/loom/web/assets/views/settings.mjs';

function setup(config) {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const calls = [];
  const data = {path: '/service/config.yaml', revision: 'one', config};
  const view = new SettingsView(document.querySelector('main'), {api: () => ({json: async (path, options) => {
    calls.push([path, options]); return options?.body ? {...data, revision: 'two', config: options.body.config} : data;
  }})});
  const input = (name, value) => {
    const node = document.querySelector(`[name="${name}"]`); assert.ok(node, name);
    node.value = value; node.dispatchEvent(new dom.window.Event('input', {bubbles: true})); return node;
  };
  const click = label => [...document.querySelectorAll('button')].find(node => node.textContent === label).click();
  const save = () => [...document.querySelectorAll('button')].find(node => node.textContent === 'Save configuration').onclick();
  return {dom, view, calls, input, click, save, close: () => {view.close(); dom.window.close();}};
}

test('typed settings save numbers, booleans, lists and model selection while preserving untouched fields and masked secrets', async () => {
  const config = {models: {main: {model: 'test', api_key: '••••••••', request_options: {custom: {threshold: 0.25}, enable_thinking: false}}},
    default_model: 'main', run: {timeout_ms: null}, task: {expected_outputs: ['Report']}, extension: {enabled: true, tags: ['keep']}};
  const ctx = setup(config);
  try {
    await ctx.view.open();
    assert.equal(document.querySelector('.settings-editor'), null);
    assert.doesNotMatch(document.body.textContent, /configuration JSON|Advanced JSON/);
    ctx.click('Models');
    ctx.input('models.main.model', 'updated'); ctx.input('models.main.temperature', '.3');
    ctx.input('models.main.request_options.enable_thinking', 'true');
    ctx.input('run.max_steps', '12'); ctx.input('run.stream', 'false');
    ctx.input('task.expected_outputs.0', 'Analysis report'); ctx.input('evaluation_model', 'main');
    await ctx.save();
    const saved = ctx.calls.find(([, options]) => options?.body)[1].body;
    assert.equal(saved.revision, 'one'); assert.equal(saved.config.models.main.model, 'updated');
    assert.equal(saved.config.models.main.temperature, .3);
    assert.equal(saved.config.models.main.api_key, '••••••••');
    assert.deepEqual(saved.config.models.main.request_options, {custom: {threshold: .25}, enable_thinking: true});
    assert.deepEqual(saved.config.run, {timeout_ms: null, max_steps: 12, stream: false});
    assert.deepEqual(saved.config.task.expected_outputs, ['Analysis report']);
    assert.equal(saved.config.evaluation_model, 'main'); assert.deepEqual(saved.config.extension, config.extension);
    assert.equal(Object.hasOwn(saved.config, 'meta_harness'), false);
    assert.match(document.body.textContent, /Configuration saved/);
  } finally {ctx.close();}
});

test('models and embedding services can be added with forms and selections refresh', async () => {
  const ctx = setup({models: {main: {model: 'main'}}, default_model: 'main'});
  try {
    await ctx.view.open(); ctx.click('Models');
    document.querySelector('[aria-label="New model alias"]').value = 'reviewer'; ctx.click('Add model');
    ctx.input('models.reviewer.model', 'review-model'); ctx.input('models.reviewer.base_url', 'https://example.com/v1');
    ctx.input('evaluation_model', 'reviewer');
    ctx.click('Embedding services'); ctx.click('Add embedding service');
    ctx.input('embedding_profiles.0.name', 'Local embeddings');
    ctx.input('embedding_profiles.0.endpoint', 'http://localhost:1234/v1/embeddings');
    await ctx.save();
    const config = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.equal(config.evaluation_model, 'reviewer'); assert.equal(config.models.reviewer.model, 'review-model');
    assert.equal(config.embedding_profiles[0].name, 'Local embeddings'); assert.match(config.embedding_profiles[0].id, /^emb_/);
  } finally {ctx.close();}
});

test('invalid values focus the right tab and prevent save without losing draft', async () => {
  const ctx = setup({models: {main: {model: 'test'}}, default_model: 'main'});
  try {
    await ctx.view.open(); ctx.input('models.main.context_window_tokens', '50');
    ctx.input('task.objective', 'Keep this draft');
    await ctx.save();
    assert.equal(ctx.calls.filter(([, options]) => options?.body).length, 0);
    assert.equal(document.getElementById('settings-tab-models').getAttribute('aria-selected'), 'true');
    assert.equal(document.querySelector('[name="task.objective"]').value, 'Keep this draft');
    ctx.input('models.main.context_window_tokens', '128000'); await ctx.save();
    assert.equal(ctx.calls.filter(([, options]) => options?.body).length, 1);
  } finally {ctx.close();}
});

test('provider options can add typed nested parameters without a JSON editor', async () => {
  const ctx = setup({models: {main: {model: 'test'}}});
  try {
    await ctx.view.open(); ctx.click('Models');
    const name = document.querySelector('[aria-label="models.main.request_options parameter name"]');
    name.value = 'custom_limit';
    const type = document.querySelector('[aria-label="models.main.request_options parameter type"]'); type.value = 'number';
    [...name.parentElement.querySelectorAll('button')].find(node => node.textContent === 'Add parameter').click();
    ctx.input('models.main.request_options.custom_limit', '9'); await ctx.save();
    const config = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.equal(config.models.main.request_options.custom_limit, 9);
  } finally {ctx.close();}
});

test('model removal preserves selected roles and untouched nested list values survive save', async () => {
  const extensions = [{name: 'first', retries: 2}, {name: 'second', retries: 4}];
  const ctx = setup({default_model: 'main', models: {main: {model: 'test'}}, extensions});
  try {
    await ctx.view.open(); ctx.click('Models'); ctx.click('Remove model');
    assert.match(document.body.textContent, /selected for a model role/);
    assert.ok(document.querySelector('[name="models.main.model"]'));
    await ctx.save();
    assert.deepEqual(ctx.calls.find(([, options]) => options?.body)[1].body.config.extensions, extensions);
  } finally {ctx.close();}
});
