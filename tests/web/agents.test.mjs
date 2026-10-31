import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {AgentsView} from '../../src/loom/web/assets/views/agents.mjs';
import {AgentsApi} from '../../src/loom/web/assets/agents-api.mjs';

function fixture(){return {id:'a',revision:2,state:'enabled',activity:'waiting',service_enabled:true,goals:[],work_packages:[],learning:[],guidance:[],sessions:[],budget:[],
 role:{name:'<img src=x>',role_text:'Follow evidence',long_term_objectives:[{desired_outcome:'Keep design accurate'}],working_directory:'/project',
 methods:[],source_routes:[],knowledge_base_ids:[],budget:{daily_tokens:1000000}}};}
function setup(api){const dom=new JSDOM('<main><header id="agents-heading" class="workspace-header"></header><section id="agents-page" class="trajectory-page"></section></main><aside></aside>');globalThis.document=dom.window.document;
 const view=new AgentsView(document.querySelector('#agents-page'),document.querySelector('aside'),{api:()=>api});return {dom,view};}

test('Agent API encodes identity and retains supplied command identity',async()=>{const calls=[];const api=new AgentsApi({json:async(...args)=>{calls.push(args);return {};}});
 await api.command('a/b','pause',3,'same-command');assert.equal(calls[0][0],'/v1/agents/a%2Fb/commands');
 assert.equal(calls[0][1].body.command_id,'same-command');assert.equal(calls[0][1].body.expected_revision,3);});

test('Agent details show actual states, tabs and literal text',async()=>{const data=fixture();const {dom,view}=setup({json:async(path)=>path==='/v1/agents'?{agents:[data]}:data});
 try{await view.open();await view.select('a');assert.match(view.root.textContent,/Keep design accurate/);assert.equal(view.root.querySelector('img'),null);
 assert.equal(view.root.querySelectorAll('.agent-tabs button').length,6);view.tab='Learning';data.learning=[{state:'deferred',coverage:'partial',blocked_reason:'Budget exhausted',session_id:'s'}];view.render();
 assert.match(view.root.textContent,/延后 · 部分/);assert.match(view.root.textContent,/Budget exhausted/);assert.equal(view.root.querySelector('a').getAttribute('href'),'#/evaluation/s');
 }finally{view.close();dom.window.close();}});

test('Closing Agent view ignores stale catalog replies',async()=>{let resolve;const {dom,view}=setup({json:()=>new Promise(r=>resolve=r)});
 const pending=view.open();view.close();view.root.textContent='another view';resolve({agents:[]});await pending;
 assert.equal(view.root.textContent,'another view');view.close();dom.window.close();});

test('Create form submits a reviewable role and stays disabled until enabled',async()=>{let sent;const data=fixture();const {dom,view}=setup({json:async(path,options)=>{
 if(options?.body){sent=options.body;return {id:'a'};}return path==='/v1/agents'?{agents:[]}:data;}});
 try{await view.open();await view.createForm();const form=view.root.querySelector('form');form.elements.objective.value='Improve quality';form.elements.workspace.value='/project';
 form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));await new Promise(r=>setTimeout(r,20));
 assert.equal(sent.role.long_term_objectives[0].desired_outcome,'Improve quality');assert.equal(sent.role.workspace,'/project');assert.equal(sent.type,undefined);
 }finally{view.close();dom.window.close();}});

test('Details reuse Evaluation shell, dashboards and keyboard tab navigation',async()=>{
 const data=fixture(); const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  const heading=document.querySelector('#agents-heading');
  assert.equal(heading.querySelector('.eyebrow').textContent,'AGENTS');
  assert.equal(heading.querySelector('h1').textContent,data.role.name);
  assert.equal(document.querySelector('img'),null);
  assert.ok(view.root.querySelector('.evaluation-tabs[role="tablist"]'));
  assert.ok(view.root.querySelector('.trace-dashboard .dashboard-stats'));
  assert.equal(view.root.querySelector('[role="tab"][aria-selected="true"]').textContent,'概览');
  view.root.querySelector('[role="tab"]').dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
  assert.equal(view.tab,'Goals');
  assert.equal(document.activeElement.id,'agent-tab-Goals');
  assert.equal(view.root.querySelector('[role="tabpanel"]').getAttribute('aria-labelledby'),'agent-tab-Goals');
  assert.match(view.root.textContent,/添加目标/);
  for(const tab of ['Work','Profile','Knowledge','Learning']){
   view.tab=tab;view.render();
   assert.ok([...view.root.querySelector('.agent-content').children].every(n=>n.classList.contains('dashboard-panel')));
  }
  await view.createForm(data); assert.equal(heading.hidden,true); assert.ok(view.root.querySelector('.setup-form'));
  [...view.root.querySelectorAll('button')].find(b=>b.textContent==='取消').click();
  await new Promise(r=>setTimeout(r,0));
  assert.equal(heading.hidden,false); assert.ok(view.root.querySelector('.evaluation-tabs'));
 }finally{view.close();dom.window.close();}
});

