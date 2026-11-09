import {element} from '../markdown.mjs';
import {panel, badge, number, disclosure} from './dashboard.mjs';

const terminal = new Set(['completed','cancelled','superseded']);
const states = {active:'In progress',ready:'Ready to plan',planning:'Planning',reviewing:'In acceptance review',completed:'Delivered',blocked:'Needs attention',
  waiting:'Waiting for the next check',awaiting_input:'Waiting for an answer',deferred:'Deferred',paused:'Paused',cancelled:'Cancelled',superseded:'Superseded',queued:'Queued'};
const name = state => states[state] || state || 'Ready to plan';
const button = (title,action,cls='quiet') => {const b=element('button',cls,title);b.type='button';b.addEventListener('click',action);return b;};
const lines = value => String(value || '').split('\n').map(x=>x.trim()).filter(Boolean);
function field(form,key,title,value='') {
  const label=element('label','agent-field',title),input=element('textarea');input.name=key;input.rows=3;input.value=value;
  label.append(input);form.append(label);return input;
}
const time = (value,timezone) => value && Number.isFinite(new Date(value).getTime())
  ? new Date(value).toLocaleString(undefined,{hour12:false,...(timezone?{timeZone:timezone,timeZoneName:'short'}:{})}) : '';
const waitingBudget = state => ['waiting','deferred'].includes(state?.state) &&
  (state.blocker?.recovery==='wait_budget' || state.blocker?.code==='PLANNING_BUDGET' && state.blocker.recovery!=='adjust_budget');
function budgetReason(state) {
  const daily=state.blocker?.details?.scopes?.find(s=>s.scope==='daily');
  return daily && [daily.used,daily.limit,daily.remaining,daily.requested].every(Number.isFinite)
    ? `${number(daily.used)} / ${number(daily.limit)} tokens used today; ${number(daily.remaining)} remaining. The next planning request must reserve ${number(daily.requested)} tokens.`
    : state.reason || 'Waiting for the daily budget to become available.';
}

export function editableStage(d,stage) {
  return !!stage && !terminal.has(stage.state) &&
    (d.objective_planning || []).some(s=>s.objective_id===stage.objective_id && s.stage_id===stage.id) &&
    d.role.long_term_objectives.some(o=>o.id===stage.objective_id);
}

