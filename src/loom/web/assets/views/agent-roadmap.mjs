import {element} from '../markdown.mjs';
import {panel, badge, number, disclosure} from './dashboard.mjs';

const terminal = new Set(['completed','cancelled','superseded']);
const states = {active:'推进中',ready:'待规划',planning:'规划中',reviewing:'验收复盘中',completed:'已交付',blocked:'需要处理',
  waiting:'等待复查',awaiting_input:'等待回答',deferred:'已延后',paused:'已暂停',cancelled:'已取消',superseded:'已调整',queued:'待执行'};
const name = state => states[state] || state || '待规划';
const button = (title,action,cls='quiet') => {const b=element('button',cls,title);b.type='button';b.addEventListener('click',action);return b;};
const lines = value => String(value || '').split('\n').map(x=>x.trim()).filter(Boolean);
function field(form,key,title,value='') {
  const label=element('label','agent-field',title),input=element('textarea');input.name=key;input.rows=3;input.value=value;
  label.append(input);form.append(label);return input;
}
const time = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(undefined,{hour12:false}) : '';

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
  const current=stage.goal_ids || [],old=[];
  for(const id of goalIds(stage,d)) {
    const goal=d.goals.find(g=>g.id===id);
    if(!goal) {root.append(element('p','muted',`目标详情暂不可用：${id}`));continue;}
    if(!current.includes(id)) {old.push(goal);continue;}
    view.renderGoal(root,d,goal);
  }
  if(!goalIds(stage,d).length) root.append(element('p','muted','尚未生成阶段目标。'));
  if(old.length) {
    const history=roadmapDisclosure(view,root,d,`attempts:${stage.id}`,`历史 attempt · ${old.length}`);
    for(const goal of old) view.renderGoal(history,d,goal);
  }
}
function stageDetail(root,stage,d,view,isCurrent) {
  root.dataset.stageId=stage.id;
  const heading=element('div','agent-roadmap-detail-heading');
  heading.append(element('h4','',`${isCurrent?(terminal.has(stage.state)?'最近阶段':'当前阶段'):'历史阶段'}：${stage.title}`),badge(name(stage.state),stage.state));root.append(heading);
  if(stage.expected_outcome) root.append(element('p','agent-roadmap-outcome',stage.expected_outcome));
  const goalSet=new Set(stage.goal_ids || []),works=d.work_packages.filter(w=>goalSet.has(w.goal_id));
  const done=d.goals.filter(g=>goalSet.has(g.id)&&g.state==='completed').length;
  root.append(element('p','muted',`目标已验收 ${done} / ${goalSet.size} · 工作包已验收 ${works.filter(w=>w.state==='succeeded').length} / ${works.length}`));
  const issues=(d.selection || []).filter(s=>goalSet.has(s.goal_id)&&s.eligible===false && !['DEPENDENCY_WAIT'].includes(s.blocker?.code) && !d.goals.some(g=>g.id===s.goal_id&&terminal.has(g.state)));
  const changed=d.goals.some(g=>goalSet.has(g.id)&&g.source_changed&&!terminal.has(g.state));
  const reasons=[...new Set(issues.map(s=>s.blocker?.code==='SOURCE_UNAVAILABLE'?'所需来源尚未刷新或已失效，工作包暂未派发。':s.reason).filter(Boolean))];
  if(changed) reasons.unshift('来源在规划后发生变化，等待复核阶段。');
  if(reasons.length && !terminal.has(stage.state)) root.append(element('p','notice',`当前卡点：${reasons.join('；')}`));
  const goals=section(root,'阶段目标');stageGoals(goals,stage,d,view);
  const contract=roadmapDisclosure(view,root,d,`contract:${stage.id}`,'交付范围与验收');
  if(stage.expected_contribution) section(contract,'对长期目标的贡献',stage.expected_contribution);
  for(const [title,key] of [['验收条件','acceptance'],['范围','scope'],['暂不包含','non_goals'],['假设','assumptions'],['停止条件','stop_conditions']])
    if(stage[key]?.length) view.showRows(section(contract,title),stage[key]);
  if(stage.estimated_cost) contract.append(element('p','muted',`预计投入：${stage.estimated_cost}`));
  contract.append(element('code','agent-roadmap-id',stage.id));
  const review=(d.stage_reviews || []).find(r=>r.id===stage.review_id);
  if(review) {
    const box=roadmapDisclosure(view,root,d,`review:${stage.id}`,'验收复盘');
    box.append(element('p','',review.decision?.reason || '复盘已记录'));
    const gaps=(review.learning || []).filter(c=>c.state!=='completed'||c.coverage!=='full');
    if(gaps.length) box.append(element('p','notice',`学习有 ${gaps.length} 项缺口；业务验收与学习状态分别记录。`));
  }
  if(isCurrent && !terminal.has(stage.state)) {
    const management=roadmapDisclosure(view,root,d,`manage:${stage.id}`,'调整这个阶段');
    const form=element('form','agent-form');field(form,'reason','调整原因');
    const action=(type,payload)=>view.act(()=>view.api.post(d.id,`stages/${encodeURIComponent(stage.id)}/commands`,
      {command_id:crypto.randomUUID(),expected_revision:stage.revision,type,reason:form.elements.reason.value,...(payload?{payload}:{})}));
    field(form,'outcome','修订后的交付',stage.expected_outcome || '');field(form,'criteria','修订后的验收条件',(stage.acceptance || []).join('\n'));
    form.append(button('保存修订并重新规划',()=>action('revise',{expected_outcome:form.elements.outcome.value,acceptance:lines(form.elements.criteria.value)})));
    const actions=element('div','actions');actions.append(button('跳过阶段',()=>action('skip')),button('取消阶段',()=>action('cancel')));form.append(actions);management.append(form);
  }
}
function controls(root,d,objective,state,view) {
  const send=(type,extra={})=>view.act(()=>view.api.post(d.id,`objectives/${encodeURIComponent(objective.id)}/planning/commands`,
    {command_id:crypto.randomUUID(),expected_revision:state.revision,type,...extra}));
  if(state.state==='awaiting_input' && state.question) {
    const box=section(root,'推进前需要你的回答'),form=element('form','agent-form');box.classList.add('notice');
    form.append(element('p','',state.question.text));view.showRows(form,state.question.options || []);field(form,'answer','你的回答');
    form.append(button('提交阶段问题回答',()=>send('answer',{question_id:state.question.id,question_revision:state.question.revision,answer:form.elements.answer.value})));box.append(form);
  }
  const manage=roadmapDisclosure(view,root,d,`planning:${objective.id}`,'调整路线与推进设置');
  manage.append(button(state.manual_pause?'恢复阶段推进':'暂停阶段推进',()=>send(state.manual_pause?'resume':'pause')));
  const form=element('form','agent-form');field(form,'reason','有哪些新信息或条件变化');
  form.append(button('根据新信息重新规划',()=>send('replan',{reason:form.elements.reason.value})));manage.append(form);
  const usage=d.objective_budget?.objectives?.find(b=>b.objective_id===objective.id);
  if(usage) manage.append(element('p','muted',`累计 Token：${number(usage.actual_tokens)} · 预留或未知：${number(usage.reserved_or_unknown)} · 无累计上限 · 调用：${number(usage.calls)}`));
  if(d.objective_budget?.unattributed_historical_calls) manage.append(element('p','muted',`另有 ${d.objective_budget.unattributed_historical_calls} 次历史调用无法归属到具体长期目标，其用量不能当作零。`));
  if(state.next_review_at) manage.append(element('p','muted',`下次复查：${time(state.next_review_at)}`));
}