test('Unchanged polling retains inspected records; server state changes refresh the dashboard',async()=>{
 const data=fixture();const {dom,view}=setup({json:async path=>path.endsWith('/runs')?{runs:[{state:'no_action',reason:'No ready goal',created_at:'2026-10-30'}]}:path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  [...view.root.querySelectorAll('button')].find(b=>b.textContent==='查看最近编排原因').click();
  await new Promise(r=>setTimeout(r,0));
  assert.match(view.root.textContent,/No ready goal/);
  const body=view.root.querySelector('[role="tabpanel"]');await view.refresh();
  assert.equal(view.root.querySelector('[role="tabpanel"]'),body);
  data.state='paused';await view.refresh();
  assert.match(document.querySelector('#agents-heading').textContent,/已暂停/);
 }finally{view.close();dom.window.close();}
});

const tick = () => new Promise(resolve => setTimeout(resolve,0));

test('Overview omits scheduling details while Work retains packages and Session links',async()=>{
 const data=fixture();
 data.goals=[{id:'baseline',objective:'调研项目技术架构',state:'completed',acceptance:['形成架构文档'],created_at:'2026-10-30'}];
 data.work_packages=[
  {id:'w2',goal_id:'baseline',objective:'构建与测试',state:'succeeded',order:1,session_id:'session/b',run_ids:['run-test']},
  {id:'w1',goal_id:'baseline',objective:'代码调研',state:'succeeded',order:0,session_id:'session-a',run_ids:['run-old','run-retry']},
  {id:'w3',goal_id:'baseline',objective:'撰写架构文档',state:'succeeded',order:2,session_id:'session/b',run_ids:['run-doc']},
 ];
 const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  assert.doesNotMatch(view.root.textContent,/当前与最近调度|最近调度目标（未归入阶段）/);
  assert.equal(view.root.querySelector('[data-scheduled-goal-id]'),null);
  view.tab='Work';view.render();
  assert.match(view.root.textContent,/代码调研/);assert.match(view.root.textContent,/构建与测试/);assert.match(view.root.textContent,/撰写架构文档/);
  assert.ok([...view.root.querySelectorAll('a')].some(a=>a.getAttribute('href')==='#/sessions/session%2Fb'));
 }finally{view.close();dom.window.close();}
});

test('Current and previous stage show concrete goals, attempts and execution destinations',async()=>{
 const data=fixture();data.role.long_term_objectives=[{id:'quality',desired_outcome:'Improve Solaris',stage_planning:{mode:'autonomous'}}];
 data.objective_planning=[{id:'p',objective_id:'quality',revision:2,state:'active',stage_id:'new'}];
 data.stages=[
  {id:'old',objective_id:'quality',title:'Rendering',state:'completed',goal_ids:['render'],acceptance:[],estimated_cost:'One package'},
  {id:'new',predecessor_id:'old',objective_id:'quality',title:'Navigation',state:'active',goal_ids:['camera'],acceptance:[],estimated_cost:'Two packages'},
 ];
 data.goals=[
  {id:'render',stage_id:'old',objective:'Verify renderer',state:'completed',acceptance:[]},
  {id:'camera-old',stage_id:'new',objective:'First camera attempt',state:'blocked',acceptance:[],revision:1},
  {id:'camera',stage_id:'new',objective:'Implement camera controls',state:'active',acceptance:[],revision:1},
 ];
 data.work_packages=[
  {id:'render-work',goal_id:'render',objective:'Renderer checks',state:'succeeded',session_id:'renderer',run_ids:['run-render']},
  {id:'camera-work',goal_id:'camera',objective:'Camera controls',state:'running',session_id:'camera',run_ids:['run-camera']},
  {id:'camera-failure',goal_id:'camera-old',objective:'Earlier camera attempt',state:'failed',session_id:'earlier',run_ids:['run-failed']},
 ];
 const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  assert.equal(view.root.querySelector('[data-scheduled-stage-id]'),null);
  view.tab='Goals';view.render();
  const current=view.root.querySelector('[data-stage-id="new"]'),previous=view.root.querySelector('[data-stage-id="old"]');
  assert.match(current.textContent,/当前阶段：Navigation/);assert.match(current.textContent,/Implement camera controls/);
  assert.match(current.textContent,/历史 attempt/);assert.doesNotMatch(current.textContent,/Renderer checks/);
  assert.match(previous.textContent,/上一阶段：Rendering/);assert.match(previous.textContent,/Verify renderer/);
  assert.equal(current.open,true);
  const old=view.root.querySelector('[data-stage-id="old"]');assert.equal(old.open,true);
  old.open=false;await tick();data.activity='working';await view.refresh();
  assert.equal(view.root.querySelector('[data-stage-id="old"]').open,false);
 }finally{view.close();dom.window.close();}
});

