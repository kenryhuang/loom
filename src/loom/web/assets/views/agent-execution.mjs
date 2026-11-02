import {element} from '../markdown.mjs';
import {panel,badge,number,table} from './dashboard.mjs';
import {roadmapDisclosure} from './agent-roadmap.mjs';

const states={pending:'等待调度',dispatched:'等待执行槽位',running:'运行中',succeeded:'已验收',incomplete:'未完成',failed:'失败',blocked:'需要处理',cancelled:'已取消',
  completed:'已完成',deferred:'等待恢复',paused:'已暂停',pausing:'暂停中',resuming:'恢复中',finished:'已结束'};
const phases={snapshot:'准备快照',factual_analysis:'事实分析',deep_eval:'Deep Eval',feedback:'分析完成'};
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
  if(!refs.childNodes.length)refs.append(element('span','muted','待创建 Session'));
  return refs;
}
function queueTable(root,headers,items,render,kind) {
  const node=table(root,headers,items.map(render));node.classList.add('agent-queue-table');node.dataset.queueTable=kind;
  [...node.tBodies[0].rows].forEach((row,i)=>{if(items[i])row.dataset.queueId=items[i].id;});
}
function queueSections(root,d,view,key,items,current,headers,render,isPending) {
  const active=items.filter(i=>current.has(i.id)),waiting=items.filter(i=>!current.has(i.id) && isPending(i)),history=items.filter(i=>!current.has(i.id) && !isPending(i));
  if(active.length)queueTable(root,headers,active,render,'current');
  else root.append(element('p','muted agent-queue-empty','当前没有正在处理的任务。'));
  for(const [kind,title,rows] of [['waiting','等待队列',waiting],['history','历史记录',history]]){
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
    if(item.action==='refresh_sources')root.append(button('刷新已绑定来源',()=>view.act(()=>view.api.post(d.id,'sources/refresh',{}))));
  }
}

