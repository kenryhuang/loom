import {element} from '../markdown.mjs';
import {panel, cards, badge, number, disclosure} from './dashboard.mjs';

const kinds = {facts:'事实',lessons:'经验',workflows:'方法'};
const confidence = {high:'高',medium:'中',low:'低',unknown:'未知'};
const reviews = {unreviewed:'待人工审核',confirmed:'人工已确认',corrected:'人工已更正'};
const states = {pending:'待处理',running:'进行中',completed:'已完成',failed:'失败',deferred:'延后',cancelled:'已取消',
  full:'完整',partial:'部分',none:'暂无',not_applicable:'无可分析证据'};
const label = value => states[value] || value || '—';
const uuid = () => globalThis.crypto.randomUUID();
function button(title, action, className = 'quiet') {
  const node = element('button',className,title); node.type = 'button'; node.addEventListener('click',action); return node;
}
function link(title, href) { const node = element('a','',title); node.href = href; return node; }
function field(root, name, title, multiline = false) {
  const wrap = element('label','agent-field',title), input = element(multiline ? 'textarea' : 'input');
  input.name = name; if (multiline) input.rows = 3; wrap.append(input); root.append(wrap); return input;
}
function choice(root, name, title, options, selected = '') {
  const wrap = element('label','agent-field',title), select = element('select'); select.name = name;
  for (const [value,text] of options) { const option = element('option','',text); option.value = value; select.append(option); }
  if (selected) select.value = selected;
  wrap.append(select); root.append(wrap); return select;
}

