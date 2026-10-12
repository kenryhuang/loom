import {element} from '../markdown.mjs';

const button = (text, action, className = 'quiet') => {
  const node = element('button', className, text); node.type = 'button'; node.onclick = action; return node;
};
export const examples = {
  mcp: {transport: 'stdio', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '/path/to/project'], env: {}, timeout_seconds: 60},
  http: {transport: 'streamable_http', url: 'http://localhost:8000/mcp', headers_env: {}, timeout_seconds: 60},
  sse: {transport: 'sse', url: 'http://localhost:8000/sse', headers_env: {}, timeout_seconds: 60},
  cli: {command: 'rg', args: ['--', '{pattern}', '{path}'], input_mode: 'arguments', effect_kind: 'read_only',
    input_schema: {type: 'object', properties: {pattern: {type: 'string'}, path: {type: 'string'}}, required: ['pattern', 'path'], additionalProperties: false}, timeout_seconds: 60},
};

export function toolChoices(root, tools, selected = [], reasons = {}) {
  root.replaceChildren();
  if (!tools.length) root.append(element('p', 'muted', 'No tools registered. Add an MCP server or CLI command in External tools.'));
  for (const tool of tools) {
    const label = element('label', 'setup-collection'), check = element('input');
    check.type = 'checkbox'; check.value = tool.id; check.checked = selected.includes(tool.id);
    const info = element('span');
    info.append(element('strong', '', `${tool.connector_name} / ${tool.name}`), element('small', 'muted', tool.description),
      element('small', 'muted', `${tool.kind.toUpperCase()} · ${tool.effect_kind === 'read_only' ? 'Read only' : 'May change external state'}`));
    if (reasons[tool.id]) info.append(element('small', 'setup-reason', reasons[tool.id]));
    label.append(check, info); root.append(label);
  }
}
export const selectedTools = root => [...root.querySelectorAll('input:checked')].map(n => n.value);