test('Stage roadmap shows actual delivery, learning gaps and versioned human answers',async()=>{
 const data=fixture();data.role.long_term_objectives=[{id:'quality',desired_outcome:'Improve Solaris',stage_planning:{mode:'autonomous'}}];
 data.objective_planning=[{id:'p',objective_id:'quality',revision:4,state:'awaiting_input',reason:'Choose the next scope',stage_id:'stage1',roadmap_id:'roadmap1',
  question:{id:'question1',revision:2,text:'Which interaction matters?',options:['Camera','Selection']}}];
 data.roadmaps=[{id:'roadmap1',baseline:'Recorded source inspection',future:[{title:'Galaxy',expected_outcome:'Tentative larger scale'}]}];
 data.stages=[{id:'stage1',objective_id:'quality',title:'Camera navigation',state:'completed',expected_contribution:'Stable controls',acceptance:['Recorded test passed'],
  goal_ids:['goal1'],estimated_cost:'Bounded package',review_id:'review1'}];
 data.stage_reviews=[{id:'review1',decision:{reason:'Original acceptance verified'},learning:[{state:'deferred',coverage:'partial'}]}];
 let sent;
 const {dom,view}=setup({json:async(path,options)=>{if(options?.body){sent={path,body:options.body};return {};}
  return path==='/v1/agents'?{agents:[data]}:data;}});
 try{
  await view.open();await view.select('a');view.tab='Goals';view.render();
  assert.match(view.root.textContent,/Recorded source inspection/);assert.match(view.root.textContent,/远期暂定：Galaxy/);
  assert.match(view.root.textContent,/学习有 1 项缺口/);assert.match(view.root.textContent,/goal1/);
  view.root.querySelector('[name="answer"]').value='Camera';
  [...view.root.querySelectorAll('button')].find(b=>b.textContent==='提交阶段问题回答').click();await tick();
  assert.equal(sent.path,'/v1/agents/a/objectives/quality/planning/commands');
  assert.equal(sent.body.expected_revision,4);assert.equal(sent.body.question_revision,2);assert.equal(sent.body.answer,'Camera');
 }finally{view.close();dom.window.close();}
});

test('Stage planning is explicitly enabled per objective with the Agent revision',async()=>{
 const data=fixture();data.role.long_term_objectives[0].id='quality';let sent;
 const {dom,view}=setup({json:async(path,options)=>{if(options?.body){sent=options.body;return {};}
  return path==='/v1/agents'?{agents:[data]}:data;}});
 try{
  await view.open();await view.select('a');view.tab='Goals';view.render();
  assert.match(view.root.textContent,/自动阶段规划未开启/);assert.doesNotMatch(view.root.textContent,/已开启，等待/);
  [...view.root.querySelectorAll('button')].find(b=>b.textContent==='开启该目标的阶段规划').click();await tick();
  assert.equal(sent.type,'enable');assert.equal(sent.expected_revision,2);
 }finally{view.close();dom.window.close();}
});
const resourceCatalog = {external_tools:[{id:'lookup',connector_name:'Research',name:'Lookup',kind:'mcp',description:'Find references',effect_kind:'read_only'}]};
const resourceBases = {knowledge_bases:[{id:'docs',name:'Project documents',description:'Design notes'}]};

