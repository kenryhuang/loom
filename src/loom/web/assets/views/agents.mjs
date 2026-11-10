import {element} from '../markdown.mjs';
import {AgentsApi} from '../agents-api.mjs';
import {AgentSetupView} from './agent-setup.mjs';
import {AgentLearningView} from './agent-learning.mjs';
import {panel, cards, table, badge, number, seconds, disclosure} from './dashboard.mjs';
import {agentResourceChoices} from './agent-resources.mjs';
import {selectedTools} from './external-tools.mjs';
import {editableStage, renderRoadmaps, roadmapDisclosure} from './agent-roadmap.mjs';
import {renderExecution} from './agent-execution.mjs';
import {AgentConfigurationEditor, configurationTitles, configuredRole} from './agent-configuration.mjs';

const status = {draft:'Disabled', enabled:'Enabled', paused:'Paused', archived:'Archived', idle:'Idle', waiting:'Waiting',
  working:'Working', planning:'Planning', degraded:'Needs attention', pending:'Pending', running:'Running', completed:'Completed',
  incomplete:'Incomplete (left queue)',failed:'Failed', blocked:'Blocked', deferred:'Deferred', cancelled:'Cancelled', queued:'Queued', active:'Active', succeeded:'Accepted',
  dispatched:'Dispatched', full:'Full', partial:'Partial', none:'None', not_applicable:'No analyzable evidence'};