export class AgentLearningView {
  constructor(root, {detail, api, transport, state, changed}) {
    Object.assign(this,{root,detail,api,transport,state,changed});
    this.state.disclosures ||= new Map();
    this.abort = new AbortController(); this.drafts = new Map(); this.saving = false; this.loadGeneration = 0;
    this.render();
  }
  get dirty() { return this.saving || this.drafts.size > 0; }
  close() { this.abort.abort(); }
  get current() { return !this.abort.signal.aborted && this.root.isConnected; }
  disclosure(root, title, key, open = false) {
    const node = disclosure(root,title), saved = this.state.disclosures;
    if (!saved.has(key)) saved.set(key,open);
    node.open = saved.get(key);
    node.addEventListener('toggle',()=>{if(this.current && node.isConnected)saved.set(key,node.open);});
    return node;
  }
  track(form, key) {
    form.dataset.draftKey = key;
    const prior = this.drafts.get(key);
    if (prior) for (const [name,value] of Object.entries(prior)) if (form.elements[name]) form.elements[name].value = value;
    const remember = () => this.drafts.set(key,Object.fromEntries([...form.elements].filter(node => node.name).map(node => [node.name,node.value])));
    form.addEventListener('input',remember); form.addEventListener('change',remember);
  }
  async save(form, action, refresh = false) {
    if (this.saving) return;
    const message = form.querySelector('[role="status"]');
    this.saving = true;
    const buttons = [...form.querySelectorAll('button')]; buttons.forEach(node => node.disabled = true);
    try {
      if (message) message.textContent = '正在保存…';
      await action();
      if (!this.current) return;
      this.drafts.delete(form.dataset.draftKey);
      document.activeElement?.blur();
      if (message) message.textContent = '操作完成。';
      this.saving = false;
      if (refresh) await this.changed();
    } catch (error) {
      if (this.current && message) message.textContent = `${error.message}${error.status === 409 ? '。内容已更新，请重新加载后核对。' : ''}`;
    } finally { this.saving = false; buttons.forEach(node => node.disabled = false); }
  }
  message(form) { const node = element('p','muted small'); node.setAttribute('role','status'); form.append(node); return node; }
  render() {
    const intro = panel(this.root,'学习与人工反馈',true);
    intro.append(element('p','muted','从执行证据中积累事实、经验和方法，结合人工审核与明确原则，指导后续任务。'));
    cards(intro,[['待人工决策',number((this.detail.pending_decisions || []).length)],
      ['有效人工指导',number(this.detail.guidance.filter(row => row.state === 'active').length)],
      ['学习记录',number(this.detail.learning.length)]]);
    this.renderDecisions();
    this.renderLibrary();
    this.renderGuidance();
    this.renderHistory();
  }
  renderDecisions() {
    const root = panel(this.root,'待人工决策',true); root.classList.add('agent-decisions');
    root.append(element('p','muted small','执行中遇到影响任务方向、且缺少偏好或约束的选择时，Agent 会在这里提问。回答用于当前任务；可复用的原则请保存到下方“人工原则与方法”。'));
    const decisions = this.detail.pending_decisions || [];
    if (!decisions.length) root.append(element('p','empty-state','当前没有待回答的问题。'));
    for (const item of decisions) {
      const request = item.request, row = element('article','agent-learning-entry');
      row.append(badge(request.kind === 'recovery' ? '执行恢复' : '执行中提问'),element('h4','',request.question),
        element('p','muted small',item.objective || ''),link('查看执行上下文',`#/sessions/${encodeURIComponent(item.session_id)}`));
      root.append(row);
      if (request.kind === 'recovery') { row.append(element('p','muted','请进入执行页面核实操作结果并完成恢复。')); continue; }
      const form = element('form','agent-form');
      const answer = field(form,'answer','你的选择或补充说明',true); answer.required = true;
      const options = element('div','agent-learning-actions');
      for (const text of request.options || []) options.append(button(text,()=>{
        answer.value = text; answer.dispatchEvent(new answer.ownerDocument.defaultView.Event('input',{bubbles:true})); answer.focus();
      },'quiet agent-decision-option'));
      if (options.children.length) form.prepend(options);
      this.track(form,`decision:${request.id}`);
      const selected = () => { for(const option of options.children) option.setAttribute('aria-pressed',String(option.textContent === answer.value)); };
      answer.addEventListener('input',selected);selected();
      const submit = element('button','primary','提交回答'); submit.type = 'submit'; form.append(submit); this.message(form);
      form.addEventListener('submit',event=>{
        event.preventDefault(); if (!answer.value.trim()) return;
        this.save(form,()=>this.transport.json(`/v1/sessions/${encodeURIComponent(item.session_id)}/commands`,{body:{
          command_id:uuid(),type:'answer_input',expected_task_revision:item.task_revision,payload:{request_id:request.id,answer:answer.value},
        }}),true);
      });
      row.append(form);
    }
  }
  renderLibrary() {
    const root = panel(this.root,'已学知识与审核',true); root.classList.add('agent-memory-library');
    root.append(element('p','muted small','未审核时沿用评估置信度；人工确认或更正后，任务上下文会明确标注人工来源并提高参考优先级。原始评估、适用范围和证据仍保留。'));
    const form = element('form','agent-learning-filters');
    const query = field(form,'query','搜索已学内容'); query.value = this.state.query || '';
    choice(form,'kind','内容类型',[['','全部类型'],...Object.entries(kinds)],this.state.kind);
    choice(form,'review','审核状态',[['','全部状态'],['unreviewed','待人工审核'],['reviewed','人工已审核']],this.state.review);
    if (!this.detail.role.working_directory) {
      choice(form,'session_id','所属 Session',this.detail.sessions.map(row=>[row.session_id,row.session_id]),this.state.session_id);
      root.append(element('p','muted small','未绑定工作目录，仅展示所选 Session 的学习内容。'));
    }
    const submit = element('button','quiet','查找 / 刷新'); submit.type = 'submit'; form.append(submit);
    form.addEventListener('submit',event=>{event.preventDefault();this.state.offset = 0;this.loadLibrary();});
    form.addEventListener('change',event=>{if(event.target.tagName === 'SELECT'){this.state.offset = 0;this.loadLibrary();}});
    root.append(form); this.filters = form;
    this.libraryStatus = element('p','muted small'); this.libraryStatus.setAttribute('role','status');
    this.results = element('div','agent-memory-results'); this.pagination = element('div','agent-learning-actions');
    root.append(this.libraryStatus,this.results,this.pagination);
    this.loadLibrary();
  }
  async loadLibrary() {
    const generation = ++this.loadGeneration;
    for (const input of this.filters.elements) if(input.name) this.state[input.name] = input.value;
    if (!this.detail.role.working_directory && !this.state.session_id) {
      this.libraryStatus.textContent = '执行产生学习记录后，可在这里按 Session 查看。'; return;
    }
    const params = new URLSearchParams({offset:this.state.offset || 0,limit:20});
    for(const name of ['query','kind','review','session_id']) if(this.state[name])params.set(name,this.state[name]);
    this.libraryStatus.textContent = '正在加载已学内容…';
    try {
      const answer = await this.api.get(this.detail.id,`memory/library?${params}`,this.abort.signal);
      if (!this.current || generation !== this.loadGeneration) return;
      this.results.replaceChildren(); this.pagination.replaceChildren();
      const records = answer.records || [];
      this.libraryStatus.textContent = `共 ${number(answer.total ?? records.length)} 条${answer.truncated ? ' · 当前仅展示最近的部分记录' : ''}${answer.issues?.length ? ' · 部分来源不可用' : ''}`;
      for (const memory of records) this.results.append(this.memoryCard(memory));
      if (!records.length) this.results.append(element('p','empty-state','当前范围内没有匹配的已学内容。'));
      if (this.state.offset > 0) this.pagination.append(button('上一页',()=>{this.state.offset = Math.max(0,this.state.offset-20);this.loadLibrary();}));
      if (answer.next_offset != null) this.pagination.append(button('下一页',()=>{this.state.offset = answer.next_offset;this.loadLibrary();}));
    } catch (error) {if(this.current && generation === this.loadGeneration) this.libraryStatus.textContent = `加载失败：${error.message}。可点击“查找 / 刷新”重试。`;}
  }
  memoryCard(memory) {
    const row = element('article','agent-learning-entry'), review = memory.review_state || 'unreviewed';
    const meta = element('div','agent-learning-meta');
    meta.append(badge(kinds[memory.kind] || memory.kind),badge(reviews[review] || review,review === 'unreviewed' ? '' : 'success'),
      element('span','muted small',`评估置信度：${confidence[memory.confidence] || '未知'}`));
    if (review !== 'unreviewed') meta.append(element('span','agent-review-priority','人工审核 · 高优先级参考'));
    if (memory.pinned) meta.append(badge('优先参考'));
    for (const tag of memory.tags || []) meta.append(badge(tag));
    row.append(meta,element('h4','',memory.title),element('p','',memory.summary),element('p','muted small',`适用范围：${memory.scope}`));
    const identity = JSON.stringify([memory.source.evaluation_id,memory.source.record_id]);
    const evidence = this.disclosure(row,'查看证据与方法细节',`evidence:${identity}`);
    const epistemic = {observed:'已观察',inferred:'推断',human_asserted:'人工更正',observed_recovery:'已观察到恢复',proposed:'待验证提案'};
    for (const [key,title] of [['basis','事实依据'],['validation','验证状态'],['revalidate_when','需重新验证的条件'],['preconditions','前提'],
      ['trigger','触发条件'],['failed_approach','失败尝试'],['failure_reason','原因'],['better_approach','改进方法'],['avoid','避免'],['preserve','保留的检查'],['expected_benefit','预期收益']]) {
      if(memory[key]) evidence.append(element('p','small',`${title}：${epistemic[memory[key]] || memory[key]}`));
    }
    if (memory.steps?.length) {const list=element('ol');for(const step of memory.steps)list.append(element('li','',typeof step === 'string' ? step : step.text || JSON.stringify(step)));evidence.append(list);}
    for (const citation of memory.citations || []) {
      const quote = element('blockquote','agent-memory-quote',citation.quote); quote.append(element('small','muted',`来源：${citation.ref}`)); evidence.append(quote);
    }
    if (memory.human_review) evidence.append(element('p','muted small',`人工审核时间：${memory.human_review.created_at}`));
    evidence.append(link('打开原始 Analysis 与证据',`#/evaluation/${encodeURIComponent(memory.source.session_id)}`));
    const editor = this.disclosure(row,'人工审核与标注',`review:${identity}`), form = element('form','agent-form');
    const action = choice(form,'kind','审核操作',[
      ['confirm','确认有效'],['correct','更正内容'],['scope','限定适用范围'],['tag','添加标签'],['pin','优先参考'],['reject','停止使用'],
    ]);
    const value = field(form,'value','更正内容、范围、标签或审核备注',true); value.maxLength = 4000;
    const key = `memory:${memory.source.evaluation_id}:${memory.source.record_id}`;
    this.track(form,key); if(this.drafts.has(key))editor.open = true;
    const submit = element('button','primary','保存审核'); submit.type = 'submit';
    form.append(submit,button('取消编辑',()=>{this.drafts.delete(key);form.reset();editor.open=false;})); this.message(form); editor.append(form);
    form.addEventListener('submit',async event=>{
      event.preventDefault(); const needsValue = ['correct','scope','tag'].includes(action.value);
      if (needsValue && !value.value.trim()) {form.querySelector('[role="status"]').textContent='请填写更正内容、适用范围或标签。';return;}
      await this.save(form,async()=>{
        await this.api.post(this.detail.id,'memory/annotations',{command_id:uuid(),evaluation_id:memory.source.evaluation_id,
          record_id:memory.source.record_id,content_digest:memory.source.content_digest,expected_revision:memory.source.annotation_revision || 0,
          kind:action.value,...(needsValue ? {value:value.value} : {reason:value.value})});
        this.drafts.delete(key); await this.loadLibrary();
      });
    });
    return row;
  }
  renderGuidance() {
    const root = panel(this.root,'人工原则与方法',true); root.classList.add('agent-human-guidance');
    root.append(element('p','muted small','明确的人类原则直接指导任务；方法的实际有效性仍由后续执行证据验证。'));
    if (this.detail.role.methods.length) {
      const defaults = this.disclosure(root,'角色默认方法','default-methods');
      const list = element('ul'); for(const method of this.detail.role.methods)list.append(element('li','',method)); defaults.append(list);
    }
    for (const entry of this.detail.guidance) {
      const row = element('article','agent-learning-entry');
      row.append(badge(entry.method_version_id ? '人工方法' : '人工原则'),element('p','',entry.text),element('p','muted small',
        `${entry.scope === 'persistent' ? '长期使用' : '仅当前任务'} · ${entry.state === 'active' ? '生效中' : '已停用'} · ${entry.applied_run_ids?.length ? '已应用' : '待下一次模型请求生效'}`));
      const form = element('form'); this.message(form);
      form.append(button(entry.state === 'active' ? '停用' : '恢复',()=>this.save(form,()=>this.api.post(this.detail.id,'profile/commands',{
        command_id:uuid(),type:entry.state === 'active' ? 'retire' : 'restore',entry_id:entry.id,expected_revision:entry.revision,
      }),true)));row.append(form);root.append(row);
    }
    const editor = this.disclosure(root,'添加原则或导入方法','guidance',!this.detail.guidance.length), form = element('form','agent-form');
    const text = field(form,'text','原则或方法内容',true); text.maxLength = 8000;
    const document = field(form,'document','方法文档路径（可选，相对工作目录）');
    const scope = choice(form,'scope','生效范围',[['persistent','当前 Agent 长期使用'],['current_task','仅当前任务']]);
    const work = choice(form,'work','当前任务',this.detail.work_packages.filter(row=>!['succeeded','cancelled','failed'].includes(row.state)).map(row=>[row.id,row.objective]));
    const update = ()=>{work.parentElement.hidden=scope.value !== 'current_task';}; scope.addEventListener('change',update);
    this.track(form,'guidance');update();
    const submit = async method => {
      if ((!method || !document.value.trim()) && !text.value.trim()) {form.querySelector('[role="status"]').textContent='请填写内容或方法文档路径。';return;}
      if(scope.value === 'current_task' && !work.value){form.querySelector('[role="status"]').textContent='当前没有可接收指导的任务，请选择长期使用。';return;}
      await this.save(form,()=>this.api.post(this.detail.id,method ? 'methods/import' : 'guidance',{
        command_id:uuid(),scope:scope.value,...(scope.value === 'current_task' ? {work_package_id:work.value} : {}),
        ...(method ? {title:'手工方法',method_text:text.value,...(document.value.trim() ? {document_path:document.value} : {})} : {text:text.value}),
      }),true);
    };
    form.append(button('保存原则',()=>submit(false),'primary'),button('导入为方法论',()=>submit(true)));this.message(form);
    form.addEventListener('submit',event=>{event.preventDefault();submit(false);});editor.append(form);
  }
  renderHistory() {
    const root = panel(this.root,'学习记录与追溯',true);
    const cycles = this.disclosure(root,`自动学习记录 · ${number(this.detail.learning.length)} 条`,'cycles');
    if (!this.detail.learning.length) cycles.append(element('p','empty-state','执行结束后将在这里显示自动评估与记忆提取结果。'));
    for (const cycle of this.detail.learning) {
      const row = element('article','agent-learning-entry');
      row.append(element('h4','',`学习记录 · ${label(cycle.state)} · ${label(cycle.coverage)}`),element('p','small',`已接受记忆：${cycle.memory_result?.accepted || 0}`));
      if(cycle.blocked_reason)row.append(element('p','agent-notice',cycle.blocked_reason));
      row.append(link('查看 Analysis 与证据',`#/evaluation/${encodeURIComponent(cycle.session_id)}`));
      if (['failed','deferred','cancelled'].includes(cycle.state) || cycle.state === 'completed' && cycle.coverage === 'partial') {
        const form = element('form'); this.message(form);
        form.append(button('重试缺口',()=>this.save(form,()=>this.api.post(this.detail.id,`learning/${encodeURIComponent(cycle.id)}/commands`,{command_id:uuid(),type:'retry'}),true)));
        row.append(form);
      }
      cycles.append(row);
    }
    const audit = this.disclosure(root,'审核历史与实际上下文','audit'), form=element('form'), output=element('div');this.message(form);
    form.append(button('查看标注历史与恢复',()=>this.save(form,async()=>{
      const {annotations}=await this.api.get(this.detail.id,'memory/annotations',this.abort.signal);if(!this.current)return;
      output.replaceChildren();const latest=new Map();
      for(const entry of annotations){const key=JSON.stringify([entry.source.evaluation_id,entry.source.record_id]);if(!latest.has(key))latest.set(key,entry);}
      for(const entry of latest.values()){
        const row=element('article','agent-learning-entry');row.append(element('p','',`${entry.kind} · ${entry.value || entry.reason || entry.source.record_id}`));
        if(['reject','retire'].includes(entry.kind)){
          const restore=element('form');this.message(restore);restore.append(button('恢复使用',()=>this.save(restore,async()=>{
            await this.api.post(this.detail.id,'memory/annotations',{command_id:uuid(),evaluation_id:entry.source.evaluation_id,record_id:entry.source.record_id,
              content_digest:entry.source.content_digest,expected_revision:entry.revision,kind:'restore'});
            row.remove();await this.loadLibrary();
          })));row.append(restore);
        }output.append(row);
      }
      if(!latest.size)output.append(element('p','empty-state','暂无人工标注。'));
    })),button('查看实际使用的上下文',()=>this.save(form,async()=>{
      const {context}=await this.api.get(this.detail.id,'context',this.abort.signal);if(!this.current)return;output.replaceChildren();
      for(const item of context.slice(-10)){
        const row=element('article','agent-learning-entry'), inspect=element('form');this.message(inspect);
        inspect.append(button(`${item.purpose} · ${item.run_id || '编排'}`,()=>this.save(inspect,async()=>{
          const value=await this.api.get(this.detail.id,`context/${encodeURIComponent(item.id)}`,this.abort.signal);
          if(this.current){row.querySelector('pre')?.remove();row.append(element('pre','agent-context-json',JSON.stringify(value,null,2)));}
        })));row.append(inspect);output.append(row);
      }
      if(!context.length)output.append(element('p','empty-state','暂无已使用的上下文。'));
    })));
    audit.append(form,output);
  }
}
