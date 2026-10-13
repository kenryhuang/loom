import {element} from '../markdown.mjs';

export const labelFor = key => key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
export const textField = (label, extra = {}) => ({label, type: 'text', ...extra});
export const numberField = (label, min = 1, extra = {}) => ({label, type: 'number', min, step: 1, ...extra});
export const choice = (label, options, extra = {}) => ({label, type: 'select', options, ...extra});
export const flag = label => ({label, type: 'boolean'});
export const modelChoice = label => ({label, type: 'model'});

export const modelFields = {
  provider: choice('Provider', [['openai', 'OpenAI compatible']], {placeholder: 'OpenAI compatible'}),
  model: textField('Model name', {required: true, placeholder: 'Provider model identifier'}),
  base_url: textField('API base URL', {type: 'url', placeholder: 'https://api.example.com/v1'}),
  api_key_env: textField('API key environment variable', {placeholder: 'LOOM_LLM_API_KEY'}),
  api_key: textField('API key', {type: 'password', help: 'Leave the masked value unchanged to retain the saved key. A direct key takes precedence over the environment variable.'}),
  temperature: numberField('Temperature', 0, {step: 'any', placeholder: 'Provider default'}),
  max_completion_tokens: numberField('Maximum output tokens'),
  context_window_tokens: numberField('Context window · tokens', 1024),
};
export const requestFields = {
  enable_thinking: flag('Enable thinking'), tool_stream: flag('Stream tool calls'), parallel_tool_calls: flag('Parallel tool calls'),
  reasoning_effort: choice('Reasoning effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh']),
  stream_options: {label: 'Stream options', fields: {include_usage: flag('Include usage')}},
};
export const taskFields = {
  objective: textField('Default objective', {type: 'multiline'}), workspace: textField('Workspace path'),
  profile: textField('Task profile', {placeholder: 'Use task default'}),
  expected_outputs: {label: 'Expected outputs', type: 'list'},
};
export const runFields = {
  plan_mode: choice('Planning mode', [['auto', 'Automatic'], ['force', 'Always plan'], ['off', 'Off']]),
  max_steps: numberField('Maximum steps'), timeout_ms: numberField('Run timeout · milliseconds'),
  tui: flag('Terminal UI'), stream: flag('Stream responses'),
  trace_path: textField('Trace file path'), trace_path_template: textField('Trace path template'),
};
export const metaFields = {
  proposer_model: modelChoice('Proposer model'), solver_model: modelChoice('Solver model'), judge_model: modelChoice('Judge model'),
  seed_analysis_version: choice('Seed analysis version', ['v1', 'v3']),
  search: {label: 'Candidate search', fields: {
    iterations: numberField('Iterations'), candidates_per_iteration: numberField('Candidates per iteration'),
    candidate_kinds: {label: 'Candidate kinds', type: 'list', options: ['declarative_patch', 'executable_component']},
    editable_surfaces: {label: 'Editable surfaces', type: 'list'},
  }},
  tasks: {label: 'Evaluation tasks', fields: {
    seed: numberField('Random seed', 1, {min: undefined}), repetitions: numberField('Repetitions'), minimum_pairs: numberField('Minimum pairs', 5),
    contamination_threshold: numberField('Contamination threshold', 0.01, {max: 0.8, step: 'any'}),
    split: {label: 'Dataset split · fractions must sum to 1', fields: Object.fromEntries(['discovery', 'validation', 'holdout'].map(k =>
      [k, numberField(labelFor(k), 0.01, {max: 1, step: 'any'})]))},
  }},
  objectives: {label: 'Objectives', fields: {
    primary: choice('Primary objective', [['task_success_rate', 'Task success rate'], ['total_tokens', 'Total tokens'], ['wall_time_ms', 'Execution time']]),
    constraints: {label: 'Regression limits', fields: {
      max_regression_rate: numberField('Maximum regression rate', 0, {step: 'any'}),
      max_cost_increase_ratio: numberField('Maximum cost increase ratio', 0, {step: 'any'}),
      max_latency_increase_ratio: numberField('Maximum latency increase ratio', 0, {step: 'any'}),
    }},
  }},
  budgets: {label: 'Budgets', fields: {
    max_candidates: numberField('Maximum candidates'), max_llm_calls: numberField('Maximum model calls'),
    max_wall_clock_minutes: numberField('Time budget · minutes'), max_cost_usd: numberField('Cost budget · USD', 0, {step: 'any'}),
  }},
  execution: {label: 'Trial execution', fields: {
    max_parallel_trials: numberField('Parallel trials'), trial_timeout_seconds: numberField('Trial timeout · seconds'),
    verifier_timeout_seconds: numberField('Verifier timeout · seconds'), stop_on_infrastructure_error: flag('Stop on infrastructure errors'),
  }},
  governance: {label: 'Promotion policy', fields: {
    mode: choice('Mode', ['local', 'production']), auto_promote_max_risk: choice('Automatic promotion risk', ['low']),
    require_approval_for: {label: 'Require approval for', type: 'list', options: ['medium', 'high', 'executable']},
  }},
};

export function action(label, handler) {
  const button = element('button', 'quiet', label); button.type = 'button'; button.onclick = handler; return button;
}
const typeOf = value => Array.isArray(value) ? 'list' : value !== null && typeof value === 'object' ? 'object' : typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : 'text';

