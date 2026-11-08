import {element} from '../markdown.mjs';
import {selectedTools} from './external-tools.mjs';
import {agentResourceChoices} from './agent-resources.mjs';

const lines = value => String(value || '').split('\n').map(s => s.trim()).filter(Boolean);
const intervals = [['300','Every 5 minutes'],['900','Every 15 minutes'],['3600','Every hour'],['21600','Every 6 hours'],['86400','Every day'],['604800','Every week']];
const budgets = [['100000','100k tokens'],['250000','250k tokens'],['500000','500k tokens'],['1000000','1M tokens'],['5000000','5M tokens'],['10000000','10M tokens'],['100000000','100M tokens']];

function button(title, action, className = 'quiet') {
  const node = element('button', className, title); node.type = 'button'; node.onclick = action; return node;
}
function block(form, title, {folded = false, summary = false} = {}) {
  const node = element(folded ? 'details' : 'section', 'setup-block');
  const heading = element(folded ? 'summary' : 'h3'); heading.append(element('span', '', title));
  const count = summary ? element('span', 'setup-selection-summary') : null;
  if (count) heading.append(count);
  const body = folded ? element('div', 'setup-block-body') : node;
  node.append(heading); if (folded) node.append(body); form.append(node);
  return {node, body, count};
}
export function field(parent, name, title, value = '', {multiline = false, required = false} = {}) {
  const wrap = element('div', 'agent-setup-field'), label = element('label', '', title);
  const input = element(multiline ? 'textarea' : 'input'); input.id = `agent-create-${name}`; input.name = name;
  label.htmlFor = input.id; input.value = value ?? ''; input.required = required;
  if (multiline) input.rows = 3;
  wrap.append(label, input); parent.append(wrap); return input;
}
function options(select, choices, selected = '') {
  select.replaceChildren();
  for (const [value, title] of choices) {
    const option = element('option', '', title); option.value = String(value); select.append(option);
  }
  select.value = String(selected ?? '');
}
export function selectField(parent, name, title, choices, selected = '') {
  const input = field(parent, name, title), select = element('select');
  select.id = input.id; select.name = name; input.replaceWith(select); options(select, choices, selected); return select;
}
export function preset(parent, name, title, choices, value, {min = 1, max = 100000000, optional = false} = {}) {
  const initial = String(value ?? '');
  const choice = selectField(parent, name, title, [...choices, ['custom','Custom']], choices.some(c => c[0] === initial) ? initial : 'custom');
  const custom = field(parent, `${name}_custom`, `${title} (custom value)`, initial);
  custom.type = 'number'; custom.min = min; custom.max = max; custom.step = 1;
  const update = () => {custom.parentElement.hidden = choice.value !== 'custom'; custom.disabled = choice.value !== 'custom'; custom.required = choice.value === 'custom';};
  choice.addEventListener('change', update); update();
  return {choice, update, value: () => optional && choice.value === '' ? null : Number(choice.value === 'custom' ? custom.value : choice.value)};
}

