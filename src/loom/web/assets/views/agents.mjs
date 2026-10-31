import {element} from '../markdown.mjs';
import {AgentsApi} from '../agents-api.mjs';
import {AgentSetupView} from './agent-setup.mjs';
import {AgentLearningView} from './agent-learning.mjs';
import {panel, cards, table, badge, number, disclosure} from './dashboard.mjs';
import {agentResourceChoices} from './agent-resources.mjs';
import {selectedTools} from './external-tools.mjs';

const status = {draft:'未启用', enabled:'已启用', paused:'已暂停', archived:'已归档', idle:'空闲', waiting:'等待',
  working:'执行中', planning:'编排中', degraded:'需要处理', pending:'待处理', running:'进行中', completed:'已完成',
  failed:'失败', blocked:'阻塞', deferred:'延后', cancelled:'已取消', queued:'排队中', active:'进行中', succeeded:'已验收',
  dispatched:'已派发', full:'完整', partial:'部分', none:'暂无', not_applicable:'无可分析证据'};
const label = value => status[value] || value || '—';
const tabs = ['Overview','Goals','Work','Profile','Knowledge','Learning'];
const titles = ['概览','目标','执行','工作方式','知识来源','学习'];
const uuid = () => globalThis.crypto.randomUUID();

function field(form, name, title, value = '', multiline = false) {
  const wrap = element('label', 'agent-field', title);
  const input = element(multiline ? 'textarea' : 'input'); input.name = name; input.value = value ?? '';
  if (multiline) input.rows = 3;
  wrap.append(input); form.append(wrap); return input;
}
function button(title, action, className = 'quiet') {
  const node = element('button', className, title); node.type = 'button'; node.addEventListener('click', action); return node;
}
function lines(value) { return String(value || '').split('\n').map(s => s.trim()).filter(Boolean); }
function link(title, href) { const node = element('a', '', title); node.href = href; return node; }