export function roadmapDisclosure(view,root,d,key,title,open=false) {
  const node=disclosure(root,title),identity=JSON.stringify([d.id,key]);
  node.open=view.stageExpansionStates.get(identity) ?? open;
  node.addEventListener('toggle',()=>{if(node.isConnected)view.stageExpansionStates.set(identity,node.open);});
  return node;
}
function section(root,title,text) {
  const box=element('section','agent-roadmap-section');box.append(element('h5','',title));
  if(text) box.append(element('p','',text));root.append(box);return box;
}
function goalIds(stage,d) {
  return [...new Set([...(stage.goal_ids || []),...d.goals.filter(g=>g.stage_id===stage.id).map(g=>g.id)])];
}
function stageGoals(root,stage,d,view) {
  const current=stage.goal_ids || [],old=[];let n=0;
  for(const id of goalIds(stage,d)) {
    const goal=d.goals.find(g=>g.id===id);
    if(!goal) {root.append(element('p','muted',`Goal details unavailable: ${id}`));continue;}
    if(!current.includes(id)) {old.push(goal);continue;}
    view.renderGoal(root,d,goal,`G${++n}`);
  }
  if(!goalIds(stage,d).length) root.append(element('p','muted','No stage goals generated yet.'));
  if(old.length) {
    const history=roadmapDisclosure(view,root,d,`attempts:${stage.id}`,`Historical attempts · ${old.length}`);
    for(const goal of old) view.renderGoal(history,d,goal);
  }
}
function stageDetail(root,stage,d,view,isCurrent) {
  root.dataset.stageId=stage.id;
  const heading=element('div','agent-roadmap-detail-heading');
  heading.append(element('h4','',`${isCurrent?(terminal.has(stage.state)?'Latest stage':'Current stage'):'Historical stage'}: ${stage.title}`),badge(name(stage.state),stage.state));root.append(heading);
  if(stage.expected_outcome) root.append(element('p','agent-roadmap-outcome',stage.expected_outcome));
  const goalSet=new Set(stage.goal_ids || []),works=d.work_packages.filter(w=>goalSet.has(w.goal_id));
  const done=d.goals.filter(g=>goalSet.has(g.id)&&g.state==='completed').length,succeeded=works.filter(w=>w.state==='succeeded').length;
  const progress=element('div','agent-roadmap-progress');
  for(const [label,value,total] of [['Goals accepted',done,goalSet.size],['Work packages accepted',succeeded,works.length]]) {
    const row=element('div','agent-roadmap-progress-row'),bar=element('progress');
    bar.max=Math.max(total,1);bar.value=value;bar.setAttribute('aria-label',`${label} ${value} / ${total}`);
    row.append(element('span','agent-roadmap-progress-label',label),bar,element('span','agent-roadmap-progress-count',`${value} / ${total}`));
    progress.append(row);
  }
  root.append(progress);
  const issues=(d.selection || []).filter(s=>goalSet.has(s.goal_id)&&s.eligible===false && !['DEPENDENCY_WAIT'].includes(s.blocker?.code) && !d.goals.some(g=>g.id===s.goal_id&&terminal.has(g.state)));
  const changed=d.goals.some(g=>goalSet.has(g.id)&&g.source_changed&&!terminal.has(g.state));
  const reasons=[...new Set(issues.map(s=>s.blocker?.code==='SOURCE_UNAVAILABLE'?'Required sources are stale or unrefreshed; work packages are not dispatched yet.':s.reason).filter(Boolean))];
  if(changed) reasons.unshift('A source changed after planning; waiting for the review stage.');
  if(reasons.length && !terminal.has(stage.state)) root.append(element('p','notice',`Current blockers: ${reasons.join('; ')}`));
  const goals=section(root,'Stage goals');stageGoals(goals,stage,d,view);
  const contract=roadmapDisclosure(view,root,d,`contract:${stage.id}`,'Delivery scope & acceptance');
  contract.classList.add('agent-roadmap-block');
  if(stage.expected_contribution) section(contract,'Contribution to the long-term objective',stage.expected_contribution);
  for(const [title,key] of [['Acceptance criteria','acceptance'],['Scope','scope'],['Not included for now','non_goals'],['Assumptions','assumptions'],['Stop conditions','stop_conditions']])
    if(stage[key]?.length) view.showRows(section(contract,title),stage[key]);
  if(stage.estimated_cost) contract.append(element('p','muted',`Estimated cost: ${stage.estimated_cost}`));
  contract.append(element('code','agent-roadmap-id',stage.id));
  const review=(d.stage_reviews || []).find(r=>r.id===stage.review_id);
  if(review) {
    const box=roadmapDisclosure(view,root,d,`review:${stage.id}`,'Acceptance review');
    box.classList.add('agent-roadmap-block');
    box.append(element('p','',review.decision?.reason || 'Review recorded'));
    const gaps=(review.learning || []).filter(c=>c.state!=='completed'||c.coverage!=='full');
    if(gaps.length) box.append(element('p','notice',`Learning has ${gaps.length} gap(s); business acceptance and learning state are tracked separately.`));
  }
  if(isCurrent && !terminal.has(stage.state)) {
    const management=roadmapDisclosure(view,root,d,`manage:${stage.id}`,'Adjust stage');
    management.classList.add('agent-roadmap-block');
    const form=element('form','agent-form');field(form,'reason','Reason for the adjustment');
    const action=(type,payload)=>view.act(()=>view.api.post(d.id,`stages/${encodeURIComponent(stage.id)}/commands`,
      {command_id:crypto.randomUUID(),expected_revision:stage.revision,type,reason:form.elements.reason.value,...(payload?{payload}:{})}));
    field(form,'outcome','Revised deliverable',stage.expected_outcome || '');field(form,'criteria','Revised acceptance criteria',(stage.acceptance || []).join('\n'));
    form.append(button('Save revision & replan',()=>action('revise',{expected_outcome:form.elements.outcome.value,acceptance:lines(form.elements.criteria.value)})));
    const actions=element('div','actions');actions.append(button('Skip stage',()=>action('skip')),button('Cancel stage',()=>action('cancel')));form.append(actions);management.append(form);
  }
  if(editableStage(d,stage)) {
    const add=roadmapDisclosure(view,root,d,`add-goal:${stage.id}`,'Add a stage goal manually');
    add.classList.add('agent-roadmap-block');
    const form=element('form','agent-form');form.dataset.stageGoalForm=stage.id;
    field(form,'objective','Deliverable for this round').required=true;
    field(form,'acceptance','Acceptance criteria (one per line)').required=true;
    form.append(element('p','muted','New goals run after the stage\'s existing goals and are checked against the overall stage acceptance criteria.'));
    const submit=element('button','primary','Add to current stage');submit.type='submit';form.append(submit);
    form.addEventListener('submit',event=>{event.preventDefault();view.act(async()=>{
      await view.api.post(d.id,`stages/${encodeURIComponent(stage.id)}/goals`,{command_id:crypto.randomUUID(),expected_revision:stage.revision,
        goal:{objective:form.elements.objective.value,acceptance:lines(form.elements.acceptance.value)}});
      form.reset();document.activeElement?.blur();
    });});add.append(form);
  }
}
function controls(root,d,objective,state,view) {
  const send=(type,extra={})=>view.act(()=>view.api.post(d.id,`objectives/${encodeURIComponent(objective.id)}/planning/commands`,
    {command_id:crypto.randomUUID(),expected_revision:state.revision,type,...extra}));
  if(state.state==='awaiting_input' && state.question) {
    const box=section(root,'Your answer is needed to proceed'),form=element('form','agent-form');box.classList.add('notice');
    form.append(element('p','',state.question.text));view.showRows(form,state.question.options || []);field(form,'answer','Your answer');
    form.append(button('Submit stage answer',()=>send('answer',{question_id:state.question.id,question_revision:state.question.revision,answer:form.elements.answer.value})));box.append(form);
  }
  const manage=roadmapDisclosure(view,root,d,`planning:${objective.id}`,'Adjust route & progress settings');
  manage.append(button(state.manual_pause?'Resume stage progress':'Pause stage progress',()=>send(state.manual_pause?'resume':'pause')));
  const form=element('form','agent-form');field(form,'reason','What new information or changed conditions are there');
  form.append(button('Replan with the new information',()=>send('replan',{reason:form.elements.reason.value})));manage.append(form);
  const usage=d.objective_budget?.objectives?.find(b=>b.objective_id===objective.id);
  if(usage) manage.append(element('p','muted',`Total tokens: ${number(usage.actual_tokens)} · reserved or unknown: ${number(usage.reserved_or_unknown)} · no cumulative cap · calls: ${number(usage.calls)}`));
  if(d.objective_budget?.unattributed_historical_calls) manage.append(element('p','muted',`Another ${d.objective_budget.unattributed_historical_calls} historical calls cannot be attributed to a specific long-term objective; their usage is not zero.`));
  if(state.next_review_at && !waitingBudget(state)) manage.append(element('p','muted',`Next review: ${time(state.next_review_at)}`));
}

