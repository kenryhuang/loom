import {element} from '../markdown.mjs';
import {panel,badge,number,table} from './dashboard.mjs';
import {roadmapDisclosure} from './agent-roadmap.mjs';

const states={pending:'Waiting for scheduling',dispatched:'Waiting for an execution slot',running:'Running',succeeded:'Accepted',incomplete:'Incomplete',failed:'Failed',blocked:'Needs attention',cancelled:'Cancelled',
  completed:'Completed',deferred:'Waiting to resume',paused:'Paused',pausing:'Pausing',resuming:'Resuming',finished:'Finished'};
const phases={snapshot:'Preparing snapshot',factual_analysis:'Factual analysis',deep_eval:'Deep Eval',feedback:'Analysis complete'};
const label=s=>states[s] || s || '—';
const time=t=>t?new Date(t).toLocaleString(undefined,{hour12:false}):'—';
const button=(name,fn)=>{const b=element('button','quiet',name);b.type='button';b.addEventListener('click',fn);return b;};
const link=(name,href)=>{const a=element('a','',name);a.href=href;return a;};
const stamp=w=>w.last_slice_at || w.started_at || w.dispatched_at || w.created_at;
const newest=(a,b)=>(Date.parse(stamp(b)) || 0)-(Date.parse(stamp(a)) || 0);
const cycleNewest=(a,b)=>(b.queue_order || 0)-(a.queue_order || 0) || (Date.parse(b.enqueued_at || b.created_at) || 0)-(Date.parse(a.enqueued_at || a.created_at) || 0);
const compact=(value,cls='')=>{const p=element('p',`agent-queue-summary ${cls}`,value);p.title=value || '';return p;};
const cell=()=>element('div','agent-queue-cell');
function analysisLink(c,deep=false) {
  const query=new URLSearchParams();
  if(c.analysis_id)query.set('analysis',c.analysis_id);
  if(deep && c.evaluation_id)query.set('evaluation',c.evaluation_id);
  if(c.run_id)query.set('run',c.run_id);
  return link(deep?'Deep Eval':'Analysis',`#/evaluation/${encodeURIComponent(c.session_id)}${query.size?'?'+query:''}`);
}
function records(c) {
  const refs=cell();
  if(c.session_id){refs.append(link('Session',`#/sessions/${encodeURIComponent(c.session_id)}`));
    if(c.analysis_id)refs.append(analysisLink(c));
    if(c.evaluation_id)refs.append(analysisLink(c,true));}
  if(!refs.childNodes.length)refs.append(element('span','muted','Session not created yet'));
  return refs;
}
function queueTable(root,headers,items,render,kind) {
  const node=table(root,headers,items.map(render));node.classList.add('agent-queue-table');node.dataset.queueTable=kind;
  [...node.tBodies[0].rows].forEach((row,i)=>{if(items[i])row.dataset.queueId=items[i].id;});
}
function queueSections(root,d,view,key,items,current,headers,render,isPending) {
  const active=items.filter(i=>current.has(i.id)),waiting=items.filter(i=>!current.has(i.id) && isPending(i)),history=items.filter(i=>!current.has(i.id) && !isPending(i));
  if(active.length)queueTable(root,headers,active,render,'current');
  else root.append(element('p','muted agent-queue-empty','No tasks in progress right now.'));
  for(const [kind,title,rows] of [['waiting','Waiting queue',waiting],['history','History',history]]){
    if(!rows.length)continue;
    const detail=roadmapDisclosure(view,root,d,`${key}:${kind}`,`${title} · ${rows.length}`);
    detail.dataset.queueSection=kind;
    queueTable(detail,headers,rows,render,kind);
  }
}
function blockerDetails(root,items,d,view) {
  for(const item of items){
    root.append(element('p','',item.reason));view.showRows(root,item.details || []);
    const steps=element('ol');for(const step of item.next_steps || [])steps.append(element('li','',step));root.append(steps);
    if(item.action==='refresh_sources')root.append(button('Refresh bound sources',()=>view.act(()=>view.api.post(d.id,'sources/refresh',{}))));
  }
}