// Fields read/write a draft lazily: displaying a default never adds it to config.
export function fields(root, read, write, specs = {}, context = {}, path = '', extensible = false) {
  const grid = element('div', 'settings-grid'); root.append(grid);
  const values = read() || {};
  for (const key of new Set([...Object.keys(specs), ...Object.keys(values)])) {
    const spec = specs[key] || {label: labelFor(key), type: typeOf(values[key])};
    const get = () => read()?.[key];
    const set = value => {
      const next = {...(read() || {})};
      if (value === undefined) delete next[key]; else Object.defineProperty(next, key, {value, enumerable: true, configurable: true, writable: true});
      write(next);
    };
    const name = path ? `${path}.${key}` : key;
    if (spec.fields || spec.type === 'object') {
      const group = element('details', 'settings-field-group'); group.open = !context.compact;
      group.append(element('summary', '', spec.label)); grid.append(group);
      fields(group, get, set, spec.fields || {}, context, name, extensible);
    } else if (spec.type === 'list') {
      const group = element('div', 'settings-list-field'); grid.append(group);
      group.append(element('span', 'settings-field-label', spec.label));
      const rows = element('div', 'settings-list'); group.append(rows);
      const render = () => {
        rows.replaceChildren();
        (get() || []).forEach((value, index) => {
          const row = element('div', 'settings-list-row'), holder = element('div');
          row.append(holder, action('Remove', () => {set(get().filter((_, i) => i !== index)); render();})); rows.append(row);
          const put = v => {const next = [...get()]; next[index] = v; set(next);};
          if (value !== null && typeof value === 'object') {
            if (Array.isArray(value)) fields(holder, () => ({items: get()[index]}), v => put(v.items), {items: {label: 'Items', type: 'list'}}, context, `${name}.${index}`, true);
            else fields(holder, () => get()[index], put, {}, context, `${name}.${index}`, true);
          } else control(holder, () => get()[index], put, {label: `${spec.label} ${index + 1}`, type: spec.options ? 'select' : typeOf(value), options: spec.options, required: true}, context, `${name}.${index}`);
        });
      };
      group.append(action('Add item', () => {set([...(get() || []), spec.options?.[0] || '']); render();})); render();
    } else control(grid, get, set, spec, context, name);
  }
  if (extensible) {
    const add = element('div', 'settings-add-property');
    const key = element('input'), type = element('select');
    key.placeholder = 'Parameter name'; key.setAttribute('aria-label', `${path} parameter name`);
    for (const [value, label] of [['text', 'Text'], ['number', 'Number'], ['boolean', 'On / off'], ['object', 'Group'], ['list', 'List']]) {
      const option = element('option', '', label); option.value = value; type.append(option);
    }
    type.setAttribute('aria-label', `${path} parameter type`);
    add.append(key, type, action('Add parameter', () => {
      const name = key.value.trim();
      key.setCustomValidity(!name ? 'Enter a parameter name.' : Object.hasOwn(read() || {}, name) || Object.hasOwn(specs, name) ? 'This parameter already exists.' : '');
      if (!key.reportValidity()) return;
      const next = {...(read() || {})};
      Object.defineProperty(next, name, {value: {text: '', number: 0, boolean: false, object: {}, list: []}[type.value], enumerable: true}); write(next);
      grid.remove(); add.remove(); fields(root, read, write, specs, context, path, extensible);
    }));
    key.oninput = () => key.setCustomValidity(''); root.append(add);
  }
}

function control(root, get, set, spec, context, name) {
  const wrap = element('label', 'settings-field'), caption = element('span', 'settings-field-label', spec.label);
  const select = ['model', 'select', 'boolean'].includes(spec.type);
  const input = element(select ? 'select' : spec.type === 'multiline' ? 'textarea' : 'input');
  input.name = name; input.setAttribute('aria-label', spec.label);
  if (select) {
    const blank = element('option', '', spec.placeholder || 'Use default'); blank.value = ''; input.append(blank);
    const options = spec.type === 'model' ? Object.keys(context.models()) : spec.type === 'boolean' ? [['true', 'Enabled'], ['false', 'Disabled']] : spec.options || [];
    for (const item of options) {
      const [value, label] = Array.isArray(item) ? item : [item, labelFor(item)];
      const option = element('option', '', label); option.value = value; input.append(option);
    }
    if (get() != null && ![...input.options].some(option => option.value === String(get()))) {
      const option = element('option', '', String(get())); option.value = String(get()); input.append(option);
    }
  } else if (spec.type === 'multiline') input.rows = 3;
  else {
    input.type = spec.type || 'text';
    input.placeholder = spec.placeholder || (spec.type === 'number' ? 'Use default' : '');
    if (spec.type === 'password') input.autocomplete = 'new-password';
    for (const key of ['min', 'max', 'step']) if (spec[key] !== undefined) input[key] = spec[key];
    if (spec.type === 'number' && spec.step === undefined) input.step = 'any';
  }
  input.value = get() == null ? '' : String(get()); input.required = Boolean(spec.required);
  input.oninput = input.onchange = () => {
    if (input.value === '') set(spec.required ? '' : undefined);
    else if (spec.type === 'number') {if (Number.isFinite(input.valueAsNumber)) set(input.valueAsNumber);}
    else set(spec.type === 'boolean' ? input.value === 'true' : input.value);
    context.dirty?.();
  };
  wrap.append(caption, input);
  if (spec.help) wrap.append(element('small', 'muted', spec.help));
  root.append(wrap);
}
