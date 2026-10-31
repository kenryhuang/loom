import {element} from '../markdown.mjs';
import {selectedTools} from './external-tools.mjs';
import {agentResourceChoices} from './agent-resources.mjs';

const lines = value => String(value || '').split('\n').map(s => s.trim()).filter(Boolean);
const intervals = [['300','每 5 分钟'],['900','每 15 分钟'],['3600','每小时'],['21600','每 6 小时'],['86400','每天'],['604800','每周']];
const budgets = [['100000','10 万 token'],['250000','25 万 token'],['500000','50 万 token'],['1000000','100 万 token'],['5000000','500 万 token'],['10000000','1000 万 token'],['100000000','1 亿 token']];

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
function field(parent, name, title, value = '', {multiline = false, required = false} = {}) {
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
function selectField(parent, name, title, choices, selected = '') {
  const input = field(parent, name, title), select = element('select');
  select.id = input.id; select.name = name; input.replaceWith(select); options(select, choices, selected); return select;
}
function preset(parent, name, title, choices, value, {min = 1, max = 100000000, optional = false} = {}) {
  const initial = String(value ?? '');
  const choice = selectField(parent, name, title, [...choices, ['custom','自定义']], choices.some(c => c[0] === initial) ? initial : 'custom');
  const custom = field(parent, `${name}_custom`, `${title}（自定义数值）`, initial);
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
    header.append(element('h2', '', edit ? '修订 Agent' : 'New Agent'), button('取消', cancel)); form.append(header);

    const identity = block(form, '角色与长期目标').body;
    field(identity, 'name', '名称', r.name || '研发搭档', {required:true});
    field(identity, 'role', '角色描述', r.role_text || '中文输出，结论附依据。先查实现，再修改设计。', {multiline:true,required:true});
    field(identity, 'objective', '长期目标', objective.desired_outcome, {multiline:true,required:true});
    this.mode = selectField(identity, 'mode', '目标类型', [['ongoing','持续维护'],['finite','一次性目标']], objective.mode || 'ongoing');

    const environment = block(form, '工作目录与模型').body;
    field(environment, 'workspace', '工作目录', r.workspace || '');
    environment.append(element('p', 'muted small', '填写服务所在机器上已有目录的绝对路径。'));
    const models = element('div', 'setup-settings-grid'); environment.append(models);
    this.model = selectField(models, 'model', '执行与规划模型', [['','加载模型列表…']]);
    this.evaluationModel = selectField(models, 'evaluation_model', '评估模型', [['','加载模型列表…']]);

    const methods = block(form, '工作方法与资料来源').body;
    field(methods, 'methods', '默认方法（每行一条）', (r.methods || []).join('\n'), {multiline:true});
    field(methods, 'sources', '资料位置（相对工作目录，每行一个）', edit ? (r.source_routes?.[0]?.paths || []).join('\n') : 'src\ntests', {multiline:true});

    const timing = block(form, '运行节奏').body, grid = element('div', 'setup-settings-grid'); timing.append(grid);
    this.interval = preset(grid, 'interval', '目标检查频率', intervals, r.interval_seconds || 86400, {min:10,max:31536000});
    let zones = Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
    const zone = r.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    zones = [...new Set([zone, 'UTC', ...zones])];
    selectField(grid, 'timezone', '时区', zones.map(z => [z,z]), zone);
    this.schedule = preset(timing, 'schedule', '周期职责', [['','仅执行显式目标'],...intervals], objective.schedule?.every_seconds || '', {min:10,max:31536000,optional:true});
    this.criteria = field(timing, 'criteria', '周期职责验收条件（每行一条）', (objective.schedule?.acceptance || []).join('\n'), {multiline:true});
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
    timing.append(element('p', 'muted small', '目标检查用于推进已提交的目标；周期职责会按所选频率创建任务。'));

    const budget = block(form, '预算').body;
    budget.id = 'agent-budget-settings';
    this.daily = preset(budget, 'daily', '每日 token 上限', budgets, r.budget?.daily_tokens || 1000000);
    this.work = preset(budget, 'work', '单工作包 token 上限', [['daily_fraction','每日总量的 1/10（自动计算）'],...budgets], r.budget?.work_tokens ?? 'daily_fraction');
    this.goalBudget = preset(budget, 'goal_budget', '单目标累计 token 上限', budgets, r.budget?.goal_tokens || 100000000);
    this.maxCalls = preset(budget, 'max_calls', '每日模型调用次数', [['200','200 次'],['500','500 次'],['1000','1000 次'],['2000','2000 次'],['5000','5000 次']], r.budget?.max_calls || 2000);
    const budgetSummary = element('p','muted small'); budgetSummary.dataset.budgetSummary = '';
    const summarizeBudget = () => {
      const work = this.workTokens(), goal = this.goalBudget.value();
      budgetSummary.textContent = Number.isFinite(work) && Number.isFinite(goal) ?
        `单工作包：${work.toLocaleString()} token；单目标累计：${goal.toLocaleString()} token。${work > goal ? '工作包仍受较小的单目标累计上限限制。' : ''}` : '请填写有效的预算数值。';
    };
    budget.addEventListener('input',summarizeBudget); budget.addEventListener('change',summarizeBudget); summarizeBudget();
    budget.append(budgetSummary,element('p', 'muted small', '每日总量包含规划、执行、验收与学习。按比例选项在保存时计算为具体额度；已有 Session 的 token 上限需在暂停后同步调整。'));
    budget.append(element('p', 'muted small', '一个目标可拆成多个工作包。单目标额度累计计算规划、所有工作包的执行与验收，以及学习；单工作包额度只计算该工作包的执行与验收。两者均跨日累计，不会每日重置。'));
    this.tools = block(form, '外部工具', {folded:true,summary:true});
    this.toolList = element('div', 'external-choices'); this.toolList.setAttribute('role','group'); this.toolList.setAttribute('aria-label','外部工具');
    this.tools.body.append(this.toolList);
    this.knowledge = block(form, '知识库', {folded:true,summary:true});
    this.knowledgeList = element('div', 'knowledge-choices'); this.knowledgeList.setAttribute('role','group'); this.knowledgeList.setAttribute('aria-label','知识库');
    this.knowledge.body.append(this.knowledgeList);
    for (const target of [this.toolList, this.knowledgeList]) target.append(element('p','muted small','加载可选项…'));
    const summarize = () => {
      this.tools.count.textContent = `已选择 ${selectedTools(this.toolList).length} 项`;
      this.knowledge.count.textContent = `已选择 ${selectedTools(this.knowledgeList).length} 项`;
    };
    form.addEventListener('change', summarize); summarize();
    this.error = element('p', 'error'); this.error.setAttribute('role', 'alert');
    this.retry = button('重新加载选项', () => this.load()); this.retry.hidden = true;
    const actions = element('div', 'setup-form-actions');
    this.submit = element('button', 'primary', edit ? '保存新版本' : '创建 Agent'); this.submit.type = 'submit'; this.submit.disabled = true;
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
  workTokens() { return this.work.choice.value === 'daily_fraction' ? Math.max(1,Math.floor(this.daily.value()/10)) : this.work.value(); }
  close() {this.closed = true; this.abort.abort();}
  async load() {
    this.submit.disabled = true; this.retry.hidden = true; this.error.textContent = '';
    try {
      const [catalog, bases] = await Promise.all([
        this.api.json('/v1/web/catalog', {signal:this.abort.signal}), this.api.json('/v1/knowledge-bases', {signal:this.abort.signal}),
      ]);
      if (this.closed) return;
      const r = this.edit?.role || {};
      for (const [select, configured] of [[this.model,r.model],[this.evaluationModel,r.evaluation_model]]) {
        const value = catalog.model_aliases?.[configured] || configured || '';
        const choices = [['','使用服务默认模型'], ...(catalog.models || []).map(m => [m.id,m.label || m.id])];
        if (value && !choices.some(c => c[0] === value)) choices.push([value, `${value}（当前不可用）`]);
        options(select, choices, value);
      }
      agentResourceChoices(this.toolList, this.knowledgeList, catalog, bases, r);
      this.tools.count.textContent = `已选择 ${selectedTools(this.toolList).length} 项`;
      this.knowledge.count.textContent = `已选择 ${selectedTools(this.knowledgeList).length} 项`;
      this.submit.disabled = false;
    } catch (error) {
      if (this.closed || error.name === 'AbortError') return;
      this.error.textContent = `无法加载可选配置：${error.message}`; this.retry.hidden = false;
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
      budget:{...prior.budget,daily_tokens:this.daily.value(),work_tokens:this.workTokens(),goal_tokens:this.goalBudget.value(),max_calls:this.maxCalls.value()}};
    delete role.schema_version;
    const paths = lines(f.sources.value);
    if (role.source_routes.length) role.source_routes[0].paths = paths;
    else if (paths.length) role.source_routes.push({id:'implementation', topic:'实现资料', paths});
    for (const route of role.source_routes) for (const key of ['knowledge_base_ids','external_tool_ids']) {
      if (route[key]) route[key] = route[key].filter(id => role[key].includes(id));
    }
    const objective = role.long_term_objectives[0]; objective.desired_outcome = f.objective.value.trim(); objective.mode = f.mode.value;
    if (this.schedule.value() && objective.mode === 'ongoing') objective.schedule = {every_seconds:this.schedule.value(),acceptance:lines(f.criteria.value)};
    else delete objective.schedule;
    return role;
  }
}
