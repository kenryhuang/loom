import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {SettingsView} from '../../src/loom/web/assets/views/settings.mjs';

test('settings separates configuration tabs and saves with revision', async () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const calls = [];
  const data = {path: '/service/config.yaml', revision: 'one', config: {models: {main: {model: 'test', api_key: '••••••••'}}, default_model: 'main'}};
  const view = new SettingsView(document.querySelector('main'), {api: () => ({json: async (path, options) => {
    calls.push([path, options]); return options?.body ? {...data, revision: 'two', config: options.body.config} : data;
  }})});
  try {
    await view.open();
    const tab = [...document.querySelectorAll('[role=tab]')].find(node => node.textContent === 'Models'); tab.click();
    assert.equal(tab.getAttribute('aria-selected'), 'true');
    const editor = document.querySelector('[aria-label="Models configuration JSON"]');
    editor.value = JSON.stringify({main: {model: 'updated', api_key: '••••••••'}});
    await [...document.querySelectorAll('button')].find(node => node.textContent === 'Save configuration').onclick();
    const saved = calls.find(([, options]) => options?.body)[1].body;
    assert.equal(saved.revision, 'one'); assert.equal(saved.config.models.main.model, 'updated');
    assert.match(document.body.textContent, /Saved to \/service\/config.yaml/);
  } finally {view.close(); dom.window.close();}
});