const label = value => status[value] || value || '—';
const tabs = ['Overview','Goals','Work','Profile','Knowledge','Learning'];
const titles = ['Overview','Goals','Work','Profile','Knowledge','Learning'];
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
    this.stageExpansionStates = new Map();
    this.roadmapSelections = new Map();
    this.sessionMetrics = new Map();
    this.heading = root.parentElement?.querySelector('#agents-heading');
  }
  close() {
    ++this.generation; this.abort?.abort(); this.refreshAbort?.abort(); clearInterval(this.timer); this.learningView?.close();
    this.refreshing=null;this.loadedKey=null;this.diagnosticsCache?.clear();
    this.setup?.close(); this.setup = null; this.creating = false; this.root.classList.remove('agent-setup-page');
    this.configurationEditor?.close(); this.configurationEditor = null;
    this.manualSchedule = null;
  }
  async open() {
    this.close(); this.abort = new AbortController();
    if (!this.apiSource()) { this.root.replaceChildren(element('p', '', 'Connect to the service to view Agents.')); return; }
    this.api = new AgentsApi(this.apiSource());
    const generation = this.generation;
    await this.refresh();
    if (generation !== this.generation || this.abort.signal.aborted) return;
    this.timer = setInterval(() => {if (!document.hidden) this.refresh().catch(e => this.showError(e));}, 1500);
  }
  showError(error) {
    if (error.name === 'AbortError') return;
    this.error(error);
    let node = this.root.querySelector('.agent-error');
    if (!node) { node = element('p', 'agent-error'); node.setAttribute('role', 'alert'); this.root.prepend(node); }
    node.textContent = `${error.message}${error.status === 409 ? ' Please refresh and check the latest version.' : ''}`;
  }
  refresh({force=false}={}) {
    const key=JSON.stringify([this.generation,this.selected,this.tab]);
    if(this.refreshing && this.refreshingKey!==key){this.refreshAbort?.abort();this.refreshing=null;}
    if(this.refreshing)return this.refreshing.then(()=>{
      if(key===JSON.stringify([this.generation,this.selected,this.tab]) && (force || this.loadedKey!==key))return this.refresh();
    });
    this.refreshingKey=key;this.refreshAbort=new AbortController();
    const signal=this.abort?.signal ? AbortSignal.any([this.abort.signal,this.refreshAbort.signal]) : this.refreshAbort.signal;
    const pending=this.refreshOnce(signal).catch(error=>{if(error.name!=='AbortError')throw error;});
    const tracked=pending.finally(()=>{if(this.refreshing===tracked)this.refreshing=null;});
    this.refreshing=tracked;return tracked;
  }
  async refreshOnce(signal) {
    const generation = this.generation;
    const selected=this.selected,tab=this.tab;
    const [catalog,detail]=await Promise.all([this.api.get(null,'',signal),
      selected?this.api.view(selected,tab.toLowerCase(),signal):Promise.resolve(null)]);
    if (generation !== this.generation || selected!==this.selected || tab!==this.tab || signal?.aborted) return;
    if(tab==='Goals' && detail && !detail.view) await this.fillSessionMetrics(detail,signal);
    if (generation !== this.generation || selected!==this.selected || tab!==this.tab || signal?.aborted) return;
    this.loadedKey=JSON.stringify([generation,selected,tab]);
    const catalogKey=JSON.stringify([this.selected,catalog]);
    this.catalog = catalog;
    if(this.renderedCatalog!==catalogKey){
      this.renderedCatalog=catalogKey;
      this.listRoot.replaceChildren(button('Create Agent', () => this.createForm(), 'primary'));
      for (const agent of catalog.agents) {
        const row = button('', () => this.select(agent.id), `session-link${agent.id === this.selected ? ' selected' : ''}`);
        row.append(element('strong', '', agent.role.name), element('small', '', label(agent.state)));
        row.setAttribute('aria-current', agent.id === this.selected ? 'true' : 'false'); this.listRoot.append(row);
      }
    }
    if (!this.selected) {
      if (!this.creating) {
        this.renderedDetail = null;
        const head = this.heading || element('header', 'workspace-header'); this.renderHeading(head, 'Select an Agent');
        const body = element('div', 'trace-dashboard'), intro = panel(body, 'Agents', true);
        intro.append(element('p', 'empty-state', 'Configure long-term objectives, a workspace, and methods so the Agent can keep making progress.'),
          button(catalog.agents.length ? 'Create Agent' : 'Create your first Agent', () => this.createForm(), 'primary'));
        this.root.replaceChildren(...(this.heading ? [] : [head]), body);
      }
      return;
    }
    this.detail = detail;
    this.updateManualSchedule(detail);
    const editing = this.root.contains(document.activeElement) && /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    const resourceEditing = this.resourceEditor?.form.isConnected && (this.resourceEditor.dirty || this.resourceEditor.saving);
    const learningEditing = this.learningView?.current && this.learningView.dirty;
    const configurationEditing = this.configurationEditor?.form.isConnected;
    if (!editing && !resourceEditing && !learningEditing && !configurationEditing && !this.creating && this.renderedDetail !== JSON.stringify(detail)) this.render();
    else if (!this.creating) this.updateSchedule(detail);
  }
  async fillSessionMetrics(detail,signal) {
    const missing=(detail.work_packages || []).filter(w=>w.session_id && !Object.hasOwn(w,'execution_seconds'));
    const ids=[...new Set(missing.map(w=>w.session_id))], now=Date.now();
    await Promise.all(ids.map(async id=>{
      const cached=this.sessionMetrics.get(id);
      if(cached && now-cached.at<30000)return;
      try {
        const session=await this.apiSource().json(`/v1/sessions/${encodeURIComponent(id)}/snapshot`,{signal});
        if(session.run) this.sessionMetrics.set(id,{at:Date.now(),execution_seconds:session.run.active_seconds,actual_tokens:session.token_budget?.used});
      } catch(error) { if(error.name==='AbortError')throw error; }
    }));
    for(const work of missing){
      const metrics=this.sessionMetrics.get(work.session_id);
      if(metrics){work.execution_seconds=metrics.execution_seconds;work.actual_tokens=metrics.actual_tokens;}
    }
  }
  changeTab(tab) {
    this.abort?.abort();this.abort=new AbortController();
    this.tab=tab;this.loadedKey=null;this.render();
    return this.refresh().catch(e=>this.showError(e));
  }
  async select(id) {
    this.abort?.abort();this.abort=new AbortController();this.loadedKey=null;
    this.learningView?.close();this.configurationEditor?.close();this.configurationEditor=null;
    this.renderedDetail=null;this.manualSchedule=null;this.setup?.close();this.setup=null;
    this.root.classList.remove('agent-setup-page');this.selected=id;this.creating=false;this.tab='Overview';
    await this.refresh();
  }
  async act(action) {
    try { await action(); await this.refresh({force:true}); }
    catch (error) { this.showError(error); }
  }
  updateManualSchedule(detail) {
    const pending=this.manualSchedule, scheduler=detail.scheduler || detail.execution?.scheduler;
    if(!pending || pending.agentId!==detail.id || pending.state==='submitting' || !scheduler || scheduler.id===pending.previousId || scheduler.trigger!=='manual')return;
    if(['checking','planning'].includes(scheduler.state)) pending.message=scheduler.state==='planning'?'Scheduling started; planning tasks.':'Scheduling started; checking the queue.';
    else if(scheduler.finished_at) {pending.state='finished';pending.message=`Scheduling check finished: ${scheduler.reason || 'See execution status for details.'}`;}
  }
  async scheduleNow(detail) {
    if(this.manualSchedule?.state==='submitting')return;
    const pending={agentId:detail.id,previousId:(detail.scheduler || detail.execution?.scheduler)?.id,state:'submitting',message:'Submitting scheduling request…'};
    this.manualSchedule=pending;
    const redraw=()=>{if(this.manualSchedule===pending && this.selected===detail.id && this.detail?.id===detail.id)this.renderHeading(this.heading || this.root.querySelector('header'),this.detail.role.name,this.detail);};
    redraw();
    try {
      const result=await this.api.command(detail.id,'wake',detail.revision);
      if(this.manualSchedule!==pending)return;
      if(result?.state && result.state!=='enabled') {pending.state='finished';pending.message='Agent is not enabled; the scheduling request was not run.';}
      else if(['checking','planning'].includes(result?.scheduler?.state)) {pending.state='finished';pending.message='Scheduling already in progress; skipped the duplicate submission.';}
      else {pending.state='queued';pending.message='Scheduling request submitted; waiting for the scheduler check.';}
      redraw();
      await this.refresh({force:true});
    } catch(error) {
      if(this.manualSchedule!==pending)return;
      if(pending.state==='submitting') {pending.state='finished';pending.message='Failed to submit the scheduling request; see the error details.';redraw();}
      this.showError(error);
    }
  }
  createForm(edit = null, section = null) {
    if(edit) {
      if(edit.view && edit.view!=='overview')return this.changeTab('Overview').then(()=>this.editConfiguration(this.detail,section || 'identity'));
      this.detail=edit;this.selected=edit.id;this.tab='Overview';this.creating=false;
      this.setup?.close();this.setup=null;this.root.classList.remove('agent-setup-page');this.render();
      return this.editConfiguration(edit,section || 'identity');
    }
    const returnTo=this.selected;
    this.configurationEditor?.close();this.configurationEditor=null;
    this.learningView?.close();
    this.renderedDetail = null; this.setup?.close(); this.creating = true; this.selected = null;
    this.root.classList.add('agent-setup-page');
    if (this.heading) this.heading.hidden = true;
    const setup = new AgentSetupView(this.root, {
      api:this.apiSource(),
      cancel:() => {
        setup.close();this.setup=null;this.creating=false;this.renderedDetail=null;
        this.root.classList.remove('agent-setup-page');this.root.replaceChildren();
        if(this.heading)this.heading.hidden=false;
        if(returnTo && this.detail?.id===returnTo){this.selected=returnTo;this.render();}
        else {
          if(this.heading)this.renderHeading(this.heading,'Select an Agent');
          this.root.append(element('p','empty-state','Creation cancelled. Select an Agent to view its configuration.'));
        }
        this.refresh().catch(e=>this.showError(e));
      },
      save:async role => {
        const result = await this.api.post(null,'',{command_id:uuid(),role});
        if (this.setup !== setup || setup.closed) return;
        document.activeElement?.blur(); setup.close(); this.setup = null;
        this.root.classList.remove('agent-setup-page'); this.selected = result.id; this.creating = false; this.tab = 'Overview';
        await this.refresh();
      },
    });
    this.setup = setup;
    return setup.load();
  }
  async editConfiguration(detail,section) {
    this.configurationEditor?.close();this.configurationEditor=null;
    const target=this.root.querySelector(`[data-configuration-section="${section}"]`);
    if(!target)return;
    let commandId,signature;
    const editor=new AgentConfigurationEditor(target,{detail,section,api:this.apiSource(),
      cancel:()=>{
        if(this.configurationEditor!==editor)return;
        this.configurationEditor=null;this.renderedDetail=null;this.render();
      },
      save:async changes=>{
        const next=JSON.stringify(changes);if(next!==signature){signature=next;commandId=uuid();}
        await this.api.post(detail.id,`configuration/${section}`,{command_id:commandId,expected_revision:detail.revision,changes},editor.abort.signal);
        if(this.configurationEditor!==editor || editor.closed || this.selected!==detail.id)return;
        editor.close();this.configurationEditor=null;this.renderedDetail=null;await this.refresh({force:true});
      }});
    this.configurationEditor=editor;
    await editor.load();
    if(!editor.closed)editor.form.querySelector('input,textarea,select')?.focus();
  }
  configurationPanel(root,detail,section,rows,wide=false) {
    const node=panel(root,configurationTitles[section],wide);node.dataset.configurationSection=section;
    const edit=button('Edit',()=>this.editConfiguration(detail,section),'agent-configuration-edit');edit.setAttribute('aria-label',`Edit ${configurationTitles[section]}`);
    edit.disabled=detail.state==='archived';node.firstChild.append(edit);
    if(detail.pending_configuration?.sections?.[section])node.firstChild.append(badge('Applies to next task'));
    table(node,['Setting','New-task configuration'],rows);return node;
  }
  renderHeading(head, title, d = null) {
    head.hidden = false; head.replaceChildren();
    const identity = element('div'); identity.append(element('small', 'eyebrow', 'AGENTS'), element('h1', '', title));
    head.append(identity);
    if (d) {
      const actions = element('div', 'trajectory-actions agent-actions');
      actions.append(badge(d.pause_pending ? 'Pausing' : label(d.state)), badge(label(d.activity), d.activity === 'degraded' ? 'blocked' : d.activity));
      const controls = d.state === 'enabled' ? [['pause','Pause'],['wake','Schedule now']] : d.state === 'archived' ? [] : d.state === 'paused' ? [['resume','Resume']] : [['enable','Enable']];
      for (const [type, title] of controls) {
        const action=button(title, () => type==='wake' ? this.scheduleNow(d) : this.act(() => this.api.command(d.id,type,d.revision)), type === 'enable' ? 'primary' : 'quiet');
        if(type==='wake' && (this.manualSchedule?.agentId===d.id && ['submitting','queued'].includes(this.manualSchedule.state) || d.schedule_requested || ['checking','planning'].includes((d.scheduler || d.execution?.scheduler)?.state))){action.disabled=true;action.title='Scheduling request already submitted or running';}
        actions.append(action);
      }
      if(this.manualSchedule?.agentId===d.id){const message=element('span','agent-schedule-feedback',this.manualSchedule.message);message.setAttribute('role','status');actions.append(message);}
      head.append(actions);
    }
  }
  render() {
    this.configurationEditor?.close();this.configurationEditor=null;
    if (this.tab !== 'Learning' || (this.detail.view && this.detail.view !== 'learning') || !this.learningView?.canUpdate(this.detail)) {
      this.learningView?.close(); this.learningView = null;
    }
    const d = this.detail, head = this.heading || element('header', 'workspace-header');
    this.renderedDetail = JSON.stringify(d); this.renderHeading(head, d.role.name, d);
    const nav = element('nav', 'evaluation-tabs agent-tabs'); nav.setAttribute('role','tablist'); nav.setAttribute('aria-label','Agent details');
    tabs.forEach((tab, i) => {
      const b = button(titles[i], () => {this.changeTab(tab); this.root.querySelector(`[data-tab="${tab}"]`).focus();});
      b.dataset.tab = tab; b.id = `agent-tab-${tab}`; b.setAttribute('role','tab'); b.setAttribute('aria-selected',String(tab === this.tab));
      b.tabIndex = tab === this.tab ? 0 : -1; b.setAttribute('aria-controls','agent-tab-content');
      b.addEventListener('keydown', event => {
        const next = event.key === 'ArrowRight' ? (i+1)%tabs.length : event.key === 'ArrowLeft' ? (i+tabs.length-1)%tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : null;
        if (next === null) return; event.preventDefault(); this.changeTab(tabs[next]); this.root.querySelector(`[data-tab="${this.tab}"]`).focus();
      }); nav.append(b);
    });
    const content = element('section', 'agent-content trace-dashboard'); content.id = 'agent-tab-content'; content.setAttribute('role','tabpanel'); content.tabIndex = 0;
    content.setAttribute('aria-labelledby',`agent-tab-${this.tab}`);
    this.root.replaceChildren(...(this.heading ? [] : [head]), nav);
    if (!d.service_enabled) this.root.append(element('p','notice','Autonomous operation is not enabled. Start the service with --enable-agents; configuration and history remain viewable.'));
    if (d.error || d.service_error) this.root.append(element('p','notice error',d.error || d.service_error));
    this.root.append(content);
    if(d.view && d.view!==this.tab.toLowerCase())content.append(element('p','empty-state','Loading…'));
    else this[`render${this.tab}`](content,d);
  }
  renderOverview(root, d) {
    const summary = panel(root,'Run overview',true);
    const schedule = element('div','agent-schedule'); schedule.dataset.agentSchedule = '';
    schedule.append(element('span','muted','Next schedule'),element('strong'),element('p','muted small'));
    summary.append(schedule); this.updateSchedule(d);
    cards(summary, [['Open goals',number(d.goals.filter(g => !['completed','cancelled','superseded'].includes(g.state)).length)],
      ['Active work packages',number(d.work_packages.filter(w => ['dispatched','running'].includes(w.state)).length)],
      ['Linked Sessions',number(d.sessions.length)], ['Learning backlog',number(d.learning.filter(l => !['completed','cancelled','failed','incomplete'].includes(l.state)).length)]]);
    const r=configuredRole(d);
    root.append(element('p','muted dashboard-wide','Configuration changes apply to newly scheduled tasks. Dispatched tasks and their recovery keep the original configuration; manage long-term objectives on the Goals tab. The workspace is fixed.'));
    if(d.configuration?.pending)root.append(element('p','notice dashboard-wide','New configuration saved; it takes effect before the next new task is scheduled, once dispatched tasks and analysis finish.'));
    const goals = panel(root,'Long-term objectives (read-only)');
    this.showRows(goals,d.role.long_term_objectives.map(g=>g.desired_outcome));
    goals.append(button('Go to Goals',()=>this.changeTab('Goals')));
    const workspace=panel(root,'Workspace (fixed)');
    table(workspace,['Setting','Current value'],[['Workspace',d.role.workspace || d.role.working_directory || 'No local directory'],
      ['Working directory',d.role.working_directory || 'No local directory']]);
    this.configurationPanel(root,d,'identity',[['Name',r.name],['Role description',r.role_text],['Default methods',(r.methods || []).join('; ') || 'Not set']]);
    this.configurationPanel(root,d,'models',[['Execution & planning model',r.model || 'Service default'],['Evaluation model',r.evaluation_model || 'Service default']]);
    this.configurationPanel(root,d,'execution',[['Planning mode',({auto:'Auto',force:'Forced planning',off:'Direct execution'})[r.plan_mode] || 'Auto'],
      ['Context plugin',r.task_spec?.context?.plugin || 'Service default'],['Workflow plugin',r.task_spec?.workflow?.plugin || 'Service default'],
      ['Tool collections',r.task_spec?.tools?.collections?.join(', ') || 'Service default']]);
    const scheduling=r.scheduling_policy || {};
    this.configurationPanel(root,d,'scheduling',[['Goal check interval',`${r.interval_seconds || 86400} s`],
      ['Scheduled runs',scheduling.enabled===false?'Manual only':'On'],['Scheduler tick interval',`${scheduling.tick_seconds || 60} s`],
      ['Auto-recoveries per task',scheduling.max_recovery_attempts ?? 5],['Timezone',r.timezone || 'UTC']]);
    const learning=r.learning_policy || {};
    this.configurationPanel(root,d,'learning',[['Auto-resumes per evaluation per day',learning.max_resumes ?? 3],
      ['Attempts per analysis round',learning.max_attempts ?? 6],['Evaluation call limit',learning.max_evaluation_calls ?? 500],
      ['Automatic method learning',r.method_learning?'On':'Off']]);
    this.configurationPanel(root,d,'resources',[['Source paths',(r.source_routes || []).flatMap(route=>route.paths || []).join(', ') || 'Not set'],
      ['Knowledge bases',(r.knowledge_base_ids || []).join(', ') || 'Not set'],['External tools',(r.external_tool_ids || []).join(', ') || 'Not set']]);
    const budget=this.configurationPanel(root,d,'budget',[['Daily token limit',number(r.budget.daily_tokens)],
      ['Per-response output limit',r.budget.output_tokens == null ? 'Use model configuration' : number(r.budget.output_tokens)]]);
    budget.append(element('p','muted small',`All tasks share the daily allowance. Resets at midnight ${d.role.timezone || 'UTC'}.`));
    const usage=panel(root,'Budget usage');
    const daily=d.daily_budget;
    table(usage,['Metric','Current value'],[['Current daily limit',number(daily?.limit ?? d.role.budget.daily_tokens)],['Used & reserved today',number(daily?.used ?? 0)],
      ['Remaining today',number(daily?.remaining ?? d.role.budget.daily_tokens)],['Total model calls',number(d.budget.reduce((sum,row)=>sum+row.calls,0))],
      ['Calls with unknown usage',number(d.budget.reduce((sum,row)=>sum+(row.unknown_calls || 0),0))]]);
    if(daily?.resets_at)usage.append(element('p','muted small',`Next reset: ${new Date(daily.resets_at).toLocaleString(undefined,{timeZone:d.role.timezone || 'UTC',timeZoneName:'short'})}`));
    this.renderBudgetUsage(root,d);
    const history = panel(root,'Orchestration history',true);
    history.firstChild.append(button('View recent orchestration reasons',()=>this.act(async()=>{const {runs}=await this.api.get(d.id,'runs');
      output.replaceChildren();table(output,['Status','Scheduled goal','Orchestration reason','Time'],runs.map(r=>{
        const goal=d.goals.find(g=>g.id===r.goal_id);
        const target=goal ? button(goal.objective,()=>this.focusGoal(goal.id)) : r.goal_id ||
          (r.kind==='stage_plan'?'Stage planning':r.kind==='stage_review'?'Stage review':'No new goal scheduled this round');
        return [badge(label(r.state)),target,r.reason,r.created_at];
      }));})));
    const output=element('div'); output.append(element('p','empty-state','See why each orchestration round ran and what it produced.'));history.append(output);
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
    const usage = this.budgetDisclosure(root,'Usage breakdown',d.id,'usage');
    usage.classList.add('agent-budget-usage','dashboard-panel','dashboard-wide');
    usage.append(element('h4','','Daily totals'));
    const days = new Map();
    for (const row of [...d.budget].sort((a,b) => b.window.localeCompare(a.window) || a.category.localeCompare(b.category))) {
      if (!days.has(row.window)) days.set(row.window,[]);
      days.get(row.window).push(row);
    }
    const metrics = ['calls','tokens','unknown_calls'];
    const headers = ['Calls','Tokens (incl. reserved)','Unknown-usage calls'];
    const entries = [...days].map(([day, rows], index) => {
      const totals = metrics.map(key => number(rows.reduce((sum,row) => sum+(row[key] || 0),0)));
      const key = JSON.stringify([d.id,'usage',day]);
      const detailRow = element('tr','agent-usage-detail-row'), cell = element('td'); cell.colSpan = 5;
      detailRow.id = `agent-usage-day-${index}`;
      const detail = element('section','agent-usage-day-detail');
      detail.append(element('h5','',`${day} · Usage by purpose`));
      table(detail,['Purpose',...headers],rows.map(row => [row.category,...metrics.map(metric => number(row[metric] || 0))]));
      cell.append(detail); detailRow.append(cell);
      const toggle = button('',() => {
        const expanded = detailRow.hidden;
        expanded ? this.expandedBudgetSections.add(key) : this.expandedBudgetSections.delete(key);
        update(expanded);
      },'quiet agent-usage-toggle');
      const update = expanded => {
        detailRow.hidden = !expanded;
        toggle.textContent = expanded ? 'Collapse details' : 'Expand details';
        toggle.setAttribute('aria-expanded',String(expanded));
        toggle.setAttribute('aria-label',`${day} ${toggle.textContent}`);
      };
      toggle.setAttribute('aria-controls',detailRow.id);
      update(this.expandedBudgetSections.has(key));
      return {values:[element('strong','',day),...totals,toggle],detailRow};
    });
    const daily = table(usage,['Date',...headers,'Usage details'],entries.map(entry => entry.values));
    [...daily.tBodies[0].rows].forEach((row,index) => {
      if (entries[index]) row.after(entries[index].detailRow);
    });
  }
  updateSchedule(d, now = Date.now()) {
    const schedule = this.root.querySelector('[data-agent-schedule]'); if (!schedule) return;
    const title = schedule.querySelector('strong'), note = schedule.querySelector('p');
    const inactive = {
      draft:['Not enabled yet','Scheduling starts once the Agent is enabled.'],
      paused:[d.pause_pending ? 'Pausing' : 'Paused','Resume the Agent to resume scheduling.'],
      archived:['Archived','No longer scheduled automatically.'],
    }[d.state] || (!d.service_enabled ? ['Autonomy not enabled','The service must enable autonomous operation before scheduling.'] : null);
    if (inactive) {title.textContent = inactive[0]; note.textContent = inactive[1]; return;}
    if (!d.next_wake_at) {
      title.textContent = 'Waiting for scheduling'; note.textContent = 'Scheduling requested; waiting for the scheduler check.'; return;
    }
    const at = new Date(d.next_wake_at);
    if (!Number.isFinite(at.getTime())) {
      title.textContent = 'Schedule time unavailable'; note.textContent = 'Waiting for the service to update the schedule time.'; return;
    }
    const time = element('time','',at.toLocaleString(undefined,{
      year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,
    }));
    time.dateTime = at.toISOString(); title.replaceChildren(time);
    const remaining = Math.ceil((at.getTime() - now) / 1000);
    const countdown = remaining <= 0 ? 'Scheduling time reached; waiting for the scheduler' : remaining < 60 ? `in ${remaining}s` :
      remaining < 3600 ? `in ~${Math.ceil(remaining / 60)}m` : remaining < 86400 ? `in ~${Math.ceil(remaining / 3600)}h` : `in ~${Math.ceil(remaining / 86400)}d`;
    note.textContent = `${countdown} · local time (${Intl.DateTimeFormat().resolvedOptions().timeZone})`;
    schedule.title = 'When the next goal check and orchestration is expected; new goals or execution results may trigger it earlier.';
  }
  showRows(root, values) { const list = element('ul','dashboard-file-list'); for (const value of values) list.append(element('li','',value)); root.append(list); }
  focusGoal(id) {
    const aid=this.detail.id;
    this.stageExpansionStates.set(JSON.stringify([aid,`goal:${id}`]),true);
    this.stageExpansionStates.set(JSON.stringify([aid,'unassigned']),true);
    const focus=()=>{
      if(this.detail.id!==aid || this.tab!=='Goals' || this.detail.view && this.detail.view!=='goals')return;
      const goal=this.detail.goals.find(g=>g.id===id);
      const stage=(this.detail.stages || []).find(s=>s.id===goal?.stage_id || s.goal_ids?.includes(id));
      const key=stage && JSON.stringify([aid,stage.objective_id]);
      if(stage && this.roadmapSelections.get(key)!==stage.id){this.roadmapSelections.set(key,stage.id);this.render();}
      const row=[...this.root.querySelectorAll('[data-goal-id]')].find(node=>node.dataset.goalId===id);
      if(row){for(let node=row;node && node!==this.root;node=node.parentElement)if(node.tagName==='DETAILS')node.open=true;
        row.focus();row.scrollIntoView?.({block:'center'});}
    };
    this.changeTab('Goals').then(focus);focus();
  }
  renderGoalWork(root,d,goal) {
    const works=d.work_packages.filter(w=>w.goal_id===goal.id).sort((a,b)=>(a.order??0)-(b.order??0));
    if(!works.length) {root.append(element('p','muted','No work packages split or dispatched yet.'));return;}
    const node=table(root,['Work package','Status','Execution time','Tokens used'],works.map(w=>{
      const name=element('div');
      name.append(element('span','',w.objective));
      if(w.reason) name.append(element('p','muted',`Goal contribution: ${w.reason}`));
      if(w.blocked_reason && !['succeeded','completed'].includes(w.state)) name.append(element('p','notice',w.blocked_reason));
      const state=badge(w.superseded_contract?'Superseded contract · '+label(w.state):label(w.state),w.state);
      const stateCell=w.session_id ? link('',`#/sessions/${encodeURIComponent(w.session_id)}`) : state;
      if(w.session_id)stateCell.append(state);
      const duration=w.execution_seconds == null ? '—' : seconds(w.execution_seconds * 1000);
      return [name,stateCell,duration,number(w.actual_tokens)];
    }));
    node.classList.add('agent-goal-work-table');
    node.querySelectorAll('th:nth-child(n+3)').forEach(th=>th.title="Shows data from the linked Session's current run; a reused Session may be linked to multiple work packages.");
  }
  renderGoals(root, d) {
    if(d.planning_engine!==2) {
      const migration=element('div','notice');
      migration.append(element('p','','This Agent uses the legacy planning flow. Migration preserves completed evidence and recompiles unstarted work into stages.'));
      const report=this.migrationPreview?.agent_id===d.id?this.migrationPreview:null;
      migration.append(button('Review planning migration',()=>this.act(async()=>{
        this.migrationPreview=await this.api.get(d.id,'planning/migration');
        this.renderedDetail=null;
      })));
      if(report) {
        migration.append(element('p','',`Replace ${report.retire_work_ids.length} unfinished work contracts; preserve ${report.preserve_completed_goal_ids.length} completed goals. A database backup is saved before migration.`));
        for(const blocker of report.blockers) migration.append(element('p','notice',blocker));
        if(!report.blockers.length) migration.append(button('Apply planning migration',()=>this.act(async()=>{
          await this.api.post(d.id,'planning/migration',{command_id:crypto.randomUUID(),expected_revision:report.revision});
          this.migrationPreview=null;
          this.renderedDetail=null;
        })));
      }
      root.append(migration);
    }
    renderRoadmaps(root,d,this);
    const assigned=new Set((d.stages || []).flatMap(s=>s.goal_ids || []));
    const standalone=d.goals.filter(g=>!assigned.has(g.id) && !(d.stages || []).some(s=>s.id===g.stage_id));
    if(standalone.length) {
      const block=roadmapDisclosure(this,root,d,'unassigned',`Historical and legacy goals · ${standalone.length}`);
      block.classList.add('dashboard-panel','dashboard-wide');
      block.append(element('p','muted','Deliverables not assigned to a stage stay here as the current-state basis for future planning.'));
      for(const goal of standalone) this.renderGoal(block,d,goal);
    }
  }
  renderGoal(root,d,goal,ord) {
    const stage=(d.stages || []).find(s=>s.id===goal.stage_id && s.goal_ids?.includes(goal.id));
    const editable=editableStage(d,stage) && !['completed','cancelled','superseded'].includes(goal.state);
    const row=roadmapDisclosure(this,root,d,`goal:${goal.id}`,'');
    row.classList.add('agent-roadmap-goal');row.dataset.goalId=goal.id;row.tabIndex=-1;
    const packages=d.work_packages.filter(w=>w.goal_id===goal.id && !w.superseded_contract);
    const accepted=packages.filter(w=>w.state==='succeeded').length,total=packages.length;
    const heading=element('span','agent-goal-heading'),name=element('span','goal-tree-title');
    if(ord) name.append(element('span','goal-tree-id',ord));
    name.append(document.createTextNode(goal.objective));
    const tags=element('span','goal-tree-tags');tags.append(badge(label(goal.state),goal.state));
    const track=element('span','agent-goal-progress-track'+(total?'':' is-unknown'));
    track.setAttribute('role','img');track.setAttribute('aria-label',`Work packages accepted ${accepted} / ${total}`);
    if(total){const fill=element('span','agent-goal-progress-fill');fill.style.width=`${Math.round(accepted/total*100)}%`;track.append(fill);}
    tags.append(track,element('span','agent-goal-progress-count',total?`${accepted} / ${total}`:'Not split yet'));
    const idcode=element('code','agent-roadmap-id',goal.id);idcode.title=goal.id;tags.append(idcode);
    heading.append(name,tags);
    row.querySelector('summary').append(heading);
    row.append(element('h5','','Acceptance criteria'));
    this.showRows(row, goal.acceptance);
    const selection = (d.selection || []).find(s => s.goal_id === goal.id);
    if (selection && selection.blocker?.code !== 'GOAL_STATE') row.append(element('p','muted',selection.reason));
    if (goal.proposal) row.append(element('p','muted',`Proposal rationale: ${goal.proposal.reason} · Source: ${goal.proposal.source}`));
    if (goal.not_before) row.append(element('p','muted',`Earliest retry: ${goal.not_before}`));
    if (goal.source_changed) row.append(element('p','notice',['completed','cancelled','superseded'].includes(goal.state)
      ? 'History: the source changed after planning.' : 'The source changed after planning; later dispatches wait for goal review.'));
    if (editable && goal.state === 'blocked') {
      const retry = element('form','agent-form');
      const kind = element('select'); kind.name='change_kind';
      for (const [value,title] of [['input','Input change'],['method','Method change'],['environment','Environment change']]) {
        const option=element('option','',title); option.value=value; kind.append(option);
      }
      retry.append(kind); field(retry,'change','What changed since the last failure','',true);
      retry.append(button('Create new attempt with backoff',()=>this.act(()=>this.api.post(d.id,`goals/${encodeURIComponent(goal.id)}/retry`,
        {command_id:uuid(),expected_revision:goal.revision,change_kind:kind.value,change:retry.elements.change.value}))));
      row.append(retry);
    }
    if ((editable || !goal.stage_id) && !['completed','cancelled','superseded'].includes(goal.state)) for (const type of ['pause','resume','cancel']) row.append(button(
      {pause:'Pause goal',resume:'Resume goal',cancel:'Cancel goal'}[type], () => this.act(() => this.api.post(d.id, `goals/${encodeURIComponent(goal.id)}/commands`,
        {command_id:uuid(), type, expected_revision:goal.revision}))));
    if (editable) row.append(button('Revise goal', () => {
      const form=element('form','agent-form');field(form,'objective','Objective',goal.objective,true);field(form,'acceptance','Acceptance criteria',goal.acceptance.join('\n'),true);
      form.append(button('Save new contract',()=>this.act(async()=>{await this.api.post(d.id,`goals/${encodeURIComponent(goal.id)}/commands`,
        {command_id:uuid(),type:'revise',expected_revision:goal.revision,payload:{objective:form.elements.objective.value,acceptance:lines(form.elements.acceptance.value)}});
        document.activeElement?.blur();})));row.append(form);
    }));
    row.append(element('h5','','Execution work packages'));this.renderGoalWork(row,d,goal);
  }
  renderWork(root, d) {
    renderExecution(root,d,this);
    const sessions = roadmapDisclosure(this,root,d,'execution-sessions','Session management & routing');
    sessions.classList.add('dashboard-wide');
    table(sessions,['Session','Ownership','Actions'],d.sessions.map(s => [link(s.session_id,`#/sessions/${encodeURIComponent(s.session_id)}`),
      badge(s.taken_over ? 'Human takeover' : 'Agent managed'),button(s.taken_over ? 'Return Session' : 'Take over Session',()=>this.act(()=>this.api.post(d.id,
        `sessions/${encodeURIComponent(s.session_id)}/commands`,{command_id:uuid(),type:s.taken_over?'release':'takeover',expected_revision:d.revision})))]));
    const routes = roadmapDisclosure(this,sessions,d,'execution-routes','Session routing rationale'), output=element('div'); routes.append(output);
    routes.firstChild.append(button('View routing history',()=>this.act(async()=>{const {routes:rows}=await this.api.get(d.id,'routes');output.replaceChildren();
      table(output,['Choice','Rationale'],rows.map(r=>[r.action,r.reason]));})));
  }
  renderProfile(root, d) {
    const role = panel(root,'Role identity',true);
    role.firstChild.append(button('Edit role', () => this.createForm(d,'identity')));
    role.append(element('p','',d.role.role_text));
    role.append(button('Manage learning & manual methods',()=>this.changeTab('Learning')));
    const preferences = panel(root,'Preferences & habits',true);
    preferences.append(element('p','muted','Explicit guidance takes effect immediately; inferred preferences keep their source, counterexamples, and review deadline. Trials only affect guidance and grant no extra tool permissions.'));
    table(preferences,['Preference','Scope','State','Evidence / review','Actions'],(d.profile_entries || []).map(p => {
      const actions=element('div','actions');
      for (const [type,title] of [['activate','Adopt from now on'],['trial','Trial for 7 days'],['lock','Lock'],[p.state==='retired'?'restore':'retire',p.state==='retired'?'Restore':'Retire']]) {
        actions.append(button(title,()=>this.act(()=>this.api.post(d.id,'preferences/commands',
          {command_id:uuid(),entry_id:p.id,expected_revision:p.revision,type}))));
      }
      const active=d.work_packages.find(w=>['running','dispatched'].includes(w.state));
      if (active) actions.append(button('Just this once',()=>this.act(()=>this.api.post(d.id,'preferences/commands',
        {command_id:uuid(),entry_id:p.id,expected_revision:p.revision,type:'once',work_package_id:active.id}))));
      return [p.text,p.scope==='owner'?`Shared: ${(d.shared_roles || []).map(r=>r.name).join(', ')}`:p.scope,
        `${p.state}${p.locked_by_user?' · Locked':''}`,`${p.independent_episodes} independent records · ${p.counterexamples.length} counterexamples · ${p.review_at}`,actions];
    }));
    const form=element('form','agent-form');
    field(form,'key','Preference name','',true); field(form,'text','Preference to adopt from now on','',true);
    const scope=element('select'); scope.name='scope'; scope.setAttribute('aria-label','Preference scope');
    for (const [value,title] of [['agent','This role only'],['owner','All roles of the same owner']]) {
      const option=element('option','',title); option.value=value; scope.append(option);
    }
    form.append(scope,button('Save explicit preference',()=>this.act(()=>this.api.post(d.id,'preferences/feedback',
      {command_id:uuid(),key:form.elements.key.value,text:form.elements.text.value,scope:scope.value,kind:'explicit'}))));
    preferences.append(form);
    const feedback=panel(root,'Method usage feedback',true), output=element('div');
    feedback.append(button('View adoption & outcomes',()=>this.act(async()=>{
      const report=await this.api.get(d.id,'methods/feedback'); output.replaceChildren();
      output.append(element('p','muted',`${report.sample_runs} execution samples · ${report.corrections} corrections. Causal improvement remains unknown.`));
      table(output,['Method / memory','Adoption','Acceptance','Actual actions','Tokens / unknown-usage calls'],report.records.map(r=>[
        r.reference_id,r.state,r.acceptance,r.action_ids.join(', '),`${r.actual_tokens ?? 'unknown'} / ${r.unknown_cost_calls}`]));
    })),output);
  }
  renderKnowledge(root,d) {
    const sources = panel(root,'Source locations',true);
    table(sources,['Purpose','Source paths','Knowledge bases','External tools'],configuredRole(d).source_routes.map(s=>[s.topic,(s.paths || []).join(', '),
      (s.knowledge_base_ids || []).join(', '),(s.external_tool_ids || []).join(', ')]));
    const resources = panel(root,'Knowledge bases & external tools',true), form = element('form','agent-resource-form'); resources.append(form);
    const grid = element('div','trace-dashboard'); form.append(grid);
    const knowledge = panel(grid,'Knowledge bases'), tools = panel(grid,'External tools');
    const knowledgeList = element('div','knowledge-choices'), toolList = element('div','external-choices');
    for (const [target,list,title] of [[knowledge,knowledgeList,'Knowledge bases'],[tools,toolList,'External tools']]) {
      list.setAttribute('role','group'); list.setAttribute('aria-label',title);
      list.append(element('p','muted','Loading choices…')); target.append(list);
    }
    const count = element('p','muted small'), message = element('p','notice'); message.hidden = true;
    message.setAttribute('role','status');
    const actions = element('div','setup-form-actions'), save = element('button','primary','Save resource bindings');
    save.type = 'submit'; save.disabled = true;
    const retry = button('Reload choices',()=>load()); retry.hidden = true;
    actions.append(save,retry); form.append(count,message,actions);
    const editor = this.resourceEditor = {form,dirty:false,saving:false,loaded:false};
    const generation = this.generation, signal = this.abort?.signal;
    const current = () => generation === this.generation && this.selected === d.id && form.isConnected;
    const summarize = () => {
      count.textContent = `${selectedTools(knowledgeList).length} knowledge bases · ${selectedTools(toolList).length} external tools selected`;
    };
    form.addEventListener('change',()=>{editor.dirty = true; summarize(); save.disabled = !editor.loaded || editor.saving;});
    const showError = error => {
      message.hidden = false; message.classList.add('error'); message.setAttribute('role','alert');
      message.textContent = `${error.message}${error.status === 409 ? ' Configuration changed; reopen the Knowledge tab to verify before saving.' : ''}`;
    };
    const load = async () => {
      retry.hidden = true; save.disabled = true; message.hidden = true;
      try {
        const [catalog,bases] = await Promise.all([
          this.apiSource().json('/v1/web/catalog',{signal}), this.apiSource().json('/v1/knowledge-bases',{signal}),
        ]);
        if (!current()) return;
        agentResourceChoices(toolList,knowledgeList,catalog,bases,configuredRole(d));
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
      const role = structuredClone(configuredRole(d)); delete role.schema_version;
      role.knowledge_base_ids = selectedTools(knowledgeList); role.external_tool_ids = selectedTools(toolList);
      for (const route of role.source_routes || []) for (const key of ['knowledge_base_ids','external_tool_ids']) {
        if (route[key]) route[key] = route[key].filter(id => role[key].includes(id));
      }
      try {
        await this.api.post(d.id,'configuration/resources',{command_id:uuid(),expected_revision:d.revision,changes:{
          knowledge_base_ids:role.knowledge_base_ids,external_tool_ids:role.external_tool_ids,source_routes:role.source_routes}});
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
    const updates=panel(root,'Knowledge updates',true);
    updates.append(button('Check bound sources',()=>this.act(()=>this.api.post(d.id,'sources/refresh',{}))));
    table(updates,['Source','State','Revision','Checked at','Usage condition'],(d.source_status || []).map(s=>[
      s.route_id,s.state,String(s.revision),s.checked_at,s.revalidate_before_use?'Revalidate before use':'Current version usable']));
    updates.append(element('p','muted','Configure refresh_seconds for periodic checks. The existing FTS local index supports incremental updates; backends without per-call metering keep coverage gaps.'));
  }
  renderLearning(root,d) {
    if (this.learningView?.canUpdate(d)) { this.learningView.update(root,d); return; }
    if (!this.learningStates.has(d.id)) this.learningStates.set(d.id,{});
    this.learningView = new AgentLearningView(root,{detail:d,api:this.api,transport:this.apiSource(),state:this.learningStates.get(d.id),
      changed:async()=>{this.renderedDetail=null;await this.refresh();}});
  }
}