export function renderExecution(root,d,view) {
  const execution=d.execution || {},policy=execution.policy || d.role.scheduling_policy || {},scheduler=execution.scheduler || {},lp=execution.learning_policy || d.role.learning_policy || {};
  const works=[...d.work_packages].sort(newest),cycles=[...d.learning].sort(cycleNewest),blockers=execution.blockers || [];
  const workPanel=panel(root,'执行队列',true);workPanel.classList.add('agent-queue-panel');
  workPanel.firstChild.append(button('调度设置',()=>view.createForm(d)));
  workPanel.append(element('p','muted small',`${policy.enabled?`定时检查队列 · 每 ${policy.tick_seconds} 秒`:'手动调度'} · 每次执行上限 1 小时${policy.enabled?' · 下次检查 '+time(execution.next_tick_at):''}`));
  const currentWork=new Set(works.filter(w=>['running','dispatched'].includes(w.state)).map(w=>w.id));
  if(!currentWork.size){const head=works.find(w=>w.id===scheduler.work_package_id && !['succeeded','cancelled'].includes(w.state)) || works.find(w=>w.state==='blocked');if(head)currentWork.add(head.id);}
  if(scheduler.manual_required)workPanel.append(element('p','agent-schedule-warning',`需要人工介入：${(scheduler.reason || '查看当前任务详情').split('；')[0]}`));
  else if(['checking','planning'].includes(scheduler.state))workPanel.append(element('p','muted',scheduler.state==='planning'?'正在规划下一轮任务。':'正在检查执行队列。'));
  queueSections(workPanel,d,view,'execution-queue',works,currentWork,['任务 / 时间','状态 / 进度','结果 / 详情','关联记录'],w=>{
    const task=cell(),state=cell(),result=cell(),refs=records(w),live=(execution.runtime || []).find(r=>r.work_package_id===w.id),context=w.latest_context;
    task.append(compact(w.objective),element('span','muted small',time(stamp(w))));
    state.append(badge(label(w.state),w.state));
    if(live)state.append(element('span','muted small',`${live.steps || 0} 步 · ${Math.floor(live.active_seconds || 0)} 秒`));
    const issues=blockers.filter(b=>b.scope_id===w.id || b.scope_id===w.goal_id);
    const reason=w.state==='succeeded'?'验收通过':(w.recovery?.exhausted || w.blocker?.code==='RECOVERY_EXHAUSTED'?'已挂起，等待人工恢复':w.blocked_reason) || issues[0]?.reason;
    if(reason && !['running','dispatched'].includes(w.state))result.append(compact(reason,w.state==='succeeded'?'muted':''));
    const detail=roadmapDisclosure(view,result,d,`execution:${w.id}`,'详情');
    detail.append(element('p','',w.objective),element('code','agent-roadmap-id',w.id));
    if(w.reason)detail.append(element('p','',`编排依据：${w.reason}`));
    if(reason && w.state!=='succeeded')detail.append(element('p','',`当前阻塞：${reason}`));
    if(w.binding)detail.append(element('p','',`绑定：${w.binding.source==='recommended'?'推荐':'兜底'}${w.binding.rationale?` · ${w.binding.rationale}`:w.binding.reason?` · ${w.binding.reason}`:''}`));
    if(w.execution_budget)detail.append(element('p','',`Token 预算 ${number(w.execution_budget.tokens)} · 自动恢复 ${w.recovery?.attempts || 0}/${policy.max_recovery_attempts ?? 3} 次`));
    const objectiveId=w.objective_id || d.goals.find(g=>g.id===w.goal_id)?.long_term_objective_id,objectiveBudget=d.objective_budgets?.[objectiveId];
    const effectiveBudget=d.objective_budget?.objectives?.find(b=>b.objective_id===objectiveId);
    if(objectiveBudget)detail.append(element('p','',`长期目标额度 ${number(effectiveBudget?.limit ?? objectiveBudget.tokens)} · 自动扩容上限 ${number(Math.max(effectiveBudget?.initial_limit || 0,policy.max_objective_tokens ?? objectiveBudget.ceiling))} · 已扩容 ${objectiveBudget.grants?.length || 0} 次`));
    if(live)detail.append(element('p','',`Session：${live.task_state} · run：${live.run_state || '—'} · ${live.checkpoint_available?'已有 checkpoint / 恢复上下文':'等待恢复点'}`));
    if(w.slice?.phase)detail.append(element('p','muted',`历史时间片 #${w.slice.number} · ${label(w.slice.phase)}`));
    if(context){detail.append(element('p','',`${context.memory_refs?.length || 0} 条相关记忆 · ${context.guidance_ids?.length || 0} 条指导 · ${context.completed_learning_ids?.length || 0} 个已完成分析`));view.showRows(detail,(context.memory_refs || []).map(r=>JSON.stringify(r)));}
    if(w.run_ids?.length)view.showRows(detail,w.run_ids);
    blockerDetails(detail,issues,d,view);
    const related=cycles.filter(c=>c.work_package_id===w.id);
    if(related[0]?.analysis_id)refs.append(analysisLink(related[0]));
    if(related[0]?.evaluation_id)refs.append(analysisLink(related[0],true));
    if(related.length>1){const previous=roadmapDisclosure(view,refs,d,`execution-analyses:${w.id}`,`其他分析 · ${related.length-1}`);for(const c of related.slice(1)){const item=records(c);item.append(element('code','agent-queue-id',c.run_id));previous.append(item);}}
    return [task,state,result,refs];
  },w=>['pending','blocked'].includes(w.state));
  const unassigned=blockers.filter(b=>!works.some(w=>w.id===b.scope_id || w.goal_id===b.scope_id) && !cycles.some(c=>c.id===b.scope_id));
  if(unassigned.length || scheduler.next_steps?.length){const detail=roadmapDisclosure(view,workPanel,d,'execution-scheduling-details','调度详情');
    if(scheduler.reason)detail.append(element('p','',scheduler.reason));view.showRows(detail,scheduler.next_steps || []);blockerDetails(detail,unassigned,d,view);}

  const learning=panel(root,'分析队列',true);learning.classList.add('agent-queue-panel');
  learning.firstChild.append(button('检查分析队列',()=>view.act(()=>view.api.post(d.id,'learning/schedule',{command_id:globalThis.crypto.randomUUID()}))));
  learning.append(element('p','muted small',`Deep Eval · 串行分析${d.learning_schedule_requested?' · 已请求检查队列':policy.enabled?' · 下次检查 '+time(d.next_learning_at):' · 手动调度'}`));
  const currentLearning=new Set(cycles.filter(c=>c.state==='running').map(c=>c.id));
  if(!currentLearning.size){const head=[...cycles].reverse().find(c=>c.state==='deferred');if(head)currentLearning.add(head.id);}
  queueSections(learning,d,view,'analysis-queue',cycles,currentLearning,['任务 / run','分析进度','结果 / 详情','关联记录'],c=>{
    const task=cell(),state=cell(),result=cell(),refs=records(c);
    task.append(compact(works.find(w=>w.id===c.work_package_id)?.objective || c.work_package_id || '历史任务'),element('code','agent-queue-id',c.run_id || c.id),element('span','muted small',time(c.enqueued_at || c.created_at)));
    state.append(badge(label(c.state),c.state),element('span','muted small',phases[c.phase] || c.phase || '等待分析'));
    result.append(element('span','',`${c.memory_result?.accepted || 0} 条已接受记忆`));
    if(c.blocked_reason)result.append(compact(c.blocked_reason,'muted'));
    const detail=roadmapDisclosure(view,result,d,`analysis:${c.id}`,'详情');
    detail.append(element('p','',`本轮 ${(c.attempts || 0)-(c.round_start_attempts || 0)}/${lp.max_attempts ?? 6} · 累计 ${c.attempts || 0} 次 · 预算等待 ${c.budget_waits || 0}/${lp.max_attempts ?? 6} · 扩容续跑 ${c.budget_resumes?.length || 0}`));
    if(c.blocked_reason)detail.append(element('p','',c.blocked_reason));
    if(c.retry_after)detail.append(element('p','',`下次重试：${time(c.retry_after)}`));
    for(const id of [c.analysis_id,c.evaluation_id])if(id)detail.append(element('code','agent-roadmap-id',id));
    if(['incomplete','failed','cancelled','deferred'].includes(c.state))refs.append(button('重新排队分析',()=>view.act(()=>view.api.post(d.id,`learning/${encodeURIComponent(c.id)}/commands`,{command_id:globalThis.crypto.randomUUID(),type:'retry'}))));
    return [task,state,result,refs];
  },c=>['pending','deferred'].includes(c.state));
  const policyDetails=roadmapDisclosure(view,learning,d,'analysis-policy','分析策略');
  policyDetails.append(element('p','muted',`按执行顺序串行调度；表格按时间倒序展示。手动重试进入队尾。每轮分析尝试与预算等待各最多 ${lp.max_attempts ?? 6} 次，Calls 上限 ${lp.max_evaluation_calls ?? 500}，学习 Token 上限 ${number(lp.max_learning_tokens)}。达到上限保留 incomplete 和已有 memory，继续下一项。`));
  if(execution.history?.length){const history=roadmapDisclosure(view,learning,d,'execution-audit','调度与分析日志');table(history,['时间','事件','具体结果'],[...execution.history].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(e=>[time(e.at),e.type,JSON.stringify(e.payload)]));}
}
