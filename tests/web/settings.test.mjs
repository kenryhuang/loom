import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {SettingsView} from '../../src/loom/web/assets/views/settings.mjs';

function setup(config) {
  // Model fixtures use one provider; the API always returns the normalized hierarchy.
  if (config.models) {
    const {models: fixtures, ...rest} = config;
    const models = Object.fromEntries(Object.entries(fixtures).map(([id, {model, ...options}]) => [id, options]));
    for (const key of ['default_model', 'evaluation_model', 'verification_model']) if (rest[key]) rest[key] = `example/${rest[key]}`;
    config = {...rest, providers: {example: {base_url: 'https://example.com/v1', api_key_env: 'TEST_KEY', models}}};
  }
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

test('Deep eval options can be configured independently of model options', async () => {
  const ctx = setup({models: {main: {model: 'main', request_options: {reasoning_effort: 'max', enable_thinking: true}}}, default_model: 'main'});
  try {
    await ctx.view.open(); ctx.click('Deep eval request options');
    ctx.input('evaluation_request_options.enable_thinking', 'true');
    ctx.input('evaluation_request_options.reasoning_effort', 'low');
    await ctx.save();
    const saved = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.deepEqual(saved.evaluation_request_options, {enable_thinking: true, reasoning_effort: 'low'});
    assert.deepEqual(saved.providers.example.models.main.request_options, {enable_thinking: true, reasoning_effort: 'max'});
  } finally {ctx.close();}
});

test('typed settings save numbers, booleans, lists and model selection while preserving untouched fields and masked secrets', async () => {
  const config = {models: {main: {model: 'test', api_key: '••••••••', request_options: {custom: {threshold: 0.25}, enable_thinking: false}}},
    default_model: 'main', run: {timeout_ms: null}, task: {expected_outputs: ['Report']}, extension: {enabled: true, tags: ['keep']}};
  const ctx = setup(config);
  try {
    await ctx.view.open();
    assert.equal(document.querySelector('.settings-editor'), null);
    assert.doesNotMatch(document.body.textContent, /configuration JSON|Advanced JSON/);
    ctx.click('Models');
    ctx.input('providers.example.models.main.temperature', '.3');
    ctx.input('providers.example.models.main.request_options.enable_thinking', 'true');
    ctx.input('run.max_steps', '12'); ctx.input('run.stream', 'false');
    ctx.input('task.expected_outputs.0', 'Analysis report'); ctx.input('evaluation_model', 'example/main');
    await ctx.save();
    const saved = ctx.calls.find(([, options]) => options?.body)[1].body;
    assert.equal(saved.revision, 'one'); assert.equal(saved.config.providers.example.models.main.model, undefined);
    assert.equal(saved.config.providers.example.models.main.temperature, .3);
    assert.equal(saved.config.providers.example.models.main.api_key, '••••••••');
    assert.deepEqual(saved.config.providers.example.models.main.request_options, {custom: {threshold: .25}, enable_thinking: true});
    assert.deepEqual(saved.config.run, {timeout_ms: null, max_steps: 12, stream: false});
    assert.deepEqual(saved.config.task.expected_outputs, ['Analysis report']);
    assert.equal(saved.config.evaluation_model, 'example/main'); assert.deepEqual(saved.config.extension, config.extension);
    assert.equal(Object.hasOwn(saved.config, 'meta_harness'), false);
    assert.match(document.body.textContent, /Configuration saved/);
  } finally {ctx.close();}
});

test('models and embedding services can be added with forms and selections refresh', async () => {
  const ctx = setup({models: {main: {model: 'main'}}, default_model: 'main'});
  try {
    await ctx.view.open(); ctx.click('Models');
    document.querySelector('[aria-label="New model ID"]').value = 'reviewer'; ctx.click('Add model');
    ctx.input('providers.example.base_url', 'https://example.com/v1');
    ctx.input('evaluation_model', 'example/reviewer');
    ctx.click('Embedding services'); ctx.click('Add embedding service');
    ctx.input('embedding_profiles.0.name', 'Local embeddings');
    ctx.input('embedding_profiles.0.endpoint', 'http://localhost:1234/v1/embeddings');
    await ctx.save();
    const config = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.equal(config.evaluation_model, 'example/reviewer'); assert.deepEqual(config.providers.example.models.reviewer, {});
    assert.equal(config.embedding_profiles[0].name, 'Local embeddings'); assert.match(config.embedding_profiles[0].id, /^emb_/);
  } finally {ctx.close();}
});

test('invalid values focus the right tab and prevent save without losing draft', async () => {
  const ctx = setup({models: {main: {model: 'test'}}, default_model: 'main'});
  try {
    await ctx.view.open(); ctx.input('providers.example.models.main.context_window_tokens', '50');
    ctx.input('task.objective', 'Keep this draft');
    await ctx.save();
    assert.equal(ctx.calls.filter(([, options]) => options?.body).length, 0);
    assert.equal(document.getElementById('settings-tab-providers').getAttribute('aria-selected'), 'true');
    assert.equal(document.querySelector('[name="task.objective"]').value, 'Keep this draft');
    ctx.input('providers.example.models.main.context_window_tokens', '128000'); await ctx.save();
    assert.equal(ctx.calls.filter(([, options]) => options?.body).length, 1);
  } finally {ctx.close();}
});

test('provider options can add typed nested parameters without a JSON editor', async () => {
  const ctx = setup({models: {main: {model: 'test'}}});
  try {
    await ctx.view.open(); ctx.click('Models');
    const name = document.querySelector('[aria-label="providers.example.models.main.request_options parameter name"]');
    name.value = 'custom_limit';
    const type = document.querySelector('[aria-label="providers.example.models.main.request_options parameter type"]'); type.value = 'number';
    [...name.parentElement.querySelectorAll('button')].find(node => node.textContent === 'Add parameter').click();
    ctx.input('providers.example.models.main.request_options.custom_limit', '9'); await ctx.save();
    const config = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.equal(config.providers.example.models.main.request_options.custom_limit, 9);
  } finally {ctx.close();}
});

test('model removal preserves selected roles and untouched nested list values survive save', async () => {
  const extensions = [{name: 'first', retries: 2}, {name: 'second', retries: 4}];
  const ctx = setup({default_model: 'main', models: {main: {model: 'test'}}, extensions});
  try {
    await ctx.view.open(); ctx.click('Models'); ctx.click('Remove model');
    assert.match(document.body.textContent, /selected for a model role/);
    assert.ok(document.querySelector('[name="providers.example.models.main.temperature"]'));
    assert.equal(document.querySelector('[name="providers.example.models.main.model"]'), null);
    await ctx.save();
    assert.deepEqual(ctx.calls.find(([, options]) => options?.body)[1].body.config.extensions, extensions);
  } finally {ctx.close();}
});

test('providers share connection fields, namespace model IDs and preserve model options', async () => {
  const ctx = setup({providers: {
    cloud: {base_url: 'https://cloud.example/v1', api_key_env: 'CLOUD_KEY', models: {main: {temperature: 0}}},
  }, default_model: 'cloud/main'});
  try {
    await ctx.view.open(); ctx.click('Models');
    assert.equal(document.querySelector('[name="providers.cloud.models.main.base_url"]'), null);
    ctx.input('providers.cloud.base_url', 'https://new.example/v1');
    ctx.input('providers.cloud.api_key_env', 'NEW_KEY');
    document.querySelector('[aria-label="New provider name"]').value = 'local'; ctx.click('Add provider');
    ctx.input('providers.local.base_url', 'http://localhost:8080/v1');
    const local = document.querySelector('[name="providers.local.base_url"]').closest('section.settings-card');
    const alias = local.querySelector('[aria-label="New model ID"]');
    const add = [...local.querySelectorAll('button')].find(node => node.textContent === 'Add model');
    alias.value = 'main'; add.click();
    const newAlias = [...document.querySelectorAll('[aria-label="New model ID"]')].at(-1);
    newAlias.value = 'main';
    [...newAlias.parentElement.querySelectorAll('button')].find(node => node.textContent === 'Add model').click();
    assert.match(newAlias.validationMessage, /already exists/);
    newAlias.value = ''; newAlias.dispatchEvent(new ctx.dom.window.Event('input'));
    ctx.input('providers.local.models.main.temperature', '.7');
    ctx.input('evaluation_model', 'local/main');
    await ctx.save();
    const config = ctx.calls.find(([, options]) => options?.body)[1].body.config;
    assert.equal(config.models, undefined);
    assert.equal(config.providers.cloud.base_url, 'https://new.example/v1');
    assert.equal(config.providers.cloud.api_key_env, 'NEW_KEY');
    assert.equal(config.providers.cloud.models.main.temperature, 0);
    assert.equal(config.providers.local.models.main.temperature, .7);
    assert.equal(config.evaluation_model, 'local/main');
  } finally {ctx.close();}
});