export function renderExecution(root,d,view) {
  const execution=d.execution || {},policy=execution.policy || d.role.scheduling_policy || {},scheduler=execution.scheduler || {},lp=execution.learning_policy || d.role.learning_policy || {};
  const works=[...d.work_packages].sort(newest),cycles=[...d.learning].sort(cycleNewest),blockers=execution.blockers || [];
  const workPanel=panel(root,'Execution queue',true);workPanel.classList.add('agent-queue-panel');
  workPanel.firstChild.append(button('Scheduling settings',()=>view.createForm(d,'scheduling')));
  workPanel.append(element('p','muted small',`${policy.enabled?`Scheduled queue checks · every ${policy.tick_seconds}s`:'Manual scheduling'} · up to 1 hour per run${policy.enabled?' · next check '+time(execution.next_tick_at):''}`));
  const currentWork=new Set(works.filter(w=>['running','dispatched'].includes(w.state)).map(w=>w.id));
  if(!currentWork.size){const head=works.find(w=>w.id===scheduler.work_package_id && !['succeeded','cancelled'].includes(w.state)) || works.find(w=>w.state==='blocked');if(head)currentWork.add(head.id);}
  if(scheduler.manual_required)workPanel.append(element('p','agent-schedule-warning',`Manual intervention required: ${(scheduler.reason || 'See current task details').split(/[；;]/)[0]}`));
  else if(['checking','planning'].includes(scheduler.state))workPanel.append(element('p','muted',scheduler.state==='planning'?'Planning the next round of tasks.':'Checking the execution queue.'));
  queueSections(workPanel,d,view,'execution-queue',works,currentWork,['Task / time','Status / progress','Result / details','Linked records'],w=>{
    const task=cell(),state=cell(),result=cell(),refs=records(w),live=(execution.runtime || []).find(r=>r.work_package_id===w.id),context=w.latest_context;
    task.append(compact(w.objective),element('span','muted small',time(stamp(w))));
    state.append(badge(label(w.state),w.state));
    if(live)state.append(element('span','muted small',`${live.steps || 0} steps · ${Math.floor(live.active_seconds || 0)}s`));
    const issues=blockers.filter(b=>b.scope_id===w.id || b.scope_id===w.goal_id);
    const reason=w.state==='succeeded'?'Acceptance passed':(w.recovery?.exhausted || w.blocker?.code==='RECOVERY_EXHAUSTED'?'Suspended; waiting for manual recovery':w.blocked_reason) || issues[0]?.reason;
    if(reason && !['running','dispatched'].includes(w.state))result.append(compact(reason,w.state==='succeeded'?'muted':''));
    const detail=roadmapDisclosure(view,result,d,`execution:${w.id}`,'Details');
    detail.append(element('p','',w.objective),element('code','agent-roadmap-id',w.id));
    if(w.reason)detail.append(element('p','',`Orchestration rationale: ${w.reason}`));
    if(reason && w.state!=='succeeded')detail.append(element('p','',`Current blocker: ${reason}`));
    if(w.binding)detail.append(element('p','',`Binding: ${w.binding.source==='recommended'?'recommended':'fallback'}${w.binding.rationale?` · ${w.binding.rationale}`:w.binding.reason?` · ${w.binding.reason}`:''}`));
    detail.append(element('p','',`Shared daily token budget · ${w.recovery?.attempts || 0}/${Math.max(w.recovery?.attempt_limit || 0,policy.max_recovery_attempts ?? 3)} recoveries`));
    if(d.state==='enabled' && ['blocked','failed'].includes(w.state))refs.append(button('Re-evaluate recovery',()=>view.act(()=>view.api.post(d.id,'commands',{
      command_id:globalThis.crypto.randomUUID(),type:'recover',expected_revision:d.revision,
      payload:{work_package_id:w.id,reason:'User requested re-evaluating the recovery configuration against the current structured blocker'}
    }))));

    if(live)detail.append(element('p','',`Session: ${live.task_state} · run: ${live.run_state || '—'} · ${live.checkpoint_available?'checkpoint / recovery context available':'waiting for a recovery point'}`));
    if(w.slice?.phase)detail.append(element('p','muted',`Historical slice #${w.slice.number} · ${label(w.slice.phase)}`));
    if(context){detail.append(element('p','',`${context.memory_refs?.length || 0} related memories · ${context.guidance_ids?.length || 0} guidance entries · ${context.completed_learning_ids?.length || 0} completed analyses`));view.showRows(detail,(context.memory_refs || []).map(r=>JSON.stringify(r)));}
    if(w.run_ids?.length)view.showRows(detail,w.run_ids);
    blockerDetails(detail,issues,d,view);
    const related=cycles.filter(c=>c.work_package_id===w.id);
    if(related[0]?.analysis_id)refs.append(analysisLink(related[0]));
    if(related[0]?.evaluation_id)refs.append(analysisLink(related[0],true));
    if(related.length>1){const previous=roadmapDisclosure(view,refs,d,`execution-analyses:${w.id}`,`Other analyses · ${related.length-1}`);for(const c of related.slice(1)){const item=records(c);item.append(element('code','agent-queue-id',c.run_id));previous.append(item);}}
    return [task,state,result,refs];
  },w=>['pending','blocked'].includes(w.state));
  const unassigned=blockers.filter(b=>!works.some(w=>w.id===b.scope_id || w.goal_id===b.scope_id) && !cycles.some(c=>c.id===b.scope_id));
  if(unassigned.length || scheduler.next_steps?.length){const detail=roadmapDisclosure(view,workPanel,d,'execution-scheduling-details','Scheduling details');
    if(scheduler.reason)detail.append(element('p','',scheduler.reason));view.showRows(detail,scheduler.next_steps || []);blockerDetails(detail,unassigned,d,view);}

  const learning=panel(root,'Analysis queue',true);learning.classList.add('agent-queue-panel');
  learning.firstChild.append(button('Check analysis queue',()=>view.act(()=>view.api.post(d.id,'learning/schedule',{command_id:globalThis.crypto.randomUUID()}))));
  learning.append(element('p','muted small',`Deep Eval · serial analysis${d.learning_schedule_requested?' · queue check requested':policy.enabled?' · next check '+time(d.next_learning_at):' · manual scheduling'}`));
  const currentLearning=new Set(cycles.filter(c=>c.state==='running').map(c=>c.id));
  if(!currentLearning.size){const head=[...cycles].reverse().find(c=>c.state==='deferred');if(head)currentLearning.add(head.id);}
  queueSections(learning,d,view,'analysis-queue',cycles,currentLearning,['Task / run','Analysis progress','Result / details','Linked records'],c=>{
    const task=cell(),state=cell(),result=cell(),refs=records(c);
    task.append(compact(works.find(w=>w.id===c.work_package_id)?.objective || c.work_package_id || 'Historical task'),element('code','agent-queue-id',c.run_id || c.id),element('span','muted small',time(c.enqueued_at || c.created_at)));
    state.append(badge(label(c.state),c.state),element('span','muted small',phases[c.phase] || c.phase || 'Waiting for analysis'));
    result.append(element('span','',`${c.memory_result?.accepted || 0} memories accepted`));
    if(c.blocked_reason)result.append(compact(c.blocked_reason,'muted'));
    const detail=roadmapDisclosure(view,result,d,`analysis:${c.id}`,'Details');
    detail.append(element('p','',`This round ${(c.attempts || 0)-(c.round_start_attempts || 0)}/${lp.max_attempts ?? 6} · ${c.attempts || 0} total · budget waits ${c.budget_waits || 0}/${lp.max_attempts ?? 6} · expansion resumes ${c.budget_resumes?.length || 0}`));
    if(c.blocked_reason)detail.append(element('p','',c.blocked_reason));
    if(c.retry_after)detail.append(element('p','',`Next retry: ${time(c.retry_after)}`));
    for(const id of [c.analysis_id,c.evaluation_id])if(id)detail.append(element('code','agent-roadmap-id',id));
    if(c.evaluation_id && d.view==='work'){
      const output=element('div');detail.append(output);let loading=false,loaded=false;
      const load=async()=>{
        if(!detail.open || loading || loaded)return;loading=true;output.textContent='Loading analysis diagnostics…';
        try{
          const cache=view.diagnosticsCache ||= new Map(),key=JSON.stringify([d.id,c.id,c.evaluation_id,c.state,c.attempts,c.phase]);
          const saved=cache.get(key);
          const data=saved && Date.now()-saved.at<10000?saved.data:
            await view.api.get(d.id,`learning/${encodeURIComponent(c.id)}/diagnostics`,view.abort?.signal);
          cache.set(key,{data,at:Date.now()});
          if(!detail.isConnected)return;
          output.replaceChildren();
          if(data.state)output.append(element('p','',`Deep Eval: ${data.state} · Calls ${data.usage?.calls || 0}/${data.settings?.max_calls || '—'}`));
          view.showRows(output,(data.error_details || []).map(e=>typeof e==='string'?e:JSON.stringify(e)));
          view.showRows(output,(data.units || []).map(u=>`${u.id} / ${u.disposition || 'pending'}: ${JSON.stringify(u.issues || [])}`));
          if(data.truncated)output.append(element('p','muted','Too many diagnostic entries; open Deep Eval for the full record.'));
          loaded=true;
        }catch(error){if(error.name!=='AbortError' && detail.isConnected){output.replaceChildren(element('p','error',error.message),button('Retry',load));}}
        finally{loading=false;}
      };
      detail.addEventListener('toggle',load);if(detail.open)load();
    }
    if(['incomplete','failed','cancelled','deferred'].includes(c.state))refs.append(button('Requeue analysis',()=>view.act(()=>view.api.post(d.id,`learning/${encodeURIComponent(c.id)}/commands`,{command_id:globalThis.crypto.randomUUID(),type:'retry'}))));
    return [task,state,result,refs];
  },c=>['pending','deferred'].includes(c.state));
  const policyDetails=roadmapDisclosure(view,learning,d,'analysis-policy','Analysis policy');
  policyDetails.append(element('p','muted',`Scheduled serially in execution order; the table shows newest first. Manual retries go to the back of the queue. Each round allows at most ${lp.max_attempts ?? 6} analysis attempts and ${lp.max_attempts ?? 6} budget waits; Calls cap ${lp.max_evaluation_calls ?? 500}, learning token cap ${number(lp.max_learning_tokens)}. At the cap the cycle keeps its incomplete state and existing memory, then moves to the next item.`));
  if(execution.history?.length){const history=roadmapDisclosure(view,learning,d,'execution-audit','Scheduling & analysis log');table(history,['Time','Event','Outcome'],[...execution.history].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(e=>[time(e.at),e.type,JSON.stringify(e.payload)]));}
}
