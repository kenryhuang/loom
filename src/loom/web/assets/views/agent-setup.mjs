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
    const stage = block(form, '自主阶段规划').body, policy = objective.stage_planning || {};
    this.stageMode = selectField(stage, 'stage_mode', '根据长期目标规划阶段并执行', [['off','关闭'],['autonomous','自动推进']], policy.mode || 'off');
    stage.append(element('p','muted','先核实现状，再拆分近期阶段；验收后自动推进下一阶段。沿用当前工作目录、权限和预算。'));
    this.stageBudget = preset(stage, 'stage_budget', '长期目标初始累计额度', budgets, policy.objective_tokens || 10000000, {min:1,max:100000000});
    field(stage, 'stage_constraints', '阶段规划约束（每行一条）', (policy.constraints || []).join('\n'), {multiline:true});
    field(stage, 'stage_completion', '最终完成条件（有限目标必填）', (policy.completion_criteria || []).join('\n'), {multiline:true});

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
    const scheduling=r.scheduling_policy || {};
    this.queueMode=selectField(grid,'queue_mode','定时调度',[['true','开启（默认）'],['false','仅手动调度']],String(scheduling.enabled ?? true));
    this.queueTick=preset(grid,'queue_tick','调度检查间隔（秒）',[['60','1 分钟'],['300','5 分钟'],['900','15 分钟'],['3600','1 小时']],scheduling.tick_seconds ?? 60,{min:1,max:31536000});
    this.recoveryAttempts=preset(grid,'recovery_attempts','每任务自动恢复次数上限',[['0','不自动恢复'],['3','3 次'],['5','5 次']],scheduling.max_recovery_attempts ?? 3,{min:0,max:10});
    this.workExpansion=selectField(grid,'work_expansion','工作包预算自动扩容',[['true','开启'],['false','关闭']],String(scheduling.auto_expand_work_budget ?? true));
    this.workCeiling=preset(grid,'work_ceiling','单工作包自动分配 / 扩容上限（token）',budgets,scheduling.max_work_tokens ?? Math.min(r.budget?.daily_tokens ?? 10000000,r.budget?.goal_tokens ?? 10000000),{min:1,max:100000000});
    this.objectiveExpansion=selectField(grid,'objective_expansion','长期目标预算自动扩容',[['true','开启'],['false','关闭']],String(scheduling.auto_expand_objective_budget ?? true));
    this.objectiveCeiling=preset(grid,'objective_ceiling','长期目标自动扩容上限（token）',budgets,scheduling.max_objective_tokens ?? r.budget?.goal_tokens ?? 10000000,{min:1,max:100000000});
    timing.append(element('p','muted small','运行中的任务不打断；阻塞时先尝试恢复，无法恢复则请求人工介入。每次派发 / 恢复的执行时间上限为 1 小时；队列全部完成后才进入新目标规划。'));
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
    const learning = block(form, '学习与 Deep Eval 预算').body;
    this.learning = preset(learning, 'learning', '每日初始学习额度', budgets, r.budget?.learning_tokens ?? 250000);
    this.learningAuto = selectField(learning, 'learning_auto', '预算不足时', [['true','自动扩容并续跑'],['false','等待下一预算窗口']], String(r.learning_policy?.auto_expand ?? true));
    this.learningMax = preset(learning, 'learning_max', '学习额度扩容上限', [['auto','初始学习额度的 4 倍（受每日总额限制）'],...budgets], r.learning_policy?.max_learning_tokens ?? 'auto');
    this.learningExpansions = preset(learning, 'learning_expansions', '每天最多扩容次数', [['0','0 次'],['1','1 次'],['2','2 次'],['3','3 次'],['5','5 次']], r.learning_policy?.max_expansions ?? 2, {min:0,max:10});
    this.learningResumes = preset(learning, 'learning_resumes', '每个评估每天最多自动续跑次数', [['0','0 次'],['1','1 次'],['2','2 次'],['3','3 次'],['5','5 次']], r.learning_policy?.max_resumes ?? 3, {min:0,max:10});
    this.learningAttempts=preset(learning,'learning_attempts','每个评估累计分析次数上限',[['3','3 次'],['6','6 次'],['10','10 次']],r.learning_policy?.max_attempts ?? 6,{min:1,max:20});
    this.learningCalls=preset(learning,'learning_eval_calls','单个评估 Calls 扩容上限',[['100','100 次'],['250','250 次'],['500','500 次']],r.learning_policy?.max_evaluation_calls ?? 500,{min:1,max:500});
    learning.append(element('p','muted small','每次扩容最多翻倍，仅对当天有效。每日总额、目标累计额度和调用次数上限保持有效；无进展时停止自动续跑，已完成分析与记忆会保留。'));
    const spec = r.task_spec || {};
    this.exec = block(form, '执行配置', {folded:true, summary:true});
    this.exec.body.append(element('p', 'muted small', '每次派发会按任务描述动态推荐工具与规划模式；推荐失败时回落到此处的兜底配置。全部留空则跟随系统默认，修改后下一次调度生效。'));
    const recommendRow = element('div', 'setup-form-actions');
    this.recommendButton = button('智能推荐', () => this.recommend()); this.recommendButton.disabled = true;
    recommendRow.append(this.recommendButton);
    this.recommendStatus = element('p', 'muted small');
    this.exec.body.append(recommendRow, this.recommendStatus);
    this.execList = element('div', 'external-choices'); this.execList.setAttribute('role', 'group'); this.execList.setAttribute('aria-label', '工具集合');
    this.execList.append(element('p', 'muted small', '加载可选项…'));
    this.exec.body.append(this.execList);
    const execGrid = element('div', 'setup-settings-grid'); this.exec.body.append(execGrid);
    this.planMode = selectField(execGrid, 'plan_mode', '规划模式', [['auto','自动（默认）'],['force','强制规划'],['off','直接执行']], r.plan_mode || 'auto');
    this.contextPlugin = selectField(execGrid, 'context_plugin', '上下文插件', [['','系统默认']], spec.context?.plugin || '');
    this.workflowPlugin = selectField(execGrid, 'workflow_plugin', '工作流插件', [['','系统默认']], spec.workflow?.plugin || '');
    this.execChoicesLoaded = false;
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
      this.updateExecSummary();
    };
    this.refreshSummaries = summarize;
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
  updateExecSummary() {
    const count = selectedTools(this.execList).length;
    this.exec.count.textContent = count ? `已选择 ${count} 项` : '跟随系统默认';
  }
  async recommend() {
    const f = this.form.elements;
    const description = [f.role.value.trim(), f.objective.value.trim()].filter(Boolean).join('\n');
    if (!description) {this.recommendStatus.textContent = '请先填写角色描述或长期目标，再请求推荐。'; return;}
    this.recommendButton.disabled = true; this.recommendStatus.textContent = '正在推荐…';
    try {
      const result = await this.api.json('/v1/agents/setup/recommend', {body:{description, model:f.model.value || null}, signal:this.abort.signal});
      if (this.closed) return;
      this.applyRecommendation(result);
    } catch (error) {
      if (this.closed || error.name === 'AbortError') return;
      this.recommendStatus.textContent = `推荐失败：${error.message}；可手工调整或重试。`;
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
    const reasons = Object.entries(result.collection_reasons || {}).map(([key, reason]) => `${key}：${reason}`).join('；');
    this.recommendStatus.textContent = (result.rationale || '已按角色描述推荐，请核对后保存。') + (reasons ? `（${reasons}）` : '');
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
        const choices = [['','使用服务默认模型'], ...(catalog.models || []).map(m => [m.id,m.label || m.id])];
        if (value && !choices.some(c => c[0] === value)) choices.push([value, `${value}（当前不可用）`]);
        options(select, choices, value);
      }
      agentResourceChoices(this.toolList, this.knowledgeList, catalog, bases, r);
      this.execChoicesLoaded = Array.isArray(catalog.tool_collections);
      this.execList.replaceChildren();
      if (!this.execChoicesLoaded) this.execList.append(element('p', 'muted small', '加载可选项…'));
      else if (!catalog.tool_collections.length) this.execList.append(element('p', 'muted small', '暂无可用工具集合。'));
      const chosen = r.task_spec?.tools?.collections || [];
      for (const collection of catalog.tool_collections || []) {
        const row = element('label', 'setup-collection'), check = element('input'), info = element('span');
        check.type = 'checkbox'; check.value = collection.id; check.checked = chosen.includes(collection.id);
        info.append(element('strong', '', collection.label || collection.id), element('small', 'muted', collection.description || ''));
        row.append(check, info); this.execList.append(row);
      }
      for (const [select, list, configured] of [[this.contextPlugin, catalog.plugins?.context, r.task_spec?.context?.plugin],[this.workflowPlugin, catalog.plugins?.workflow, r.task_spec?.workflow?.plugin]]) {
        const choices = [['','系统默认'], ...(list || []).map(p => [p.id, p.label || p.id])];
        if (configured && !choices.some(c => c[0] === configured)) choices.push([configured, `${configured}（当前不可用）`]);
        options(select, choices, configured || '');
      }
      this.recommendButton.disabled = !(catalog.setup_recommendation && catalog.models.length);
      this.updateExecSummary();
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
      scheduling_policy:{enabled:this.queueMode.value==='true',tick_seconds:this.queueTick.value(),max_recovery_attempts:this.recoveryAttempts.value(),
        auto_expand_work_budget:this.workExpansion.value==='true',max_work_tokens:this.workCeiling.value(),
        auto_expand_objective_budget:this.objectiveExpansion.value==='true',max_objective_tokens:this.objectiveCeiling.value()},
      learning_policy:{...prior.learning_policy,auto_expand:this.learningAuto.value === 'true',
        max_learning_tokens:this.learningMax.choice.value === 'auto' ? Math.min(this.daily.value(),this.learning.value()*4) : this.learningMax.value(),
        max_expansions:this.learningExpansions.value(),max_resumes:this.learningResumes.value(),max_attempts:this.learningAttempts.value(),max_evaluation_calls:this.learningCalls.value()},
      budget:{...prior.budget,daily_tokens:this.daily.value(),work_tokens:this.workTokens(),goal_tokens:this.goalBudget.value(),max_calls:this.maxCalls.value(),
        ...(prior.budget?.learning_tokens != null || !this.edit || this.learning.value() !== 250000 ? {learning_tokens:this.learning.value()} : {})}};
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
    else if (paths.length) role.source_routes.push({id:'implementation', topic:'实现资料', paths});
    for (const route of role.source_routes) for (const key of ['knowledge_base_ids','external_tool_ids']) {
      if (route[key]) route[key] = route[key].filter(id => role[key].includes(id));
    }
    const objective = role.long_term_objectives[0]; objective.desired_outcome = f.objective.value.trim(); objective.mode = f.mode.value;
    if (this.stageMode.value === 'autonomous' || objective.stage_planning) objective.stage_planning = {
      ...objective.stage_planning,mode:this.stageMode.value,objective_tokens:this.stageBudget.value(),
      constraints:lines(f.stage_constraints.value),completion_criteria:lines(f.stage_completion.value),
    };
    if (this.schedule.value() && objective.mode === 'ongoing') objective.schedule = {every_seconds:this.schedule.value(),acceptance:lines(f.criteria.value)};
    else delete objective.schedule;
    return role;
  }
}
