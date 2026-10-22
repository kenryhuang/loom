import {element} from '../markdown.mjs';
import {action, fields, textField, modelChoice, providerFields, modelFields, requestFields, taskFields, runFields, metaFields, labelFor} from './settings-fields.mjs';

export class SettingsView {
  constructor(root, {api, changed}) {Object.assign(this, {root, api, changed});}
  close() {this.abort?.abort();}
  async open() {
    this.close(); this.abort = new AbortController();
    const signal = this.abort.signal;
    this.root.replaceChildren(element('h1', '', 'Settings'));
    try {
      const data = await this.api().json('/v1/settings', {signal});
      if (!signal.aborted) this.render(data);
    } catch (error) {if (!signal.aborted) this.root.append(element('p', 'error', error.message));}
  }
  render(data, message = '') {
    this.data = data;
    this.draft = structuredClone(data.config);
    const draft = this.draft, signal = this.abort.signal;
    this.root.replaceChildren(element('h1', '', 'Settings'), element('p', 'muted small settings-path', data.path));
    const notice = element('p', 'notice', message); notice.hidden = !message; notice.setAttribute('role', 'status');
    const announce = message => {notice.textContent = message; notice.hidden = false;};
    this.root.append(notice);
    const controls = element('fieldset', 'settings-controls'); this.root.append(controls);
    const tabs = element('div', 'evaluation-tabs settings-tabs'), body = element('div', 'settings-body');
    tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Configuration categories'); controls.append(tabs, body);
    const groups = [['general', 'Model selection'], ['providers', 'Models'], ['embedding_profiles', 'Embedding services'],
      ['task', 'Task defaults'], ['run', 'Run defaults'], ['meta_harness', 'Evaluation / Evolve']];
    const extras = Object.keys(draft).filter(key => key !== 'model_aliases' && draft[key] !== null && typeof draft[key] === 'object' && !groups.some(([id]) => key === id));
    groups.push(...extras.map(key => [key, labelFor(key)]));
    const panels = new Map();
    const choose = key => {
      this.selected = key;
      for (const [id, {panel, tab}] of panels) {
        panel.hidden = id !== key; tab.setAttribute('aria-selected', String(id === key)); tab.tabIndex = id === key ? 0 : -1;
      }
    };
    const context = {models: () => Object.fromEntries(Object.entries(draft.providers || {}).flatMap(([name, provider]) => Object.entries(provider.models || {}).map(([id, model]) => [`${name}/${id}`, model])))};
    for (const [key, title] of groups) {
      const tab = action(title, () => choose(key)), panel = element('section', 'settings-category');
      tab.id = `settings-tab-${key}`; panel.id = `settings-content-${key}`;
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', panel.id);
      panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id);
      panel.append(element('h2', '', title)); tabs.append(tab); body.append(panel); panels.set(key, {panel, tab});
      tab.onkeydown = event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); const index = groups.findIndex(([id]) => id === key);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? groups.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + groups.length) % groups.length;
        choose(groups[next][0]); panels.get(groups[next][0]).tab.focus();
      };
      if (key === 'general') {
        panel.append(element('p', 'muted', 'Choose which configured model handles each role.'));
        const known = ['default_model', 'evaluation_model', 'verification_model', 'visual_verification_model'];
        const scalarKeys = [...new Set([...known, ...Object.keys(draft).filter(k =>
          (draft[k] === null || typeof draft[k] !== 'object') && !groups.some(([id]) => id === k))])];
        fields(panel, () => Object.fromEntries(scalarKeys.filter(k => Object.hasOwn(draft, k)).map(k => [k, draft[k]])), next => {
          for (const k of scalarKeys) {if (Object.hasOwn(next, k)) draft[k] = next[k]; else delete draft[k];}
        }, {default_model: modelChoice('Default task model'), evaluation_model: modelChoice('Deep evaluation model'), verification_model: modelChoice('Acceptance verifier model'), visual_verification_model: modelChoice('Visual acceptance model')}, context);
      } else if (key === 'providers') this.providers(panel, context, announce);
      else if (key === 'embedding_profiles') this.embeddings(panel, context);
      else {
        if (key === 'run') panel.append(element('p', 'muted', 'Defaults for new task runs. Empty fields retain Loom defaults.'));
        if (key === 'meta_harness') panel.append(element('p', 'muted', 'Configure optimization models, candidate search and trial budgets.'));
        if (Array.isArray(draft[key])) fields(panel, () => ({items: draft[key]}), value => {draft[key] = value.items;},
          {items: {label: title, type: 'list'}}, context, key);
        else fields(panel, () => draft[key], value => {draft[key] = value;}, {task: taskFields, run: runFields, meta_harness: metaFields}[key] || {}, context, key);
      }
    }
    choose(panels.has(this.selected) ? this.selected : 'general');
    const footer = element('div', 'settings-footer');
    const save = action('Save configuration', async () => {
      const invalid = [...controls.querySelectorAll('input,select,textarea')].find(input => !input.checkValidity());
      if (invalid) {
        const panel = invalid.closest('[role=tabpanel]');
        if (panel) choose([...panels].find(([, entry]) => entry.panel === panel)[0]);
        for (let node = invalid.parentElement; node && node !== controls; node = node.parentElement) if (node.tagName === 'DETAILS') node.open = true;
        invalid.reportValidity(); invalid.focus(); announce('Check the highlighted field before saving.'); return;
      }
      const models = context.models();
      for (const key of ['default_model', 'evaluation_model', 'verification_model', 'visual_verification_model']) {
        if (draft[key] && !Object.hasOwn(models, draft[key])) {announce(`${labelFor(key)} must refer to a configured model.`); choose('general'); return;}
      }
      if (draft.meta_harness && Object.keys(draft.meta_harness).length) {
        for (const key of ['proposer_model', 'solver_model', 'judge_model']) if (!Object.hasOwn(models, draft.meta_harness[key])) {
          announce(`Select a configured ${labelFor(key).toLowerCase()} before saving Evaluation / Evolve settings.`); choose('meta_harness'); return;
        }
      }
      controls.disabled = true; save.textContent = 'Saving…';
      try {
        const result = await this.api().json('/v1/settings', {signal, body: {revision: this.data.revision, config: structuredClone(draft)}});
        if (signal.aborted) return;
        this.render(result, 'Configuration saved. New requests use the updated settings.');
        await this.changed?.();
      } catch (error) {if (!signal.aborted) announce(error.message);}
      finally {controls.disabled = false; save.textContent = 'Save configuration';}
    });
    save.className = 'primary'; footer.append(element('span', 'muted small', 'Changes are applied when you save.'), save);
    controls.append(footer);
  }
  redraw() {this.render({...this.data, config: this.draft});}
  providers(root, context, announce) {
    const providers = this.draft.providers ||= {};
    for (const [name, provider] of Object.entries(providers)) {
      const card = element('section', 'settings-card');
      card.append(element('h3', '', name)); root.append(card);
      fields(card, () => Object.fromEntries(Object.entries(providers[name]).filter(([key]) => key !== 'models')),
        value => {providers[name] = {...value, models: providers[name].models};}, providerFields, context, `providers.${name}`);
      this.models(card, context, announce, name, provider.models ||= {});
      card.append(action('Remove provider', () => {
        if (Object.keys(providers[name].models).length) {announce('Remove the provider’s models first.'); return;}
        delete providers[name]; this.redraw();
      }));
    }
    const add = element('div', 'settings-add-model'), name = element('input');
    name.placeholder = 'Provider name, e.g. dashscope'; name.setAttribute('aria-label', 'New provider name');
    name.oninput = () => name.setCustomValidity('');
    add.append(name, action('Add provider', () => {
      const key = name.value.trim();
      name.setCustomValidity(!/^[a-zA-Z0-9_-]+$/.test(key) ? 'Use letters, numbers, underscores or hyphens.' : Object.hasOwn(providers, key) ? 'This provider already exists.' : '');
      if (!name.reportValidity()) return;
      Object.defineProperty(providers, key, {value: {base_url: '', api_key_env: 'LOOM_LLM_API_KEY', models: {}}, enumerable: true, writable: true, configurable: true});
      this.redraw();
    })); root.append(add);
  }
  models(root, context, announce, providerName, models) {
    const prefix = `providers.${providerName}.models`;
    if (!Object.keys(models).length) root.append(element('p', 'muted', 'No models configured. Add a model to get started.'));
    for (const name of Object.keys(models)) {
      const card = element('details', 'settings-card'); card.open = `${providerName}/${name}` === (this.openModel || this.draft.default_model || `${providerName}/${Object.keys(models)[0]}`);
      const summary = element('summary'); summary.append(element('strong', '', `${providerName}/${name}`));
      card.append(summary); root.append(card);
      fields(card, () => Object.fromEntries(Object.entries(models[name]).filter(([key]) => key !== 'request_options')),
        value => {models[name] = {...value, ...(models[name].request_options !== undefined ? {request_options: models[name].request_options} : {})};},
        modelFields, context, `${prefix}.${name}`);
      const advanced = element('details', 'settings-field-group'); advanced.append(element('summary', '', 'Provider request options'));
      fields(advanced, () => models[name].request_options, value => {models[name].request_options = value;}, requestFields, {...context, compact: true}, `${prefix}.${name}.request_options`, true);
      card.append(advanced, action('Remove model', () => {
        const used = ['default_model', 'evaluation_model', 'verification_model', 'visual_verification_model'].some(key => this.draft[key] === `${providerName}/${name}`)
          || ['proposer_model', 'solver_model', 'judge_model'].some(key => this.draft.meta_harness?.[key] === `${providerName}/${name}`);
        if (used) {announce(`“${name}” is selected for a model role. Change its model selections before removing it.`); return;}
        delete models[name];
        if (this.draft.model_aliases) this.draft.model_aliases = Object.fromEntries(Object.entries(this.draft.model_aliases).filter(([, target]) => target !== `${providerName}/${name}`));
        this.redraw();
      }));
    }
    const add = element('div', 'settings-add-model'), name = element('input');
    name.placeholder = 'Model ID, e.g. glm-5.2'; name.setAttribute('aria-label', 'New model ID');
    name.oninput = () => name.setCustomValidity('');
    add.append(name, action('Add model', () => {
      const key = name.value.trim();
      name.setCustomValidity(!key || /\s/.test(key) ? 'Enter a model ID without whitespace.' : Object.hasOwn(models, key) ? 'This model already exists in this provider.' : '');
      if (!name.reportValidity()) return;
      Object.defineProperty(models, key, {value: {}, enumerable: true, writable: true, configurable: true});
      this.openModel = `${providerName}/${key}`; this.redraw();
    })); root.append(add);
  }
  embeddings(root, context) {
    const profiles = this.draft.embedding_profiles || [];
    if (!profiles.length) root.append(element('p', 'muted', 'No embedding services configured.'));
    profiles.forEach((profile, index) => {
      const card = element('section', 'settings-card'); card.append(element('h3', '', profile.name || 'New embedding service')); root.append(card);
      const specs = {
        name: textField('Name', {required: true}), endpoint: textField('Embeddings endpoint', {required: true, type: 'url'}),
        model: textField('Embedding model', {required: true}), api_key_env: textField('API key environment variable'),
      };
      fields(card, () => Object.fromEntries(Object.entries(profiles[index]).filter(([key]) => key !== 'id')),
        value => {profiles[index] = {...value, id: profile.id};}, specs, context, `embedding_profiles.${index}`);
      card.append(action('Remove service', () => {profiles.splice(index, 1); this.draft.embedding_profiles = profiles; this.redraw();}));
    });
    root.append(action('Add embedding service', () => {
      profiles.push({id: `emb_${crypto.randomUUID().replaceAll('-', '')}`, name: 'New embedding service',
        endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings', model: 'text-embedding-v4', api_key_env: 'LOOM_LLM_API_KEY'});
      this.draft.embedding_profiles = profiles; this.redraw();
    }), element('p', 'muted small', 'Credentials use environment-variable names. An indexed service cannot change its model or endpoint.'));
  }
}