test('Knowledge tab selects resources and saves a role revision without losing advanced configuration',async()=>{
 let data=fixture(),sent;
 Object.assign(data.role,{external_tool_ids:['old-tool'],knowledge_base_ids:['old-base'],model:'judge',task_spec:{custom:'keep'},
  source_routes:[{id:'docs',topic:'Design',paths:['docs'],knowledge_base_ids:['old-base'],external_tool_ids:['old-tool'],freshness:'latest'}]});
 const prior=structuredClone(data.role);
 const {dom,view}=setup({json:async(path,options)=>{
  if(path==='/v1/web/catalog')return resourceCatalog;
  if(path==='/v1/knowledge-bases')return resourceBases;
  if(options?.body){sent=options.body;data={...data,revision:data.revision+1,role:sent.role};return data;}
  return path==='/v1/agents'?{agents:[data]}:structuredClone(data);
 }});
 try{
  await view.open();await view.select('a');view.tab='Knowledge';view.render();
  const form=view.root.querySelector('.agent-resource-form');
  assert.equal(form.querySelector('button[type=submit]').disabled,true);await tick();
  assert.match(form.textContent,/Project documents/);assert.match(form.textContent,/Research \/ Lookup/);
  assert.match(form.textContent,/old-base（当前不可用）/);
  for(const input of form.querySelectorAll('input')){input.checked=['docs','lookup'].includes(input.value);input.dispatchEvent(new dom.window.Event('change',{bubbles:true}));}
  assert.match(form.textContent,/已选择 1 个知识库 · 1 个外部工具/);
  // A background state update must not discard a draft once focus leaves a checkbox.
  data={...data,activity:'planning'};document.activeElement?.blur();await view.refresh();
  assert.equal(view.root.querySelector('.agent-resource-form'),form);
  form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  assert.equal(sent.expected_revision,2);assert.ok(sent.command_id);
  assert.deepEqual(sent.role.knowledge_base_ids,['docs']);assert.deepEqual(sent.role.external_tool_ids,['lookup']);
  assert.equal(sent.role.model,prior.model);assert.deepEqual(sent.role.task_spec,prior.task_spec);
  assert.deepEqual(sent.role.long_term_objectives,prior.long_term_objectives);assert.deepEqual(sent.role.budget,prior.budget);
  assert.deepEqual(sent.role.source_routes,[{...prior.source_routes[0],knowledge_base_ids:[],external_tool_ids:[]}]);
  assert.deepEqual(prior.source_routes[0].knowledge_base_ids,['old-base']);
  assert.equal(view.tab,'Knowledge');
  const saved=view.root.querySelector('.agent-resource-form');assert.notEqual(saved,form);
  assert.deepEqual([...saved.querySelectorAll('input:checked')].map(n=>n.value),['docs','lookup']);
 }finally{view.close();dom.window.close();}
});

test('Knowledge resource loading can retry and a revision conflict preserves selections',async()=>{
 const data=fixture();let unavailable=true,posts=0;
 const {dom,view}=setup({json:async(path,options)=>{
  if(path==='/v1/web/catalog'){if(unavailable)throw new Error('Catalog offline');return resourceCatalog;}
  if(path==='/v1/knowledge-bases')return resourceBases;
  if(options?.body){posts++;throw Object.assign(new Error('Revision conflict'),{status:409});}
  return path==='/v1/agents'?{agents:[data]}:data;
 }});
 try{
  await view.open();await view.select('a');view.tab='Knowledge';view.render();await tick();
  const form=view.root.querySelector('.agent-resource-form'),save=form.querySelector('button[type=submit]');
  assert.match(form.textContent,/Catalog offline/);assert.equal(save.disabled,true);
  unavailable=false;[...form.querySelectorAll('button')].find(b=>b.textContent==='重新加载选项').click();await tick();
  const check=form.querySelector('input[value="docs"]');check.click();assert.equal(save.disabled,false);
  save.click();await tick();
  assert.equal(posts,1);assert.equal(check.checked,true);assert.equal(check.disabled,false);
  assert.match(form.querySelector('[role=alert]').textContent,/配置已更新/);
  await view.refresh();assert.equal(view.root.querySelector('.agent-resource-form'),form);
 }finally{view.close();dom.window.close();}
});

test('Leaving Knowledge ignores catalog responses for the detached resource editor',async()=>{
 const data=fixture(),pending=[];
 const {dom,view}=setup({json:async path=>{
  if(['/v1/web/catalog','/v1/knowledge-bases'].includes(path))return new Promise(resolve=>pending.push(resolve));
  return path==='/v1/agents'?{agents:[data]}:data;
 }});
 try{
  await view.open();await view.select('a');view.tab='Knowledge';view.render();
  const form=view.root.querySelector('.agent-resource-form');view.tab='Overview';view.render();
  pending[0](resourceCatalog);pending[1](resourceBases);await tick();
  assert.equal(form.querySelector('input'),null);
  assert.equal(view.root.querySelector('.agent-resource-form'),null);
 }finally{view.close();dom.window.close();}
});

