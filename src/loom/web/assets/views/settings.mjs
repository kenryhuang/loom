import {element} from '../markdown.mjs';

export class SettingsView {
  constructor(root, {api, changed}) {Object.assign(this, {root, api, changed});}
  close() {this.abort?.abort();}
  async open() {
    this.close(); this.abort = new AbortController();
    const signal = this.abort.signal;
    this.root.replaceChildren(element('h1', '', 'Settings'));
    const notice = element('p', 'notice'); this.root.append(notice);
    try {
      const data = await this.api().json('/v1/settings', {signal});
      if (signal.aborted) return;
      this.data = data;
      notice.textContent = data.path;
      const tabs = element('div', 'kb-tabs settings-tabs'), body = element('section', 'settings-body');
      tabs.setAttribute('role', 'tablist'); this.root.append(tabs, body);
      const config = data.config, scalar = {};
      for (const [key, value] of Object.entries(config)) if (value === null || typeof value !== 'object') scalar[key] = value;
      const groups = [['general', 'Model selection', scalar], ['models', 'Models', config.models || {}],
        ['embedding_profiles', 'Embedding services', config.embedding_profiles || []], ['task', 'Task defaults', config.task || {}],
        ['run', 'Run defaults', config.run || {}], ['meta_harness', 'Evaluation / Evolve', config.meta_harness || {}]];
      for (const [key, value] of Object.entries(config)) if (typeof value === 'object' && value !== null && !groups.some(g => g[0] === key)) groups.push([key, key.replaceAll('_', ' '), value]);
      const editors = new Map();
      for (const [key, label, value] of groups) {
        const tab = element('button', 'quiet', label); tab.type = 'button'; tab.setAttribute('role', 'tab');
        const panel = element('section'); panel.setAttribute('role', 'tabpanel'); panel.append(element('h2', '', label));
        const editor = element('textarea', 'settings-editor'); editor.value = JSON.stringify(value, null, 2); editor.rows = 20;
        editor.spellcheck = false; editor.setAttribute('aria-label', `${label} configuration JSON`);
        panel.append(element('p', 'muted', 'Edit this configuration as JSON. Save writes config.yaml. Masked secrets are preserved.'), editor);
        if (key === 'embedding_profiles') {
          panel.replaceChildren(element('h2', '', label));
          const profilesRoot = element('div', 'settings-profiles');
          const advanced = element('details'); advanced.append(element('summary', '', 'Advanced JSON'), editor);
          const renderProfiles = () => {
            const profiles = JSON.parse(editor.value); profilesRoot.replaceChildren();
            profiles.forEach((profile, index) => {
              const card = element('section', 'settings-profile');
              const fields = element('div', 'knowledge-form');
              for (const [name, caption] of [['name', 'Name'], ['endpoint', 'Embeddings endpoint'], ['model', 'Model'], ['api_key_env', 'API key environment variable']]) {
                const label = element('label', 'knowledge-field', caption), input = element('input');
                input.value = profile[name] || ''; input.type = name === 'endpoint' ? 'url' : 'text';
                input.oninput = () => {profiles[index][name] = input.value; editor.value = JSON.stringify(profiles, null, 2);};
                label.append(input); fields.append(label);
              }
              const remove = element('button', 'quiet', 'Remove service');
              remove.onclick = () => {profiles.splice(index, 1); editor.value = JSON.stringify(profiles, null, 2); renderProfiles();};
              card.append(fields, remove); profilesRoot.append(card);
            });
          };
          const add = element('button', 'quiet', 'Add embedding service');
          add.onclick = () => {
            try {
              const profiles = JSON.parse(editor.value); profiles.push({id: `emb_${crypto.randomUUID().replaceAll('-', '')}`, name: 'New embedding service',
                endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings', model: 'text-embedding-v4', api_key_env: 'LOOM_LLM_API_KEY'});
              editor.value = JSON.stringify(profiles, null, 2); renderProfiles();
            } catch (error) {notice.textContent = error.message;}
          };
          editor.onchange = () => {try {renderProfiles();} catch (error) {notice.textContent = error.message;}};
          panel.append(profilesRoot, add, element('p', 'muted', 'Use an environment-variable name for credentials. Changing an indexed model requires a new service and index.'), advanced);
          renderProfiles();
        }
        panel.hidden = key !== (this.selected || 'general'); tab.setAttribute('aria-selected', String(!panel.hidden));
        tab.onclick = () => {
          this.selected = key;
          for (const entry of editors.values()) {entry.panel.hidden = entry.key !== key; entry.tab.setAttribute('aria-selected', String(entry.key === key));}
        };
        editors.set(key, {key, panel, tab, editor}); tabs.append(tab); body.append(panel);
      }
      const save = element('button', 'primary', 'Save configuration');
      save.onclick = async () => {
        save.disabled = true;
        try {
          const next = {};
          for (const [key, entry] of editors) {
            const value = JSON.parse(entry.editor.value);
            if (key === 'general') Object.assign(next, value); else next[key] = value;
          }
          const result = await this.api().json('/v1/settings', {signal, body: {revision: this.data.revision, config: next}});
          this.data = result;
          // Replace plaintext secrets entered in the editor with the server's masked view.
          for (const [key, entry] of editors) if (key !== 'general') entry.editor.value = JSON.stringify(result.config[key], null, 2);
          notice.textContent = `Saved to ${result.path}. New requests use the updated configuration; running calls keep their current settings.`;
          await this.changed?.();
        } catch (error) {notice.textContent = error.message;} finally {save.disabled = false;}
      };
      this.root.append(save);
    } catch (error) {if (!signal.aborted) notice.textContent = error.message;}
  }
}