export class ExternalToolsView {
  constructor(root, {api, registerButton, changed = () => {}}) {
    Object.assign(this, {root, api, registerButton, changed}); this.generation = 0; this.query = '';
    if (registerButton) registerButton.onclick = () => this.edit();
  }
  close() {this.generation++; if (this.registerButton) this.registerButton.disabled = true;}
  async open() {
    const generation = ++this.generation, api = this.api();
    if (this.registerButton) this.registerButton.disabled = true;
    this.root.replaceChildren(element('p', 'muted', 'Loading external tools…'));
    if (!api) {this.root.replaceChildren(element('p', 'muted', 'Connect to manage external tools.')); return;}
    try {
      const data = await api.json('/v1/external-tools');
      if (generation !== this.generation || api !== this.api()) return;
      this.data = data; this.render();
      if (this.registerButton) this.registerButton.disabled = false;
    } catch (error) {if (generation === this.generation) this.root.replaceChildren(element('p', 'notice error', error.message));}
  }
  render() {
    const root = this.root; root.replaceChildren();
    const header = element('div', 'dashboard-toolbar'); header.append(element('h1', '', 'External tools'),
      button('Refresh', () => this.open())); root.append(header);
    const toolbar = element('div', 'external-list-toolbar'), search = element('input');
    this.search = search;
    search.type = 'search'; search.placeholder = 'Search connections or tools…';
    search.setAttribute('aria-label', 'Search external tools'); search.value = this.query;
    search.oninput = () => {this.query = search.value; this.renderConnections();};
    this.count = element('span', 'muted small'); this.count.setAttribute('role', 'status');
    toolbar.append(search, this.count); root.append(toolbar);
    this.status = element('p', 'notice'); this.status.hidden = true; root.append(this.status);
    this.editor = element('section', 'dashboard-panel'); this.editor.hidden = true; root.append(this.editor);
    this.list = element('div', 'external-connections'); root.append(this.list); this.renderConnections();
  }
  renderConnections() {
    const list = this.list; list.replaceChildren();
    const query = this.query.trim().toLowerCase();
    const connections = this.data.connections.filter(item =>
      [item.name, item.description, item.kind, ...item.tools.flatMap(tool => [tool.name, tool.description])]
        .some(value => (value || '').toLowerCase().includes(query)));
    this.count.textContent = `${connections.length} / ${this.data.connections.length} connections`;
    if (!connections.length) list.append(element('p', 'empty-state', this.data.connections.length ? 'No matching connections.' : 'No external connections yet. Use Register to add one.'));
    for (const item of connections) {
      const card = element('section', 'dashboard-panel'), row = element('div', 'external-connection-heading');
      const actions = element('div', 'external-connection-actions');
      actions.append(
        button('Edit', () => this.edit(item)), button(item.enabled ? 'Disable' : 'Enable', () => this.mutate(`/v1/external-tools/${item.id}`, {
          name: item.name, description: item.description, kind: item.kind, config: item.config, enabled: !item.enabled,
        })));
      if (item.kind === 'mcp') actions.append(button('Discover tools', () => this.mutate(`/v1/external-tools/${item.id}/discover`, {})));
      row.append(element('h2', '', item.name), element('span', 'status', `${item.kind.toUpperCase()} · ${item.enabled ? 'Enabled' : 'Disabled'}`), actions);
      card.append(row, element('p', '', item.description), element('p', 'muted', `${item.tools.length} tools`));
      for (const tool of item.tools) {
        const detail = element('details'); detail.append(element('summary', '', `${tool.name} · ${tool.effect_kind}`),
          element('p', '', tool.description), element('pre', 'external-schema', JSON.stringify(tool.input_schema, null, 2))); card.append(detail);
      }
      list.append(card);
    }
  }
  edit(item) {
    const form = element('form', 'external-editor'); this.editor.replaceChildren(form); this.editor.hidden = false;
    form.append(element('h2', '', item ? 'Edit connection' : 'Register connection'));
    const fields = {};
    const field = (name, title, tag = 'input') => {const label = element('label', '', title), input = element(tag); input.name = name; label.append(input); form.append(label); return fields[name] = input;};
    field('name', 'Name').value = item?.name || ''; fields.name.required = true;
    field('description', 'Description used for task selection', 'textarea').value = item?.description || ''; fields.description.required = true;
    const kind = field('kind', 'Connection type', 'select');
    for (const [id, text] of [['mcp', 'MCP server'], ['cli', 'CLI command']]) {const o = element('option', '', text); o.value = id; kind.append(o);}
    kind.value = item?.kind || 'mcp';
    const config = field('config', 'Connection configuration (JSON)', 'textarea'); config.className = 'external-config';
    config.value = JSON.stringify(item?.config || examples.mcp, null, 2); config.required = true;
    kind.onchange = () => config.value = JSON.stringify(examples[kind.value], null, 2);
    const presets = element('div', 'trajectory-actions'); presets.append(button('MCP stdio example', () => {kind.value = 'mcp'; config.value = JSON.stringify(examples.mcp, null, 2);}),
      button('MCP HTTP example', () => {kind.value = 'mcp'; config.value = JSON.stringify(examples.http, null, 2);}),
      button('MCP SSE example', () => {kind.value = 'mcp'; config.value = JSON.stringify(examples.sse, null, 2);}),
      button('CLI example', () => {kind.value = 'cli'; config.value = JSON.stringify(examples.cli, null, 2);})); form.append(presets);
    form.append(element('p', 'muted small', 'env and headers_env reference service environment-variable names. CLI arguments may use whole placeholders such as {query}, or receive JSON on stdin.'));
    const save = element('button', 'primary', 'Save connection'); save.type = 'submit'; form.append(save, button('Cancel', () => this.editor.hidden = true));
    form.onsubmit = async event => {
      event.preventDefault();
      try {await this.mutate(`/v1/external-tools${item ? '/' + item.id : ''}`, {name: fields.name.value, description: fields.description.value,
        kind: kind.value, config: JSON.parse(config.value), enabled: item?.enabled ?? true});}
      catch (error) {this.status.hidden = false; this.status.textContent = error.message;}
    };
    fields.name.focus();
  }
  async mutate(path, body) {
    const api = this.api(), generation = this.generation;
    this.status.hidden = false; this.status.textContent = path.endsWith('/discover') ? 'Connecting and discovering tools…' : 'Saving…';
    if (this.registerButton) this.registerButton.disabled = true;
    this.search.disabled = true;
    this.root.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      await api.json(path, {body});
      if (generation !== this.generation || api !== this.api()) return;
      await this.changed(); await this.open();
    } catch (error) {if (generation === this.generation) {this.status.textContent = error.message; this.status.classList.add('error');}}
    finally {if (generation === this.generation) {
      this.root.querySelectorAll('button').forEach(b => b.disabled = false);
      this.search.disabled = false;
      if (this.registerButton) this.registerButton.disabled = false;
    }}
  }
}

export async function bindExternalTools({api, selected, save}) {
  const data = await api.json('/v1/external-tools');
  const dialog = element('dialog', 'external-bind-dialog'), form = element('form'), choices = element('div', 'external-choices'), error = element('p', 'notice error');
  form.append(element('h2', '', 'Session external tools'), choices, error); toolChoices(choices, data.tools, selected);
  const submit = element('button', 'primary', 'Apply tools'); submit.type = 'submit';
  form.append(submit, button('Cancel', () => dialog.close())); dialog.append(form); document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove(), {once: true});
  form.onsubmit = async event => {event.preventDefault(); submit.disabled = true;
    try {await save(selectedTools(choices)); dialog.close();} catch (e) {error.textContent = e.message;} finally {submit.disabled = false;}};
  dialog.showModal();
}