export function renderRoadmaps(root,d,view) {
  for(const [index,objective] of d.role.long_term_objectives.entries()) {
    const key=JSON.stringify([d.id,objective.id ?? index]),state=(d.objective_planning || []).find(s=>s.objective_id===objective.id);
    const stages=(d.stages || []).filter(s=>s.objective_id===objective.id);
    const current=stages.find(s=>s.id===state?.stage_id) || stages.at(-1);
    const roadmap=(d.roadmaps || []).find(r=>r.id===state?.roadmap_id);
    const enabled=objective.stage_planning?.mode==='autonomous';
    const budgetWaiting=waitingBudget(state);
    const statusName=budgetWaiting ? 'Waiting for daily budget'
      : d.planning_engine===2 && state?.state==='waiting' && ['MODEL_PROTOCOL','CONTRACT_INVALID'].includes(state.blocker?.code)
      ? 'Planning needs correction' : name(state?.state);
    const block=panel(root,'Delivery roadmap',true);block.classList.add('agent-roadmap');block.dataset.objectiveId=objective.id || '';
    const header=block.querySelector('header');header.append(badge(enabled?statusName:'Auto-progress not enabled',state?.state));
    if(roadmap?.revision || roadmap?.created_at) header.append(element('small','muted',`${roadmap.revision?'route v'+roadmap.revision:''}${roadmap.created_at?' · '+time(roadmap.created_at):''}`));
    block.append(element('h4','agent-roadmap-vision',objective.desired_outcome));
    if(roadmap?.baseline) block.append(element('p','agent-roadmap-baseline',`Current baseline · ${roadmap.baseline}`));
    if(!enabled) {
      block.append(element('p','muted',d.planning_engine===2
        ? 'Autonomous progression is off. Scheduled responsibilities and explicit triggers can still publish complete stages; each plan includes its goals, work packages and verification checks.'
        : 'Automatic stage planning is off. Once enabled, it verifies the current state and generates stage goals within the existing scope and budget, then splits them into execution.'));
      block.append(element('p','muted','Long-term objectives have no cumulative token cap; all tasks share the Agent\'s daily total.'));
      if(objective.id) block.append(button('Enable stage planning for this objective',()=>view.act(()=>view.api.post(d.id,`objectives/${encodeURIComponent(objective.id)}/planning/commands`,
        {command_id:crypto.randomUUID(),expected_revision:d.revision,type:'enable'}))));
    }
    if(state && ['blocked','awaiting_input','deferred','paused','waiting'].includes(state.state))
      block.append(element('p','notice',`${statusName}: ${budgetWaiting?budgetReason(state):state.reason || 'Waiting for the next planning update'}`));
    if(budgetWaiting && state.next_review_at)
      block.append(element('p','muted',`Automatic budget check after ${time(state.next_review_at,d.role.timezone)}.`));
    if(state?.revision_requested) block.append(element('p','notice',budgetWaiting
      ? 'The plan revision is saved and will continue automatically when daily budget is available. New work starts after the revised plan passes validation.'
      : 'A plan revision is pending. The previous contract remains in history; new work starts after the replacement plan passes validation.'));
    if(state?.blocker?.details?.issues?.length) for(const issue of state.blocker.details.issues) {
      const detail=`${issue.path}: ${issue.message}`;
      if(!state.reason?.includes(detail)) block.append(element('p','muted',detail));
    }
    if(!current) block.append(element('p','empty-state',enabled
      ? (d.planning_engine===2?'Preparing the first complete stage. If project facts are missing, a discovery stage will appear here.':'Enabled; waiting to verify the current state and run the first planning round.')
      :'No stage route yet; historical goals are kept below.'));
    const nodes=[...stages.filter(s=>s.state==='completed' && s.id!==current?.id).map(s=>({id:s.id,title:s.title,state:s.state,tag:'Delivered',stage:s})),
      ...(current?[{id:current.id,title:current.title,state:current.state,tag:terminal.has(current.state)?'Latest stage':current.kind==='discovery'?'Discovery stage':'Current stage',stage:current}]:[]),
      ...(roadmap?.future || []).map((f,i)=>({id:`future:${roadmap.id}:${f.key || i}`,title:f.title,tag:'Tentative direction',future:f,state:'tentative'}))];
    // Replaced/cancelled stages remain inspectable in history, not on the active route.
    const history=stages.filter(s=>!nodes.some(n=>n.id===s.id));
    const all=[...nodes,...history.map(s=>({id:s.id,title:s.title,tag:'Historical stage',state:s.state,stage:s}))];
    const selected=all.find(n=>n.id===view.roadmapSelections.get(key)) || all.find(n=>n.id===current?.id) || nodes[0];
    const choose=id=>{
      view.roadmapSelections.set(key,id);view.render();
      const scope=[...view.root.querySelectorAll('.agent-roadmap')].find(n=>n.dataset.objectiveId===(objective.id || ''));
      [...scope?.querySelectorAll('[data-roadmap-node]') || []].find(n=>n.dataset.roadmapNode===id)?.focus({preventScroll:true});
    };
    const rail=element('ol','agent-roadmap-rail');rail.setAttribute('aria-label','Stage route');
    nodes.forEach((node,i)=>{
      const item=element('li','agent-roadmap-step');item.dataset.state=node.state;
      const b=button('',()=>choose(node.id),'agent-roadmap-node');b.dataset.roadmapNode=node.id;b.setAttribute('aria-pressed',String(selected?.id===node.id));
      if(node.id===current?.id)b.setAttribute('aria-current','step');
      b.append(element('span','agent-roadmap-node-tag',`${String(i+1).padStart(2,'0')} / ${node.tag}`),element('strong','',node.title),
        element('span','agent-roadmap-node-outcome',node.stage?.expected_outcome || node.future?.expected_outcome || ''),badge(node.future?'To be confirmed at review':name(node.state),node.state));
      item.append(b);rail.append(item);
    });
    if(nodes.length)block.append(rail);
    queueMicrotask(()=>{
      if(!rail.isConnected || rail.scrollWidth<=rail.clientWidth)return;
      const target=[...rail.querySelectorAll('[data-roadmap-node]')].find(n=>n.dataset.roadmapNode===(selected?.id || current?.id));
      if(target)rail.scrollLeft=Math.max(0,target.parentElement.offsetLeft-rail.firstElementChild.offsetLeft-40);
    });
    if(current && selected?.id!==current.id) block.append(button('View current stage',()=>choose(current.id)));
    const detail=element('section','agent-roadmap-detail');detail.setAttribute('aria-label','Selected route node');
    if(selected?.stage) stageDetail(detail,selected.stage,d,view,selected.stage.id===current?.id);
    else if(selected?.future) {
      detail.append(element('h4','',`Tentative long-term: ${selected.title}`),element('p','',selected.future.expected_outcome),element('p','muted','This is a planning direction, not a committed execution goal. It is reassessed against the current stage\'s deliveries, acceptance, and new information.'));
      const names=new Map([['current',current?.title || 'Current stage'],...(roadmap?.future || []).map(f=>[f.key,f.title])]);
      if(selected.future.depends_on?.length)detail.append(element('p','muted',`Depends on: ${selected.future.depends_on.map(id=>names.get(id)||id).join(' → ')}`));
    }
    if(selected)block.append(detail);
    const basis=roadmapDisclosure(view,block,d,`basis:${objective.id}`,'Current state & planning basis');
    basis.append(element('p','',roadmap?.baseline || 'No stage baseline yet; actual delivery evidence becomes the basis for later planning.'));
    if(roadmap?.reason) section(basis,'Why this route',roadmap.reason);
    if(roadmap?.created_at)basis.append(element('p','muted',`Route updated at ${time(roadmap.created_at)}`));
    const revisions=(d.roadmaps || []).filter(r=>r.objective_id===objective.id || stages.some(s=>s.roadmap_id===r.id));
    if(revisions.length>1 || history.length) {
      const changes=roadmapDisclosure(view,block,d,`history:${objective.id}`,`Route change history · ${Math.max(revisions.length-1,0)} update(s)`);
      for(const stage of history) changes.append(button(`${name(stage.state)} · ${stage.title}`,()=>choose(stage.id)));
      for(const r of revisions.slice().reverse()) {
        const record=section(changes,`${r.id===roadmap?.id?'Current route':'Historical route'}${r.revision?' · v'+r.revision:''}${r.created_at?' · '+time(r.created_at):''}`,r.reason);
        if(r.baseline)record.append(element('p','muted',`State at the time: ${r.baseline}`));
        if(r.future?.length)view.showRows(record,r.future.map(f=>f.title));
      }
    }
    if(state) controls(block,d,objective,state,view);
  }
}