export class AgentsView {
  constructor(root, listRoot, {api, error = () => {}}) {
    this.root = root; this.listRoot = listRoot; this.apiSource = api; this.error = error;
    this.generation = 0; this.tab = 'Overview'; this.selected = null;
    this.expandedBudgetSections = new Set();
    this.learningStates = new Map();
    this.heading = root.parentElement?.querySelector('#agents-heading');
  }
  close() {
    ++this.generation; this.abort?.abort(); clearInterval(this.timer); this.learningView?.close();
    this.setup?.close(); this.setup = null; this.creating = false; this.root.classList.remove('agent-setup-page');
  }
  async open() {
    this.close(); this.abort = new AbortController();
    if (!this.apiSource()) { this.root.replaceChildren(element('p', '', '连接服务后查看 Agents。')); return; }
    this.api = new AgentsApi(this.apiSource());
    const generation = this.generation;
    await this.refresh();
    if (generation !== this.generation || this.abort.signal.aborted) return;
    this.timer = setInterval(() => this.refresh().catch(e => this.showError(e)), 1500);
  }
  showError(error) {
    if (error.name === 'AbortError') return;
    this.error(error);
    let node = this.root.querySelector('.agent-error');
    if (!node) { node = element('p', 'agent-error'); node.setAttribute('role', 'alert'); this.root.prepend(node); }
    node.textContent = `${error.message}${error.status === 409 ? '。请刷新后核对最新版本。' : ''}`;
  }
  async refresh() {
    const generation = this.generation, signal = this.abort?.signal;
    const catalog = await this.api.get(null, '', signal);
    if (generation !== this.generation || signal?.aborted) return;
    this.catalog = catalog;
    this.listRoot.replaceChildren(button('创建 Agent', () => this.createForm(), 'primary'));
    for (const agent of catalog.agents) {
      const row = button('', () => this.select(agent.id), `session-link${agent.id === this.selected ? ' selected' : ''}`);
      row.append(element('strong', '', agent.role.name), element('small', '', label(agent.state)));
      row.setAttribute('aria-current', agent.id === this.selected ? 'true' : 'false'); this.listRoot.append(row);
    }
    if (!this.selected) {
      if (!this.creating) {
        this.renderedDetail = null;
        const head = this.heading || element('header', 'workspace-header'); this.renderHeading(head, '选择一个 Agent');
        const body = element('div', 'trace-dashboard'), intro = panel(body, 'Agents', true);
        intro.append(element('p', 'empty-state', '配置长期目标、工作目录与方法，让 Agent 持续推进工作。'),
          button(catalog.agents.length ? '创建 Agent' : '创建第一个 Agent', () => this.createForm(), 'primary'));
        this.root.replaceChildren(...(this.heading ? [] : [head]), body);
      }
      return;
    }
    const selected = this.selected;
    const detail = await this.api.get(selected, '', signal);
    if (generation !== this.generation || selected !== this.selected || signal?.aborted) return;
    this.detail = detail;
    const editing = this.root.contains(document.activeElement) && /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    const resourceEditing = this.resourceEditor?.form.isConnected && (this.resourceEditor.dirty || this.resourceEditor.saving);
    const learningEditing = this.learningView?.current && this.learningView.dirty;
    if (!editing && !resourceEditing && !learningEditing && !this.creating && this.renderedDetail !== JSON.stringify(detail)) this.render();
    else if (!this.creating) this.updateSchedule(detail);
  }
  async select(id) { this.learningView?.close(); this.renderedDetail = null; this.setup?.close(); this.setup = null; this.root.classList.remove('agent-setup-page'); this.selected = id; this.creating = false; this.tab = 'Overview'; await this.refresh(); }
  async act(action) {
    try { await action(); await this.refresh(); }
    catch (error) { this.showError(error); }
  }
  createForm(edit = null, section = null) {
    this.learningView?.close();
    this.renderedDetail = null; this.setup?.close(); this.creating = true; if (!edit) this.selected = null;
    this.root.classList.add('agent-setup-page');
    if (this.heading) this.heading.hidden = true;
    const setup = new AgentSetupView(this.root, {
      api:this.apiSource(), edit,
      cancel:() => {setup.close(); this.setup = null; this.creating = false; this.root.classList.remove('agent-setup-page'); this.refresh().catch(e => this.showError(e));},
      save:async role => {
        const result = await this.api.post(edit?.id || null, edit ? 'role/revisions' : '',
          {command_id:uuid(), role, ...(edit ? {expected_revision:edit.revision} : {})});
        if (this.setup !== setup || setup.closed) return;
        document.activeElement?.blur(); setup.close(); this.setup = null;
        this.root.classList.remove('agent-setup-page'); this.selected = result.id; this.creating = false; this.tab = 'Overview';
        await this.refresh();
      },
    });
    this.setup = setup;
    return setup.load().then(() => {
      if (this.setup === setup && section === 'budget') setup.form.querySelector('#agent-budget-settings')?.scrollIntoView?.({block:'start'});
    });
  }
  renderHeading(head, title, d = null) {
    head.hidden = false; head.replaceChildren();
    const identity = element('div'); identity.append(element('small', 'eyebrow', 'AGENTS'), element('h1', '', title));
    head.append(identity);
    if (d) {
      const actions = element('div', 'trajectory-actions agent-actions');
      actions.append(badge(d.pause_pending ? '暂停中' : label(d.state)), badge(label(d.activity), d.activity === 'degraded' ? 'blocked' : d.activity));
      const controls = d.state === 'enabled' ? [['pause','暂停'],['wake','立即检查']] : d.state === 'archived' ? [] : d.state === 'paused' ? [['resume','恢复']] : [['enable','启用']];
      for (const [type, title] of controls) actions.append(button(title, () => this.act(() => this.api.command(d.id,type,d.revision)), type === 'enable' ? 'primary' : 'quiet'));
      head.append(actions);
    }
  }
  render() {
    this.learningView?.close();
    const d = this.detail, head = this.heading || element('header', 'workspace-header');
    this.renderedDetail = JSON.stringify(d); this.renderHeading(head, d.role.name, d);
    const nav = element('nav', 'evaluation-tabs agent-tabs'); nav.setAttribute('role','tablist'); nav.setAttribute('aria-label','Agent 详情');
    tabs.forEach((tab, i) => {
      const b = button(titles[i], () => {this.tab = tab; this.render(); this.root.querySelector(`[data-tab="${tab}"]`).focus();});
      b.dataset.tab = tab; b.id = `agent-tab-${tab}`; b.setAttribute('role','tab'); b.setAttribute('aria-selected',String(tab === this.tab));
      b.tabIndex = tab === this.tab ? 0 : -1; b.setAttribute('aria-controls','agent-tab-content');
      b.addEventListener('keydown', event => {
        const next = event.key === 'ArrowRight' ? (i+1)%tabs.length : event.key === 'ArrowLeft' ? (i+tabs.length-1)%tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : null;
        if (next === null) return; event.preventDefault(); this.tab = tabs[next]; this.render(); this.root.querySelector(`[data-tab="${this.tab}"]`).focus();
      }); nav.append(b);
    });
    const content = element('section', 'agent-content trace-dashboard'); content.id = 'agent-tab-content'; content.setAttribute('role','tabpanel'); content.tabIndex = 0;
    content.setAttribute('aria-labelledby',`agent-tab-${this.tab}`);
    this.root.replaceChildren(...(this.heading ? [] : [head]), nav);
    if (!d.service_enabled) this.root.append(element('p','notice','自主运行尚未启用。启动服务时添加 --enable-agents；配置和历史仍可查看。'));
    if (d.error || d.service_error) this.root.append(element('p','notice error',d.error || d.service_error));
    this.root.append(content); this[`render${this.tab}`](content,d);
  }
  renderOverview(root, d) {
    const summary = panel(root,'运行概览',true);
    const schedule = element('div','agent-schedule'); schedule.dataset.agentSchedule = '';
    schedule.append(element('span','muted','下次调度'),element('strong'),element('p','muted small'));
    summary.append(schedule); this.updateSchedule(d);
    cards(summary, [['待处理目标',number(d.goals.filter(g => !['completed','cancelled','superseded'].includes(g.state)).length)],
      ['活动工作包',number(d.work_packages.filter(w => ['dispatched','running'].includes(w.state)).length)],
      ['关联 Session',number(d.sessions.length)], ['学习待办',number(d.learning.filter(l => !['completed','cancelled'].includes(l.state)).length)]]);
    const goals = panel(root,'长期目标');
    this.showRows(goals,d.role.long_term_objectives.map(g => g.desired_outcome));
    const runtime = panel(root,'运行配置');
    table(runtime,['配置','当前值'],[['工作目录',d.role.working_directory || '无本地目录'],['执行与规划模型',d.role.model || '服务默认模型'],
      ['评估模型',d.role.evaluation_model || '服务默认模型']]);
    const budget = panel(root,'预算与用量',true);
    budget.firstChild.append(button('调整预算',()=>this.createForm(d,'budget')));
    cards(budget,[['每日 token 上限',number(d.role.budget.daily_tokens)],['单工作包上限',number(d.role.budget.work_tokens)],
      ['单目标累计上限',number(d.role.budget.goal_tokens)],['每日调用次数上限',number(d.role.budget.max_calls)],['累计模型调用',number(d.budget.reduce((sum,r) => sum+r.calls,0))],
      ['未知用量调用',number(d.budget.reduce((sum,r) => sum+(r.unknown_calls || 0),0))]]);
    if (d.learning_budget) {
      const learning = d.learning_budget;
      const learningSection = element('section','agent-learning-budget');
      learningSection.append(element('h4','','学习额度'));
      budget.append(learningSection);
      cards(learningSection,[['每日初始学习额度',number(learning.initial_tokens)],['今日学习额度',number(learning.effective_tokens)],
        ['学习自动扩容',learning.policy.auto_expand ? `开启，最多 ${number(learning.policy.max_learning_tokens)} token` : '关闭']]);
      const grants = (d.learning_allocations || []).flatMap(a => a.grants.map(g => [a.window,number(g.before),number(g.after),g.reason]));
      if (grants.length) table(this.budgetDisclosure(learningSection,`学习窗口调整记录 · ${number(grants.length)} 条`,d.id,'learning'),
        ['预算窗口','扩容前','扩容后','原因'],grants);
    }
    this.renderBudgetUsage(budget,d);
    const history = panel(root,'编排记录',true);
    history.firstChild.append(button('查看最近编排原因',()=>this.act(async()=>{const {runs}=await this.api.get(d.id,'runs');
      output.replaceChildren();table(output,['状态','编排原因','时间'],runs.map(r=>[badge(label(r.state)),r.reason,r.created_at]));})));
    const output=element('div'); output.append(element('p','empty-state','查看每轮编排的原因与结果。'));history.append(output);
  }
  budgetDisclosure(root, title, agentId, section) {
    const node = disclosure(root,title), key = JSON.stringify([agentId,section]);
    node.open = this.expandedBudgetSections.has(key);
    node.addEventListener('toggle', () => {
      if (node.isConnected) node.open ? this.expandedBudgetSections.add(key) : this.expandedBudgetSections.delete(key);
    });
    return node;
  }
  renderBudgetUsage(root, d) {
    const usage = this.budgetDisclosure(root,'用量明细',d.id,'usage');
    usage.classList.add('agent-budget-usage');
    usage.append(element('h4','','每日汇总'));
    const days = new Map();
    for (const row of [...d.budget].sort((a,b) => b.window.localeCompare(a.window) || a.category.localeCompare(b.category))) {
      if (!days.has(row.window)) days.set(row.window,[]);
      days.get(row.window).push(row);
    }
    const metrics = ['calls','tokens','unknown_calls'];
    const headers = ['调用数','Token（含预留）','未知用量调用'];
    const entries = [...days].map(([day, rows], index) => {
      const totals = metrics.map(key => number(rows.reduce((sum,row) => sum+(row[key] || 0),0)));
      const key = JSON.stringify([d.id,'usage',day]);
      const detailRow = element('tr','agent-usage-detail-row'), cell = element('td'); cell.colSpan = 5;
      detailRow.id = `agent-usage-day-${index}`;
      const detail = element('section','agent-usage-day-detail');
      detail.append(element('h5','',`${day} · 用途明细`));
      table(detail,['用途',...headers],rows.map(row => [row.category,...metrics.map(metric => number(row[metric] || 0))]));
      cell.append(detail); detailRow.append(cell);
      const toggle = button('',() => {
        const expanded = detailRow.hidden;
        expanded ? this.expandedBudgetSections.add(key) : this.expandedBudgetSections.delete(key);
        update(expanded);
      },'quiet agent-usage-toggle');
      const update = expanded => {
        detailRow.hidden = !expanded;
        toggle.textContent = expanded ? '收起明细' : '展开明细';
        toggle.setAttribute('aria-expanded',String(expanded));
        toggle.setAttribute('aria-label',`${day} ${toggle.textContent}`);
      };
      toggle.setAttribute('aria-controls',detailRow.id);
      update(this.expandedBudgetSections.has(key));
      return {values:[element('strong','',day),...totals,toggle],detailRow};
    });
    const daily = table(usage,['日期',...headers,'用途明细'],entries.map(entry => entry.values));
    [...daily.tBodies[0].rows].forEach((row,index) => {
      if (entries[index]) row.after(entries[index].detailRow);
    });
  }
  updateSchedule(d, now = Date.now()) {
    const schedule = this.root.querySelector('[data-agent-schedule]'); if (!schedule) return;
    const title = schedule.querySelector('strong'), note = schedule.querySelector('p');
    const inactive = {
      draft:['尚未启用','启用 Agent 后开始调度。'],
      paused:[d.pause_pending ? '暂停中' : '已暂停','恢复 Agent 后重新调度。'],
      archived:['已归档','不再自动调度。'],
    }[d.state] || (!d.service_enabled ? ['自主运行未启用','服务启用自主运行后才能调度。'] : null);
    if (inactive) {title.textContent = inactive[0]; note.textContent = inactive[1]; return;}
    if (!d.next_wake_at) {
      title.textContent = '等待调度'; note.textContent = '已请求调度，等待调度器检查。'; return;
    }
    const at = new Date(d.next_wake_at);
    if (!Number.isFinite(at.getTime())) {
      title.textContent = '调度时间暂不可用'; note.textContent = '等待服务更新调度时间。'; return;
    }
    const time = element('time','',at.toLocaleString(undefined,{
      year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,
    }));
    time.dateTime = at.toISOString(); title.replaceChildren(time);
    const remaining = Math.ceil((at.getTime() - now) / 1000);
    const countdown = remaining <= 0 ? '已到调度时间，等待调度器处理' : remaining < 60 ? `${remaining} 秒后` :
      remaining < 3600 ? `约 ${Math.ceil(remaining / 60)} 分钟后` : remaining < 86400 ? `约 ${Math.ceil(remaining / 3600)} 小时后` : `约 ${Math.ceil(remaining / 86400)} 天后`;
    note.textContent = `${countdown} · 本地时间（${Intl.DateTimeFormat().resolvedOptions().timeZone}）`;
    schedule.title = '预计进行下一次目标检查与编排的时间；新目标或执行结果可能触发提前调度。';
  }
  showRows(root, values) { const list = element('ul','dashboard-file-list'); for (const value of values) list.append(element('li','',value)); root.append(list); }
  renderGoals(root, d) {
    if (!d.goals.length) panel(root,'目标队列',true).append(element('p','empty-state','暂无目标。在下方添加本次交付目标与验收条件。'));
    for (const goal of d.goals) {
      const row = element('article', 'dashboard-panel dashboard-wide agent-card'); row.append(element('h3','',goal.objective),badge(label(goal.state),goal.state));
      this.showRows(row, goal.acceptance);
      if (!['completed','cancelled','superseded'].includes(goal.state)) for (const type of ['pause','resume','cancel']) row.append(button(
        {pause:'暂停目标',resume:'恢复目标',cancel:'取消目标'}[type], () => this.act(() => this.api.post(d.id, `goals/${encodeURIComponent(goal.id)}/commands`,
          {command_id:uuid(), type, expected_revision:goal.revision}))));
      if (!['completed','cancelled','superseded'].includes(goal.state)) row.append(button('修订目标', () => {
        const form=element('form','agent-form');field(form,'objective','目标',goal.objective,true);field(form,'acceptance','验收条件',goal.acceptance.join('\n'),true);
        form.append(button('保存新合同',()=>this.act(async()=>{await this.api.post(d.id,`goals/${encodeURIComponent(goal.id)}/commands`,
          {command_id:uuid(),type:'revise',expected_revision:goal.revision,payload:{objective:form.elements.objective.value,acceptance:lines(form.elements.acceptance.value),
            long_term_objective_id:goal.long_term_objective_id,depends_on:goal.depends_on,priority:goal.priority,stop_conditions:goal.stop_conditions,learning_dependency:goal.learning_dependency}});
          document.activeElement?.blur();})));row.append(form);
      }));
      root.append(row);
    }
    const form = element('form','agent-form'); field(form,'objective','本次交付目标','',true); field(form,'acceptance','验收条件（每行一条）','',true);
    const submit = element('button','primary','添加目标'); submit.type='submit'; form.append(submit);
    form.addEventListener('submit', e => {e.preventDefault(); this.act(async () => {await this.api.post(d.id,'goals',
      {command_id:uuid(),goal:{objective:form.elements.objective.value,acceptance:lines(form.elements.acceptance.value)}}); form.reset(); document.activeElement?.blur();});}); panel(root,'添加目标',true).append(form);
  }
  renderWork(root, d) {
    const work = panel(root,'工作包',true);
    table(work,['目标','状态','Session','编排依据'],d.work_packages.map(w => [w.objective,badge(label(w.state),w.state),
      w.session_id ? link('打开执行记录',`#/sessions/${encodeURIComponent(w.session_id)}`) : '尚未派发',w.blocked_reason || w.reason]));
    const sessions = panel(root,'Session 管理',true);
    table(sessions,['Session','归属','操作'],d.sessions.map(s => [link(s.session_id,`#/sessions/${encodeURIComponent(s.session_id)}`),
      badge(s.taken_over ? '人工接管' : 'Agent 管理'),button(s.taken_over ? '归还 Session' : '接管 Session',()=>this.act(()=>this.api.post(d.id,
        `sessions/${encodeURIComponent(s.session_id)}/commands`,{command_id:uuid(),type:s.taken_over?'release':'takeover',expected_revision:d.revision})))]));
    const routes = panel(root,'Session 选择依据',true), output=element('div'); routes.append(output);
    routes.firstChild.append(button('查看路由记录',()=>this.act(async()=>{const {routes:rows}=await this.api.get(d.id,'routes');output.replaceChildren();
      table(output,['选择','依据'],rows.map(r=>[r.action,r.reason]));})));
  }
  renderProfile(root, d) {
    const role = panel(root,'角色定位',true);
    role.firstChild.append(button('修订角色与预算', () => this.createForm(d)));
    role.append(element('p','',d.role.role_text));
    role.append(button('管理学习与人工方法',()=>{this.tab='Learning';this.render();}));
  }
  renderKnowledge(root,d) {
    const sources = panel(root,'资料获取位置',true);
    table(sources,['用途','资料位置','知识库','外部工具'],d.role.source_routes.map(s=>[s.topic,(s.paths || []).join('、'),
      (s.knowledge_base_ids || []).join('、'),(s.external_tool_ids || []).join('、')]));
    const resources = panel(root,'知识库与外部工具',true), form = element('form','agent-resource-form'); resources.append(form);
    const grid = element('div','trace-dashboard'); form.append(grid);
    const knowledge = panel(grid,'知识库'), tools = panel(grid,'外部工具');
    const knowledgeList = element('div','knowledge-choices'), toolList = element('div','external-choices');
    for (const [target,list,title] of [[knowledge,knowledgeList,'知识库'],[tools,toolList,'外部工具']]) {
      list.setAttribute('role','group'); list.setAttribute('aria-label',title);
      list.append(element('p','muted','加载可选项…')); target.append(list);
    }
    const count = element('p','muted small'), message = element('p','notice'); message.hidden = true;
    message.setAttribute('role','status');
    const actions = element('div','setup-form-actions'), save = element('button','primary','保存资源绑定');
    save.type = 'submit'; save.disabled = true;
    const retry = button('重新加载选项',()=>load()); retry.hidden = true;
    actions.append(save,retry); form.append(count,message,actions);
    const editor = this.resourceEditor = {form,dirty:false,saving:false,loaded:false};
    const generation = this.generation, signal = this.abort?.signal;
    const current = () => generation === this.generation && this.selected === d.id && form.isConnected;
    const summarize = () => {
      count.textContent = `已选择 ${selectedTools(knowledgeList).length} 个知识库 · ${selectedTools(toolList).length} 个外部工具`;
    };
    form.addEventListener('change',()=>{editor.dirty = true; summarize(); save.disabled = !editor.loaded || editor.saving;});
    const showError = error => {
      message.hidden = false; message.classList.add('error'); message.setAttribute('role','alert');
      message.textContent = `${error.message}${error.status === 409 ? '。配置已更新，请重新进入知识来源页核对后再保存。' : ''}`;
    };
    const load = async () => {
      retry.hidden = true; save.disabled = true; message.hidden = true;
      try {
        const [catalog,bases] = await Promise.all([
          this.apiSource().json('/v1/web/catalog',{signal}), this.apiSource().json('/v1/knowledge-bases',{signal}),
        ]);
        if (!current()) return;
        agentResourceChoices(toolList,knowledgeList,catalog,bases,d.role);
        editor.loaded = true; summarize();
      } catch (error) {
        if (!current() || error.name === 'AbortError') return;
        showError(error); retry.hidden = false;
      }
    };
    form.addEventListener('submit',async event => {
      event.preventDefault(); if (save.disabled) return;
      editor.saving = true; save.disabled = true; message.hidden = true;
      form.querySelectorAll('input').forEach(input => input.disabled = true);
      const role = structuredClone(d.role); delete role.schema_version;
      role.knowledge_base_ids = selectedTools(knowledgeList); role.external_tool_ids = selectedTools(toolList);
      for (const route of role.source_routes || []) for (const key of ['knowledge_base_ids','external_tool_ids']) {
        if (route[key]) route[key] = route[key].filter(id => role[key].includes(id));
      }
      try {
        await this.api.post(d.id,'role/revisions',{command_id:uuid(),expected_revision:d.revision,role});
        if (!current()) return;
        editor.dirty = false; editor.saving = false; this.renderedDetail = null;
        document.activeElement?.blur(); await this.refresh();
      } catch (error) {if (current()) showError(error);}
      finally {
        editor.saving = false;
        if (current()) {
          form.querySelectorAll('input').forEach(input => input.disabled = false);
          save.disabled = !editor.dirty;
        }
      }
    });
    load();
    panel(root,'知识更新',true).append(element('p','muted','使用已绑定的来源。周期刷新和自动连接新来源将在后续阶段开放。'));
  }
  renderLearning(root,d) {
    if (!this.learningStates.has(d.id)) this.learningStates.set(d.id,{});
    this.learningView = new AgentLearningView(root,{detail:d,api:this.api,transport:this.apiSource(),state:this.learningStates.get(d.id),
      changed:async()=>{this.renderedDetail=null;await this.refresh();}});
  }
}