/** Uses New Session's setup blocks, settings grid, resource choices and actions. */
export class AgentSetupView {
  constructor(root, {api, edit = null, save, cancel}) {
    Object.assign(this, {root, api, edit, save});
    this.abort = new AbortController(); this.closed = false;
    const r = edit?.role || {}, objective = r.long_term_objectives?.[0] || {};
    const form = this.form = element('form', 'setup-form agent-setup-form');
    const header = element('div', 'dialog-heading');
    header.append(element('h2', '', edit ? 'Revise Agent' : 'New Agent'), button('Cancel', cancel)); form.append(header);

    const identity = block(form, 'Role & long-term objective').body;
    field(identity, 'name', 'Name', r.name || 'Engineering partner', {required:true});
    field(identity, 'role', 'Role description', r.role_text || 'Respond in English and back conclusions with evidence. Check the implementation before updating the design.', {multiline:true,required:true});
    field(identity, 'objective', 'Long-term objective', objective.desired_outcome, {multiline:true,required:true});
    this.mode = selectField(identity, 'mode', 'Objective type', [['ongoing','Ongoing maintenance'],['finite','One-time goal']], objective.mode || 'ongoing');
    const stage = block(form, 'Autonomous stage planning').body, policy = objective.stage_planning || {};
    this.stageMode = selectField(stage, 'stage_mode', 'Plan stages from the long-term objective and execute them', [['off','Off'],['autonomous','Auto-progress']], policy.mode || 'off');
    stage.append(element('p','muted','Verifies the current state first, then splits near-term stages; the next stage starts automatically after acceptance. Uses the current workspace, permissions, and budget.'));
    field(stage, 'stage_constraints', 'Stage planning constraints (one per line)', (policy.constraints || []).join('\n'), {multiline:true});
    field(stage, 'stage_completion', 'Final completion criteria (required for finite objectives)', (policy.completion_criteria || []).join('\n'), {multiline:true});

    const environment = block(form, 'Workspace & models').body;
    field(environment, 'workspace', 'Workspace', r.workspace || '');
    environment.append(element('p', 'muted small', 'Absolute path of an existing directory on the machine running the service.'));
    const models = element('div', 'setup-settings-grid'); environment.append(models);
    this.model = selectField(models, 'model', 'Execution & planning model', [['','Loading model list…']]);
    this.evaluationModel = selectField(models, 'evaluation_model', 'Evaluation model', [['','Loading model list…']]);

    const methods = block(form, 'Methods & sources').body;
    field(methods, 'methods', 'Default methods (one per line)', (r.methods || []).join('\n'), {multiline:true});
    field(methods, 'sources', 'Source paths (relative to the workspace, one per line)', edit ? (r.source_routes?.[0]?.paths || []).join('\n') : 'src\ntests', {multiline:true});

    const timing = block(form, 'Cadence').body, grid = element('div', 'setup-settings-grid'); timing.append(grid);
    this.interval = preset(grid, 'interval', 'Goal check frequency', intervals, r.interval_seconds || 86400, {min:10,max:31536000});
    const scheduling=r.scheduling_policy || {};
    this.queueMode=selectField(grid,'queue_mode','Scheduled runs',[['true','On (default)'],['false','Manual only']],String(scheduling.enabled ?? true));
    this.queueTick=preset(grid,'queue_tick','Scheduler tick interval (seconds)',[['60','1 minute'],['300','5 minutes'],['900','15 minutes'],['3600','1 hour']],scheduling.tick_seconds ?? 60,{min:1,max:31536000});
    this.recoveryAttempts=preset(grid,'recovery_attempts','Auto-recovery attempts per task',[['0','No auto-recovery'],['3','3'],['5','5']],scheduling.max_recovery_attempts ?? 3,{min:0,max:10});
    timing.append(element('p','muted small','Running tasks are never interrupted; on blockage the Agent tries recovery first and asks for human help only if recovery fails. Each dispatch / recovery run is capped at 1 hour; new goal planning starts only after the queue drains.'));
    let zones = Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
    const zone = r.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    zones = [...new Set([zone, 'UTC', ...zones])];
    selectField(grid, 'timezone', 'Timezone', zones.map(z => [z,z]), zone);
    this.schedule = preset(timing, 'schedule', 'Recurring duty', [['','Only explicit goals'],...intervals], objective.schedule?.every_seconds || '', {min:10,max:31536000,optional:true});
    this.criteria = field(timing, 'criteria', 'Recurring duty acceptance criteria (one per line)', (objective.schedule?.acceptance || []).join('\n'), {multiline:true});
    const scheduleChanged = () => {
      const enabled = this.mode.value === 'ongoing';
      this.schedule.choice.disabled = !enabled;
      if (!enabled) {this.schedule.choice.value = ''; this.schedule.update();}
      const active = enabled && this.schedule.choice.value !== '';
      this.criteria.parentElement.hidden = !active; this.criteria.disabled = !active; this.criteria.required = active;
    };
    this.mode.onchange = scheduleChanged; this.schedule.choice.addEventListener('change', () => {
      const active = this.mode.value === 'ongoing' && this.schedule.choice.value !== '';
      this.criteria.parentElement.hidden = !active; this.criteria.disabled = !active; this.criteria.required = active;
    }); scheduleChanged();
    timing.append(element('p', 'muted small', 'Goal checks advance submitted goals; a recurring duty creates tasks at the chosen frequency.'));

    const budget = block(form, 'Budget').body;
    budget.id = 'agent-budget-settings';
    this.daily = preset(budget, 'daily', 'Daily token limit', budgets, r.budget?.daily_tokens || 1000000);
    budget.append(element('p', 'muted small', 'Planning, execution, acceptance, and learning share one daily token total. Usage resets at midnight in the chosen timezone; history is kept, and long-term objectives, goals, and work packages have no cumulative token cap.'));
    const learning = block(form, 'Learning & Deep Eval recovery').body;
    this.learningResumes = preset(learning, 'learning_resumes', 'Max auto-resumes per evaluation per day', [['0','0'],['1','1'],['2','2'],['3','3'],['5','5']], r.learning_policy?.max_resumes ?? 3, {min:0,max:10});
    this.learningAttempts=preset(learning,'learning_attempts','Max analysis attempts per evaluation',[['3','3'],['6','6'],['10','10']],r.learning_policy?.max_attempts ?? 6,{min:1,max:20});
    this.learningCalls=preset(learning,'learning_eval_calls','Calls expansion cap per evaluation',[['100','100'],['250','250'],['500','500']],r.learning_policy?.max_evaluation_calls ?? 500,{min:1,max:500});
    learning.append(element('p','muted small','Learning uses the shared daily allowance and waits for the next day once it is exhausted. Auto-resume stops when there is no progress; completed analyses and memories are kept.'));
    const spec = r.task_spec || {};
    this.exec = block(form, 'Execution configuration', {folded:true, summary:true});
    this.exec.body.append(element('p', 'muted small', 'Each dispatch recommends tools and a planning mode from the task description; when recommendation fails it falls back to this configuration. Leave everything empty to follow system defaults; changes apply to the next dispatch.'));
    const recommendRow = element('div', 'setup-form-actions');
    this.recommendButton = button('Recommend for me', () => this.recommend()); this.recommendButton.disabled = true;
    recommendRow.append(this.recommendButton);
    this.recommendStatus = element('p', 'muted small');
    this.exec.body.append(recommendRow, this.recommendStatus);
    this.execList = element('div', 'external-choices'); this.execList.setAttribute('role', 'group'); this.execList.setAttribute('aria-label', 'Tool collections');
    this.execList.append(element('p', 'muted small', 'Loading choices…'));
    this.exec.body.append(this.execList);
    const execGrid = element('div', 'setup-settings-grid'); this.exec.body.append(execGrid);
    this.planMode = selectField(execGrid, 'plan_mode', 'Planning mode', [['auto','Auto (default)'],['force','Forced planning'],['off','Direct execution']], r.plan_mode || 'auto');
    this.contextPlugin = selectField(execGrid, 'context_plugin', 'Context plugin', [['','System default']], spec.context?.plugin || '');
    this.workflowPlugin = selectField(execGrid, 'workflow_plugin', 'Workflow plugin', [['','System default']], spec.workflow?.plugin || '');
    this.execChoicesLoaded = false;
    this.tools = block(form, 'External tools', {folded:true,summary:true});
    this.toolList = element('div', 'external-choices'); this.toolList.setAttribute('role','group'); this.toolList.setAttribute('aria-label','External tools');
    this.tools.body.append(this.toolList);
    this.knowledge = block(form, 'Knowledge bases', {folded:true,summary:true});
    this.knowledgeList = element('div', 'knowledge-choices'); this.knowledgeList.setAttribute('role','group'); this.knowledgeList.setAttribute('aria-label','Knowledge bases');
    this.knowledge.body.append(this.knowledgeList);
    for (const target of [this.toolList, this.knowledgeList]) target.append(element('p','muted small','Loading choices…'));
    const summarize = () => {
      this.tools.count.textContent = `${selectedTools(this.toolList).length} selected`;
      this.knowledge.count.textContent = `${selectedTools(this.knowledgeList).length} selected`;
      this.updateExecSummary();
    };
    this.refreshSummaries = summarize;
    form.addEventListener('change', summarize); summarize();
    this.error = element('p', 'error'); this.error.setAttribute('role', 'alert');
    this.retry = button('Reload choices', () => this.load()); this.retry.hidden = true;
    const actions = element('div', 'setup-form-actions');
    this.submit = element('button', 'primary', edit ? 'Save new version' : 'Create Agent'); this.submit.type = 'submit'; this.submit.disabled = true;
    actions.append(this.submit); form.append(this.error, this.retry, actions);
    form.addEventListener('invalid', event => {
      for (let node = event.target.closest('details'); node; node = node.parentElement?.closest('details')) node.open = true;
    }, true);
    form.onsubmit = async event => {
      event.preventDefault(); if (this.submit.disabled || !form.reportValidity()) return;
      this.submit.disabled = true; this.error.textContent = '';
      try {await this.save(this.payload());}
      catch (error) {if (!this.closed) this.error.textContent = error.message;}
      finally {if (!this.closed) this.submit.disabled = false;}
    };
    root.replaceChildren(form);
  }
  close() {this.closed = true; this.abort.abort();}
  updateExecSummary() {
    const count = selectedTools(this.execList).length;
    this.exec.count.textContent = count ? `${count} selected` : 'Follow system defaults';
  }
  async recommend() {
    const f = this.form.elements;
    const description = [f.role.value.trim(), f.objective.value.trim()].filter(Boolean).join('\n');
    if (!description) {this.recommendStatus.textContent = 'Fill in the role description or long-term objective before requesting a recommendation.'; return;}
    this.recommendButton.disabled = true; this.recommendStatus.textContent = 'Recommending…';
    try {
      const result = await this.api.json('/v1/agents/setup/recommend', {body:{description, model:f.model.value || null}, signal:this.abort.signal});
      if (this.closed) return;
      this.applyRecommendation(result);
    } catch (error) {
      if (this.closed || error.name === 'AbortError') return;
      this.recommendStatus.textContent = `Recommendation failed: ${error.message}; adjust manually or retry.`;
    } finally {
      if (!this.closed) this.recommendButton.disabled = !(this.catalog?.setup_recommendation && this.catalog?.models?.length);
    }
  }
  applyRecommendation(result) {
    const spec = result.task_spec || {}, chosen = spec.tools?.collections || [];
    for (const check of this.execList.querySelectorAll('input')) check.checked = chosen.includes(check.value);
    for (const [select, value] of [[this.contextPlugin, spec.context?.plugin],[this.workflowPlugin, spec.workflow?.plugin]])
      if (value && [...select.options].some(o => o.value === value)) select.value = value;
    this.planMode.value = result.plan_mode || 'auto';
    for (const [list, ids] of [[this.knowledgeList, result.knowledge_base_ids || []],[this.toolList, result.external_tool_ids || []]])
      for (const check of list.querySelectorAll('input')) if (ids.includes(check.value)) check.checked = true;
    const reasons = Object.entries(result.collection_reasons || {}).map(([key, reason]) => `${key}: ${reason}`).join('; ');
    this.recommendStatus.textContent = (result.rationale || 'Recommended from the role description; review before saving.') + (reasons ? ` (${reasons})` : '');
    this.refreshSummaries();
  }
  async load() {
    this.submit.disabled = true; this.retry.hidden = true; this.error.textContent = '';
    try {
      const [catalog, bases] = await Promise.all([
        this.api.json('/v1/web/catalog', {signal:this.abort.signal}), this.api.json('/v1/knowledge-bases', {signal:this.abort.signal}),
      ]);
      if (this.closed) return;
      this.catalog = catalog;
      const r = this.edit?.role || {};
      for (const [select, configured] of [[this.model,r.model],[this.evaluationModel,r.evaluation_model]]) {
        const value = catalog.model_aliases?.[configured] || configured || '';
        const choices = [['','Use service default model'], ...(catalog.models || []).map(m => [m.id,m.label || m.id])];
        if (value && !choices.some(c => c[0] === value)) choices.push([value, `${value} (currently unavailable)`]);
        options(select, choices, value);
      }
      agentResourceChoices(this.toolList, this.knowledgeList, catalog, bases, r);
      this.execChoicesLoaded = Array.isArray(catalog.tool_collections);
      this.execList.replaceChildren();
      if (!this.execChoicesLoaded) this.execList.append(element('p', 'muted small', 'Loading choices…'));
      else if (!catalog.tool_collections.length) this.execList.append(element('p', 'muted small', 'No tool collections available.'));
      const chosen = r.task_spec?.tools?.collections || [];
      for (const collection of catalog.tool_collections || []) {
        const row = element('label', 'setup-collection'), check = element('input'), info = element('span');
        check.type = 'checkbox'; check.value = collection.id; check.checked = chosen.includes(collection.id);
        info.append(element('strong', '', collection.label || collection.id), element('small', 'muted', collection.description || ''));
        row.append(check, info); this.execList.append(row);
      }
      for (const [select, list, configured] of [[this.contextPlugin, catalog.plugins?.context, r.task_spec?.context?.plugin],[this.workflowPlugin, catalog.plugins?.workflow, r.task_spec?.workflow?.plugin]]) {
        const choices = [['','System default'], ...(list || []).map(p => [p.id, p.label || p.id])];
        if (configured && !choices.some(c => c[0] === configured)) choices.push([configured, `${configured} (currently unavailable)`]);
        options(select, choices, configured || '');
      }
      this.recommendButton.disabled = !(catalog.setup_recommendation && catalog.models.length);
      this.updateExecSummary();
      this.tools.count.textContent = `${selectedTools(this.toolList).length} selected`;
      this.knowledge.count.textContent = `${selectedTools(this.knowledgeList).length} selected`;
      this.submit.disabled = false;
    } catch (error) {
      if (this.closed || error.name === 'AbortError') return;
      this.error.textContent = `Could not load configuration choices: ${error.message}`; this.retry.hidden = false;
    }
  }
  payload() {
    const f = this.form.elements, prior = this.edit?.role || {}, workspace = f.workspace.value.trim() || null;
    const role = {...prior, name:f.name.value.trim(), role_text:f.role.value.trim(), workspace,
      working_directory:workspace === prior.workspace ? prior.working_directory : workspace,
      long_term_objectives:this.edit ? structuredClone(prior.long_term_objectives) : [{id:'primary'}],
      source_routes:structuredClone(prior.source_routes || []), methods:lines(f.methods.value),
      interval_seconds:this.interval.value(), timezone:f.timezone.value, model:f.model.value || null, evaluation_model:f.evaluation_model.value || null,
      knowledge_base_ids:selectedTools(this.knowledgeList), external_tool_ids:selectedTools(this.toolList),
      scheduling_policy:{...prior.scheduling_policy,enabled:this.queueMode.value==='true',tick_seconds:this.queueTick.value(),
        max_recovery_attempts:this.recoveryAttempts.value()},
      learning_policy:{...prior.learning_policy,max_resumes:this.learningResumes.value(),
        max_attempts:this.learningAttempts.value(),max_evaluation_calls:this.learningCalls.value()},
      budget:{...prior.budget,daily_tokens:this.daily.value()}};
    delete role.schema_version;
    role.plan_mode = f.plan_mode.value || 'auto';
    if (this.execChoicesLoaded) {
      // Merge over the prior spec: the form manages tools.collections and the
      // context/workflow plugins; every other key (session_environment, outputs,
      // acceptance, execution_runtime, tools.budget, …) is preserved untouched.
      const spec = prior.task_spec && typeof prior.task_spec === 'object' ? structuredClone(prior.task_spec) : {};
      spec.schema_version = 1;
      const collections = selectedTools(this.execList);
      if (collections.length) (spec.tools = spec.tools || {}).collections = collections;
      else if (spec.tools) delete spec.tools.collections;
      for (const [kind, plugin] of [['context', f.context_plugin.value], ['workflow', f.workflow_plugin.value]]) {
        if (plugin) spec[kind] = {...(spec[kind] || {}), plugin};
        else delete spec[kind];
      }
      if (spec.tools && !Object.keys(spec.tools).length) delete spec.tools;
      role.task_spec = Object.keys(spec).some(key => key !== 'schema_version') ? spec : null;
    }
    const paths = lines(f.sources.value);
    if (role.source_routes.length) role.source_routes[0].paths = paths;
    else if (paths.length) role.source_routes.push({id:'implementation', topic:'Implementation sources', paths});
    for (const route of role.source_routes) for (const key of ['knowledge_base_ids','external_tool_ids']) {
      if (route[key]) route[key] = route[key].filter(id => role[key].includes(id));
    }
    const objective = role.long_term_objectives[0]; objective.desired_outcome = f.objective.value.trim(); objective.mode = f.mode.value;
    if (this.stageMode.value === 'autonomous' || objective.stage_planning) objective.stage_planning = {
      ...objective.stage_planning,mode:this.stageMode.value,
      constraints:lines(f.stage_constraints.value),completion_criteria:lines(f.stage_completion.value),
    };
    if (this.schedule.value() && objective.mode === 'ongoing') objective.schedule = {every_seconds:this.schedule.value(),acceptance:lines(f.criteria.value)};
    else delete objective.schedule;
    return role;
  }
}
