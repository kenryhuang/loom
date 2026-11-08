import {element} from '../markdown.mjs';
import {panel, cards, badge, number, disclosure} from './dashboard.mjs';

const kinds = {facts:'Facts',lessons:'Lessons',workflows:'Methods'};
const confidence = {high:'High',medium:'Medium',low:'Low',unknown:'Unknown'};
const reviews = {unreviewed:'Awaiting human review',confirmed:'Confirmed by human',corrected:'Corrected by human'};
const states = {pending:'Pending',running:'Running',completed:'Completed',incomplete:'Incomplete (left queue)',failed:'Failed',deferred:'Deferred',cancelled:'Cancelled',
  full:'Full',partial:'Partial',none:'None',not_applicable:'No analyzable evidence'};
const label = value => states[value] || value || '—';
const uuid = () => globalThis.crypto.randomUUID();
function button(title, action, className = 'quiet') {
  const node = element('button',className,title); node.type = 'button'; node.addEventListener('click',action); return node;
}
function link(title, href) { const node = element('a','',title); node.href = href; return node; }
function analysisHref(source) {
  const query = new URLSearchParams({analysis:source.analysis_id});
  if(source.evaluation_id)query.set('evaluation',source.evaluation_id);
  if(source.run_id)query.set('run',source.run_id);
  return `#/evaluation/${encodeURIComponent(source.session_id)}?${query}`;
}
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
      if (message) message.textContent = 'Saving…';
      await action();
      if (!this.current) return;
      this.drafts.delete(form.dataset.draftKey);
      document.activeElement?.blur();
      if (message) message.textContent = 'Done.';
      this.saving = false;
      if (refresh) await this.changed();
    } catch (error) {
      if (this.current && message) message.textContent = `${error.message}${error.status === 409 ? ' Content changed; reload and verify.' : ''}`;
    } finally { this.saving = false; buttons.forEach(node => node.disabled = false); }
  }
  message(form) { const node = element('p','muted small'); node.setAttribute('role','status'); form.append(node); return node; }
  render() {
    const intro = panel(this.root,'Learning & human feedback',true);
    intro.append(element('p','muted','Builds facts, lessons, and methods from execution evidence; combined with human review and explicit principles, they guide later tasks.'));
    cards(intro,[['Pending decisions',number((this.detail.pending_decisions || []).length)],
      ['Active human guidance',number(this.detail.guidance.filter(row => row.state === 'active').length)],
      ['Learning records',number(this.detail.learning.length)]]);
    this.renderDecisions();
    this.renderLibrary();
    this.renderGuidance();
    this.renderHistory();
  }
  renderDecisions() {
    const root = panel(this.root,'Pending human decisions',true); root.classList.add('agent-decisions');
    root.append(element('p','muted small','When execution hits a choice that affects task direction and no preference or constraint covers it, the Agent asks here. Answers apply to the current task; save reusable principles under "Human principles & methods" below.'));
    const decisions = this.detail.pending_decisions || [];
    if (!decisions.length) root.append(element('p','empty-state','No questions waiting for an answer.'));
    for (const item of decisions) {
      const request = item.request, row = element('article','agent-learning-entry');
      row.append(badge(request.kind === 'recovery' ? 'Execution recovery' : 'Question during execution'),element('h4','',request.question),
        element('p','muted small',item.objective || ''),link('View execution context',`#/sessions/${encodeURIComponent(item.session_id)}`));
      root.append(row);
      if (request.kind === 'recovery') { row.append(element('p','muted','Open the execution page to verify the operation results and complete recovery.')); continue; }
      const form = element('form','agent-form');
      const answer = field(form,'answer','Your choice or additional notes',true); answer.required = true;
      const options = element('div','agent-learning-actions');
      for (const text of request.options || []) options.append(button(text,()=>{
        answer.value = text; answer.dispatchEvent(new answer.ownerDocument.defaultView.Event('input',{bubbles:true})); answer.focus();
      },'quiet agent-decision-option'));
      if (options.children.length) form.prepend(options);
      this.track(form,`decision:${request.id}`);
      const selected = () => { for(const option of options.children) option.setAttribute('aria-pressed',String(option.textContent === answer.value)); };
      answer.addEventListener('input',selected);selected();
      const submit = element('button','primary','Submit answer'); submit.type = 'submit'; form.append(submit); this.message(form);
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
    const root = panel(this.root,'Learned knowledge & review',true); root.classList.add('agent-memory-library');
    root.append(element('p','muted small','Before review, entries keep their evaluation confidence; after human confirmation or correction, task context explicitly marks the human source and raises reference priority. The original evaluation, scope, and evidence are preserved.'));
    const form = element('form','agent-learning-filters');
    const query = field(form,'query','Search learned content'); query.value = this.state.query || '';
    choice(form,'kind','Content type',[['','All types'],...Object.entries(kinds)],this.state.kind);
    choice(form,'review','Review state',[['','All states'],['unreviewed','Awaiting human review'],['reviewed','Human reviewed']],this.state.review);
    if (!this.detail.role.working_directory) {
      choice(form,'session_id','Owning Session',this.detail.sessions.map(row=>[row.session_id,row.session_id]),this.state.session_id);
      root.append(element('p','muted small','No workspace bound; showing learned content for the selected Session only.'));
    }
    const submit = element('button','quiet','Search / refresh'); submit.type = 'submit'; form.append(submit);
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
      this.libraryStatus.textContent = 'Once execution produces learning records, you can browse them here by Session.'; return;
    }
    const params = new URLSearchParams({offset:this.state.offset || 0,limit:20});
    for(const name of ['query','kind','review','session_id']) if(this.state[name])params.set(name,this.state[name]);
    this.libraryStatus.textContent = 'Loading learned content…';
    try {
      const answer = await this.api.get(this.detail.id,`memory/library?${params}`,this.abort.signal);
      if (!this.current || generation !== this.loadGeneration) return;
      this.results.replaceChildren(); this.pagination.replaceChildren();
      const records = answer.records || [];
      this.libraryStatus.textContent = `${number(answer.total ?? records.length)} total${answer.truncated ? ' · showing only the most recent records' : ''}${answer.issues?.length ? ' · some sources unavailable' : ''}`;
      for (const memory of records) this.results.append(this.memoryCard(memory));
      if (!records.length) this.results.append(element('p','empty-state','No learned content matches the current filters.'));
      if (this.state.offset > 0) this.pagination.append(button('Previous page',()=>{this.state.offset = Math.max(0,this.state.offset-20);this.loadLibrary();}));
      if (answer.next_offset != null) this.pagination.append(button('Next page',()=>{this.state.offset = answer.next_offset;this.loadLibrary();}));
    } catch (error) {if(this.current && generation === this.loadGeneration) this.libraryStatus.textContent = `Load failed: ${error.message}. Use "Search / refresh" to retry.`;}
  }
  memoryCard(memory) {
    const row = element('article','agent-learning-entry'), review = memory.review_state || 'unreviewed';
    const meta = element('div','agent-learning-meta');
    meta.append(badge(kinds[memory.kind] || memory.kind),badge(reviews[review] || review,review === 'unreviewed' ? '' : 'success'),
      element('span','muted small',`Evaluation confidence: ${confidence[memory.confidence] || 'Unknown'}`));
    if (review !== 'unreviewed') meta.append(element('span','agent-review-priority','Human reviewed · high-priority reference'));
    if (memory.pinned) meta.append(badge('Priority reference'));
    for (const tag of memory.tags || []) meta.append(badge(tag));
    row.append(meta,element('h4','',memory.title),element('p','',memory.summary),element('p','muted small',`Scope: ${memory.scope}`));
    const identity = JSON.stringify([memory.source.evaluation_id,memory.source.record_id]);
    const evidence = this.disclosure(row,'View evidence & method details',`evidence:${identity}`);
    const epistemic = {observed:'Observed',inferred:'Inferred',human_asserted:'Human corrected',observed_recovery:'Recovery observed',proposed:'Proposal pending validation'};
    for (const [key,title] of [['basis','Factual basis'],['validation','Validation state'],['revalidate_when','Revalidate when'],['preconditions','Preconditions'],
      ['trigger','Trigger'],['failed_approach','Failed approach'],['failure_reason','Reason'],['better_approach','Better approach'],['avoid','Avoid'],['preserve','Checks to preserve'],['expected_benefit','Expected benefit']]) {
      if(memory[key]) evidence.append(element('p','small',`${title}: ${epistemic[memory[key]] || memory[key]}`));
    }
    if (memory.steps?.length) {const list=element('ol');for(const step of memory.steps)list.append(element('li','',typeof step === 'string' ? step : step.text || JSON.stringify(step)));evidence.append(list);}
    for (const citation of memory.citations || []) {
      const quote = element('blockquote','agent-memory-quote',citation.quote); quote.append(element('small','muted',`Source: ${citation.ref}`)); evidence.append(quote);
    }
    if (memory.human_review) evidence.append(element('p','muted small',`Human reviewed at: ${memory.human_review.created_at}`));
    if(memory.source.session_id && memory.source.analysis_id)
      evidence.append(link('Open the original Analysis & evidence',analysisHref(memory.source)));
    else if(memory.source.session_id)
      evidence.append(link('Open the source Session (analysis unavailable)',`#/sessions/${encodeURIComponent(memory.source.session_id)}`));
    const editor = this.disclosure(row,'Human review & annotation',`review:${identity}`), form = element('form','agent-form');
    const action = choice(form,'kind','Review action',[
      ['confirm','Confirm valid'],['correct','Correct content'],['scope','Limit scope'],['tag','Add tag'],['pin','Priority reference'],['reject','Stop using'],
    ]);
    const value = field(form,'value','Correction, scope, tag, or review note',true); value.maxLength = 4000;
    const key = `memory:${memory.source.evaluation_id}:${memory.source.record_id}`;
    this.track(form,key); if(this.drafts.has(key))editor.open = true;
    const submit = element('button','primary','Save review'); submit.type = 'submit';
    form.append(submit,button('Discard edits',()=>{this.drafts.delete(key);form.reset();editor.open=false;})); this.message(form); editor.append(form);
    form.addEventListener('submit',async event=>{
      event.preventDefault(); const needsValue = ['correct','scope','tag'].includes(action.value);
      if (needsValue && !value.value.trim()) {form.querySelector('[role="status"]').textContent='Enter the correction, scope, or tag.';return;}
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
    const root = panel(this.root,'Human principles & methods',true); root.classList.add('agent-human-guidance');
    root.append(element('p','muted small','Explicit human principles guide tasks directly; the actual effectiveness of methods is still validated by later execution evidence.'));
    if (this.detail.role.methods.length) {
      const defaults = this.disclosure(root,'Role default methods','default-methods');
      const list = element('ul'); for(const method of this.detail.role.methods)list.append(element('li','',method)); defaults.append(list);
    }
    for (const entry of this.detail.guidance) {
      const row = element('article','agent-learning-entry');
      row.append(badge(entry.method_version_id ? 'Human method' : 'Human principle'),element('p','',entry.text),element('p','muted small',
        `${entry.scope === 'persistent' ? 'Long-term use' : 'Current task only'} · ${entry.state === 'active' ? 'Active' : 'Retired'} · ${entry.applied_run_ids?.length ? 'Applied' : 'Takes effect on the next model request'}`));
      const form = element('form'); this.message(form);
      form.append(button(entry.state === 'active' ? 'Retire' : 'Restore',()=>this.save(form,()=>this.api.post(this.detail.id,'profile/commands',{
        command_id:uuid(),type:entry.state === 'active' ? 'retire' : 'restore',entry_id:entry.id,expected_revision:entry.revision,
      }),true)));row.append(form);root.append(row);
    }
    const editor = this.disclosure(root,'Add a principle or import a method','guidance',!this.detail.guidance.length), form = element('form','agent-form');
    const text = field(form,'text','Principle or method content',true); text.maxLength = 8000;
    const document = field(form,'document','Method document path (optional, relative to the workspace)');
    const scope = choice(form,'scope','Scope',[['persistent','Long-term use for this Agent'],['current_task','Current task only']]);
    const work = choice(form,'work','Current task',this.detail.work_packages.filter(row=>!['succeeded','cancelled','failed'].includes(row.state)).map(row=>[row.id,row.objective]));
    const update = ()=>{work.parentElement.hidden=scope.value !== 'current_task';}; scope.addEventListener('change',update);
    this.track(form,'guidance');update();
    const submit = async method => {
      if ((!method || !document.value.trim()) && !text.value.trim()) {form.querySelector('[role="status"]').textContent='Enter content or a method document path.';return;}
      if(scope.value === 'current_task' && !work.value){form.querySelector('[role="status"]').textContent='No task can receive guidance right now; choose long-term use.';return;}
      await this.save(form,()=>this.api.post(this.detail.id,method ? 'methods/import' : 'guidance',{
        command_id:uuid(),scope:scope.value,...(scope.value === 'current_task' ? {work_package_id:work.value} : {}),
        ...(method ? {title:'Manual method',method_text:text.value,...(document.value.trim() ? {document_path:document.value} : {})} : {text:text.value}),
      }),true);
    };
    form.append(button('Save principle',()=>submit(false),'primary'),button('Import as method',()=>submit(true)));this.message(form);
    form.addEventListener('submit',event=>{event.preventDefault();submit(false);});editor.append(form);
  }
  renderHistory() {
    const root = panel(this.root,'Learning history & traceability',true);
    const cycles = this.disclosure(root,`Automatic learning records · ${number(this.detail.learning.length)}`,'cycles');
    if (!this.detail.learning.length) cycles.append(element('p','empty-state','Automatic evaluation and memory extraction results appear here after execution finishes.'));
    for (const cycle of this.detail.learning) {
      const row = element('article','agent-learning-entry');
      row.append(element('h4','',`Learning record · ${label(cycle.state)} · ${label(cycle.coverage)}`),element('p','small',`Memories accepted: ${cycle.memory_result?.accepted || 0}`));
      if(cycle.blocked_reason)row.append(element('p','agent-notice',cycle.blocked_reason));
      if(cycle.session_id && cycle.analysis_id)row.append(link('View Analysis & evidence',analysisHref(cycle)));
      else row.append(element('p','muted small','No Analysis available yet.'));
      if (['incomplete','failed','deferred','cancelled'].includes(cycle.state) || cycle.state === 'completed' && cycle.coverage === 'partial') {
        const form = element('form'); this.message(form);
        form.append(button('Retry gaps',()=>this.save(form,()=>this.api.post(this.detail.id,`learning/${encodeURIComponent(cycle.id)}/commands`,{command_id:uuid(),type:'retry'}),true)));
        row.append(form);
      }
      cycles.append(row);
    }
    const audit = this.disclosure(root,'Review history & actual context','audit'), form=element('form'), output=element('div');this.message(form);
    form.append(button('View annotation history & restores',()=>this.save(form,async()=>{
      const {annotations}=await this.api.get(this.detail.id,'memory/annotations',this.abort.signal);if(!this.current)return;
      output.replaceChildren();const latest=new Map();
      for(const entry of annotations){const key=JSON.stringify([entry.source.evaluation_id,entry.source.record_id]);if(!latest.has(key))latest.set(key,entry);}
      for(const entry of latest.values()){
        const row=element('article','agent-learning-entry');row.append(element('p','',`${entry.kind} · ${entry.value || entry.reason || entry.source.record_id}`));
        if(['reject','retire'].includes(entry.kind)){
          const restore=element('form');this.message(restore);restore.append(button('Restore use',()=>this.save(restore,async()=>{
            await this.api.post(this.detail.id,'memory/annotations',{command_id:uuid(),evaluation_id:entry.source.evaluation_id,record_id:entry.source.record_id,
              content_digest:entry.source.content_digest,expected_revision:entry.revision,kind:'restore'});
            row.remove();await this.loadLibrary();
          })));row.append(restore);
        }output.append(row);
      }
      if(!latest.size)output.append(element('p','empty-state','No human annotations yet.'));
    })),button('View context actually used',()=>this.save(form,async()=>{
      const {context}=await this.api.get(this.detail.id,'context',this.abort.signal);if(!this.current)return;output.replaceChildren();
      for(const item of context.slice(-10)){
        const row=element('article','agent-learning-entry'), inspect=element('form');this.message(inspect);
        inspect.append(button(`${item.purpose} · ${item.run_id || 'orchestration'}`,()=>this.save(inspect,async()=>{
          const value=await this.api.get(this.detail.id,`context/${encodeURIComponent(item.id)}`,this.abort.signal);
          if(this.current){row.querySelector('pre')?.remove();row.append(element('pre','agent-context-json',JSON.stringify(value,null,2)));}
        })));row.append(inspect);output.append(row);
      }
      if(!context.length)output.append(element('p','empty-state','No context used yet.'));
    })));
    audit.append(form,output);
  }
}