export function renderRoadmaps(root,d,view) {
  for(const [index,objective] of d.role.long_term_objectives.entries()) {
    const key=JSON.stringify([d.id,objective.id ?? index]),state=(d.objective_planning || []).find(s=>s.objective_id===objective.id);
    const stages=(d.stages || []).filter(s=>s.objective_id===objective.id);
    const current=stages.find(s=>s.id===state?.stage_id) || stages.at(-1);
    const roadmap=(d.roadmaps || []).find(r=>r.id===state?.roadmap_id);
    const enabled=objective.stage_planning?.mode==='autonomous';
    const block=panel(root,'实现路线图',true);block.classList.add('agent-roadmap');block.dataset.objectiveId=objective.id || '';
    const header=block.querySelector('header');header.append(badge(enabled?name(state?.state):'未开启自动推进',state?.state));
    if(roadmap?.revision || roadmap?.created_at) header.append(element('small','muted',`${roadmap.revision?'路线 v'+roadmap.revision:''}${roadmap.created_at?' · '+time(roadmap.created_at):''}`));
    block.append(element('h4','agent-roadmap-vision',objective.desired_outcome));
    const completed=stages.filter(s=>s.state==='completed').length;
    block.append(element('p','muted agent-roadmap-caption',`已交付 ${completed} 个阶段 · ${current && !terminal.has(current.state)?'1 个当前阶段':'暂无执行中阶段'} · ${roadmap?.future?.length || 0} 个暂定方向`));
    block.append(element('p','muted small','路线随实际交付与复盘更新；暂定方向尚未派发，后续可能调整。'));
    if(roadmap?.baseline) block.append(element('p','agent-roadmap-baseline',`当前基线 · ${roadmap.baseline}`));
    if(!enabled) {
      block.append(element('p','muted','自动阶段规划未开启。开启后将核实现状，并在既有范围与预算内生成阶段目标、拆分执行。'));
      block.append(element('p','muted','长期目标无累计 Token 上限，所有任务共用 Agent 的每日总额。'));
      if(objective.id) block.append(button('开启该目标的阶段规划',()=>view.act(()=>view.api.post(d.id,`objectives/${encodeURIComponent(objective.id)}/planning/commands`,
        {command_id:crypto.randomUUID(),expected_revision:d.revision,type:'enable'}))));
    }
    if(state && ['blocked','awaiting_input','deferred','paused','waiting'].includes(state.state))
      block.append(element('p','notice',`${name(state.state)}：${state.reason || '等待下一次规划更新'}`));
    if(!current) block.append(element('p','empty-state',enabled?'已开启，等待核实现状和首轮规划。':'尚未生成阶段路线；已有独立目标保留在下方。'));
    const nodes=[...stages.filter(s=>s.state==='completed' && s.id!==current?.id).map(s=>({id:s.id,title:s.title,state:s.state,tag:'已交付',stage:s})),
      ...(current?[{id:current.id,title:current.title,state:current.state,tag:terminal.has(current.state)?'最近阶段':'当前阶段',stage:current}]:[]),
      ...(roadmap?.future || []).map((f,i)=>({id:`future:${roadmap.id}:${f.key || i}`,title:f.title,tag:'暂定方向',future:f,state:'tentative'}))];
    // Replaced/cancelled stages remain inspectable in history, not on the active route.
    const history=stages.filter(s=>!nodes.some(n=>n.id===s.id));
    const all=[...nodes,...history.map(s=>({id:s.id,title:s.title,tag:'历史阶段',state:s.state,stage:s}))];
    const selected=all.find(n=>n.id===view.roadmapSelections.get(key)) || all.find(n=>n.id===current?.id) || nodes[0];
    const choose=id=>{
      view.roadmapSelections.set(key,id);view.render();
      const scope=[...view.root.querySelectorAll('.agent-roadmap')].find(n=>n.dataset.objectiveId===(objective.id || ''));
      [...scope?.querySelectorAll('[data-roadmap-node]') || []].find(n=>n.dataset.roadmapNode===id)?.focus({preventScroll:true});
    };
    const rail=element('ol','agent-roadmap-rail');rail.setAttribute('aria-label','阶段路线');
    nodes.forEach((node,i)=>{
      const item=element('li','agent-roadmap-step');item.dataset.state=node.state;
      const b=button('',()=>choose(node.id),'agent-roadmap-node');b.dataset.roadmapNode=node.id;b.setAttribute('aria-pressed',String(selected?.id===node.id));
      if(node.id===current?.id)b.setAttribute('aria-current','step');
      b.append(element('span','agent-roadmap-node-tag',`${String(i+1).padStart(2,'0')} / ${node.tag}`),element('strong','',node.title),
        element('span','agent-roadmap-node-outcome',node.stage?.expected_outcome || node.future?.expected_outcome || ''),badge(node.future?'待后续复盘确定':name(node.state),node.state));
      item.append(b);rail.append(item);
    });
    if(nodes.length)block.append(rail);
    queueMicrotask(()=>{
      if(!rail.isConnected || rail.scrollWidth<=rail.clientWidth)return;
      const target=[...rail.querySelectorAll('[data-roadmap-node]')].find(n=>n.dataset.roadmapNode===(selected?.id || current?.id));
      if(target)rail.scrollLeft=Math.max(0,target.parentElement.offsetLeft-rail.firstElementChild.offsetLeft-40);
    });
    if(current && selected?.id!==current.id) block.append(button('查看当前阶段',()=>choose(current.id)));
    const detail=element('section','agent-roadmap-detail');detail.setAttribute('aria-label','选中路线节点');
    if(selected?.stage) stageDetail(detail,selected.stage,d,view,selected.stage.id===current?.id);
    else if(selected?.future) {
      detail.append(element('h4','',`远期暂定：${selected.title}`),element('p','',selected.future.expected_outcome),element('p','muted','这是规划方向，不是已承诺的执行目标。会根据当前阶段的交付、验收与新信息重新判断。'));
      const names=new Map([['current',current?.title || '当前阶段'],...(roadmap?.future || []).map(f=>[f.key,f.title])]);
      if(selected.future.depends_on?.length)detail.append(element('p','muted',`依赖：${selected.future.depends_on.map(id=>names.get(id)||id).join(' → ')}`));
    }
    if(selected)block.append(detail);
    const basis=roadmapDisclosure(view,block,d,`basis:${objective.id}`,'现状与规划依据');
    basis.append(element('p','',roadmap?.baseline || '尚未形成阶段基线；实际交付证据会成为后续规划依据。'));
    if(roadmap?.reason) section(basis,'为什么选择这条路线',roadmap.reason);
    if(roadmap?.created_at)basis.append(element('p','muted',`路线更新于 ${time(roadmap.created_at)}`));
    const revisions=(d.roadmaps || []).filter(r=>r.objective_id===objective.id || stages.some(s=>s.roadmap_id===r.id));
    if(revisions.length>1 || history.length) {
      const changes=roadmapDisclosure(view,block,d,`history:${objective.id}`,`路线调整记录 · ${Math.max(revisions.length-1,0)} 次更新`);
      for(const stage of history) changes.append(button(`${name(stage.state)} · ${stage.title}`,()=>choose(stage.id)));
      for(const r of revisions.slice().reverse()) {
        const record=section(changes,`${r.id===roadmap?.id?'当前路线':'历史路线'}${r.revision?' · v'+r.revision:''}${r.created_at?' · '+time(r.created_at):''}`,r.reason);
        if(r.baseline)record.append(element('p','muted',`当时的现状：${r.baseline}`));
        if(r.future?.length)view.showRows(record,r.future.map(f=>f.title));
      }
    }
    if(state) controls(block,d,objective,state,view);
  }
}