test('Overview shows local next scheduling time and updates its countdown without rebuilding the page',async t=>{
 const data=fixture();let now=Date.parse('2026-10-30T08:00:00Z');data.next_wake_at='2026-10-30T08:02:00+00:00';
 t.mock.method(Date,'now',()=>now);
 const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  const body=view.root.querySelector('[role=tabpanel]'),schedule=body.querySelector('[data-agent-schedule]');
  assert.match(schedule.textContent,/下次调度/);
  assert.equal(schedule.querySelector('time').dateTime,'2026-10-30T08:02:00.000Z');
  assert.match(schedule.textContent,/约 2 分钟后/);
  assert.ok(schedule.textContent.includes(Intl.DateTimeFormat().resolvedOptions().timeZone));
  now+=90000;await view.refresh();
  assert.equal(view.root.querySelector('[role=tabpanel]'),body);
  assert.match(schedule.textContent,/30 秒后/);
  now+=31000;await view.refresh();
  assert.match(schedule.textContent,/已到调度时间，等待调度器处理/);
  assert.doesNotMatch(schedule.textContent,/-1 秒|NaN|Invalid/);
 }finally{view.close();dom.window.close();}
});

test('Overview distinguishes paused, disabled, unscheduled and invalid scheduling times',async()=>{
 const data=fixture();data.next_wake_at='2026-10-30T08:02:00Z';
 const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{
  await view.open();await view.select('a');
  for(const [state,title] of [['draft','尚未启用'],['paused','已暂停'],['archived','已归档']]){
   data.state=state;await view.refresh();const schedule=view.root.querySelector('[data-agent-schedule]');
   assert.match(schedule.textContent,new RegExp(title));assert.equal(schedule.querySelector('time'),null);
  }
  data.state='enabled';data.service_enabled=false;await view.refresh();
  assert.match(view.root.querySelector('[data-agent-schedule]').textContent,/自主运行未启用/);
  data.service_enabled=true;data.next_wake_at='';await view.refresh();
  assert.match(view.root.querySelector('[data-agent-schedule]').textContent,/等待调度/);
  data.next_wake_at='invalid';await view.refresh();
  const schedule=view.root.querySelector('[data-agent-schedule]');
  assert.match(schedule.textContent,/调度时间暂不可用/);assert.doesNotMatch(schedule.textContent,/Invalid|NaN/);
 }finally{view.close();dom.window.close();}
});

test('Overview shows effective learning allowance and audited expansion',async()=>{
 const data=fixture();data.learning_budget={initial_tokens:250000,effective_tokens:500000,policy:{auto_expand:true,max_learning_tokens:1000000}};
 data.learning_allocations=[{window:'2026-10-31',grants:[{before:250000,after:500000,reason:'Learning request exceeded allowance by 1509'}]}];
 const {dom,view}=setup({json:async path=>path==='/v1/agents'?{agents:[data]}:data});
 try{await view.open();await view.select('a');assert.match(view.root.textContent,/今日学习额度/);assert.match(view.root.textContent,/500,000/);assert.match(view.root.textContent,/exceeded allowance by 1509/);}
 finally{view.close();dom.window.close();}
});

test('Profile shows shared scope, conflicts, review and versioned adoption controls',async()=>{
 const data=fixture(),calls=[];
 data.shared_roles=[{id:'a',name:'Research'},{id:'b',name:'Writer'}];
 data.profile_entries=[{id:'p',key:'style',text:'<script>concise</script>',scope:'owner',state:'candidate',revision:3,
  independent_episodes:2,counterexamples:[],review_at:'2026-11-30',locked_by_user:false}];
 const {dom,view}=setup({json:async(path,options)=>{if(options?.body)calls.push([path,options.body]);
  return path==='/v1/agents'?{agents:[data]}:data;}});
 try {
  await view.open();await view.select('a');view.tab='Profile';view.render();
  assert.match(view.root.textContent,/Research、Writer/);assert.equal(view.root.querySelector('script'),null);
  [...view.root.querySelectorAll('button')].find(b=>b.textContent==='试用七天').click();await tick();
  assert.equal(calls[0][1].expected_revision,3);assert.equal(calls[0][1].type,'trial');
 } finally {view.close();dom.window.close();}
});
