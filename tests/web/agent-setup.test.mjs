import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {AgentSetupView} from '../../src/loom/web/assets/views/agent-setup.mjs';

function fixture({edit = null, api, save = async () => {}} = {}) {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const catalog = {models:[{id:'fast',label:'Fast model'},{id:'judge',label:'Judge model'}],
    external_tools:[{id:'lookup',connector_name:'Research',name:'Lookup',kind:'mcp',description:'Find references',effect_kind:'read_only'}]};
  const bases = {knowledge_bases:[{id:'kb-project',name:'Project documents',description:'Design notes'}]};
  const calls = [];
  const view = new AgentSetupView(document.querySelector('main'), {
    edit, save, cancel:() => {}, api:api || {json:async path => {calls.push(path);return path.endsWith('/catalog') ? catalog : bases;}},
  });
  const change = node => node.dispatchEvent(new dom.window.Event('change',{bubbles:true}));
  return {dom,view,calls,change,close:() => {view.close();dom.window.close();}};
}

test('Agent setup shares Session blocks and loads named model/resource choices',async () => {
  const f = fixture();
  try {
    assert.equal(f.view.submit.disabled,true);
    await f.view.load();
    assert.deepEqual(f.calls,['/v1/web/catalog','/v1/knowledge-bases']);
    assert.equal(f.view.form.querySelectorAll('.setup-block').length,10);
    assert.ok(f.view.form.matches('.setup-form'));
    for(const name of ['model','evaluation_model','mode','interval','schedule','daily','timezone'])
      assert.equal(f.view.form.elements[name].tagName,'SELECT');
    assert.match(f.view.model.textContent,/Fast model/);
    assert.match(f.view.toolList.textContent,/Research \/ Lookup/);
    assert.match(f.view.knowledgeList.textContent,/Project documents/);
    assert.equal(f.view.form.querySelector('textarea[name="tools"], textarea[name="knowledge"]'),null);
    assert.equal(f.view.submit.disabled,false);
    f.view.form.elements.name.value='My Agent'; f.view.form.elements.objective.value='Maintain project';
    f.view.form.elements.workspace.value='/project'; f.view.model.value='fast'; f.view.evaluationModel.value='judge';
    f.view.toolList.querySelector('input').checked=true; f.change(f.view.toolList.querySelector('input'));
    f.view.knowledgeList.querySelector('input').checked=true;
    f.view.interval.choice.value='3600'; f.change(f.view.interval.choice);
    f.view.daily.choice.value='250000'; f.change(f.view.daily.choice);
    const role=f.view.payload();
    assert.equal(role.model,'fast'); assert.equal(role.evaluation_model,'judge');
    assert.equal(role.interval_seconds,3600); assert.equal(role.budget.daily_tokens,250000);
    assert.deepEqual(role.external_tool_ids,['lookup']); assert.deepEqual(role.knowledge_base_ids,['kb-project']);
    assert.match(f.view.tools.count.textContent,/1/);
    assert.equal(role.long_term_objectives[0].schedule,undefined);
    assert.equal(role.long_term_objectives[0].stage_planning,undefined);
    assert.equal(role.scheduling_policy.enabled,true);
    assert.equal(role.scheduling_policy.tick_seconds,60);
    assert.equal(role.scheduling_policy.max_recovery_attempts,3);
    assert.equal(role.scheduling_policy.auto_expand_objective_budget,undefined);
    assert.equal(f.view.form.elements.objective_ceiling,undefined);
    assert.equal(f.view.form.elements.stage_budget,undefined);
    assert.equal(f.view.form.elements.slice_default,undefined);
    f.view.queueTick.choice.value='custom';f.change(f.view.queueTick.choice);
    f.view.form.elements.queue_tick_custom.value='180';
    assert.equal(f.view.payload().scheduling_policy.tick_seconds,180);
    f.view.stageMode.value='autonomous';
    f.view.form.elements.stage_constraints.value='先设计，再修改';
    f.view.form.elements.stage_completion.value='可复现验收通过';
    const planning=f.view.payload().long_term_objectives[0].stage_planning;
    assert.equal(planning.mode,'autonomous'); assert.equal(planning.objective_tokens,undefined);
    assert.deepEqual(planning.constraints,['先设计，再修改']);
    assert.deepEqual(planning.completion_criteria,['可复现验收通过']);
  } finally {f.close();}
});

test('Recurring work reveals acceptance, finite goals disable it, custom values remain available',async () => {
  const f=fixture();
  try {
    await f.view.load();
    assert.equal(f.view.criteria.required,false); assert.equal(f.view.criteria.parentElement.hidden,true);
    f.view.schedule.choice.value='86400'; f.change(f.view.schedule.choice);
    assert.equal(f.view.criteria.required,true); assert.equal(f.view.criteria.parentElement.hidden,false);
    f.view.criteria.value='Produce cited report';
    assert.deepEqual(f.view.payload().long_term_objectives[0].schedule,{every_seconds:86400,acceptance:['Produce cited report']});
    f.view.mode.value='finite'; f.change(f.view.mode);
    assert.equal(f.view.schedule.choice.disabled,true); assert.equal(f.view.criteria.required,false);
    assert.equal(f.view.payload().long_term_objectives[0].schedule,undefined);
    f.view.interval.choice.value='custom'; f.change(f.view.interval.choice);
    f.view.form.elements.interval_custom.value='1234';
    assert.equal(f.view.payload().interval_seconds,1234);
  } finally {f.close();}
});

test('Editing preserves advanced role configuration and makes unavailable bindings visible',async () => {
  const role={name:'Existing',role_text:'Evidence first',workspace:'/project',working_directory:'/project/sub',model:'missing',evaluation_model:'judge',
    timezone:'Asia/Shanghai',interval_seconds:1234,budget:{daily_tokens:123456,goal_tokens:900000,work_tokens:23456,max_calls:500},methods:['Read evidence'],
    knowledge_base_ids:['missing-kb'],external_tool_ids:['missing-tool'],task_spec:{schema_version:1},
    long_term_objectives:[{id:'quality',mode:'finite',desired_outcome:'Review',indicators:['Evidence']},{id:'second',mode:'ongoing',desired_outcome:'Another goal'}],
    source_routes:[{id:'docs',topic:'Design',paths:['docs'],freshness:'Check latest',knowledge_base_ids:['missing-kb']},{id:'code',topic:'Implementation',paths:['src']}]};
  const f=fixture({edit:{id:'a',revision:2,role}});
  try {
    await f.view.load();
    assert.match(f.view.model.textContent,/missing \(currently unavailable\)/);
    assert.match(f.view.knowledgeList.textContent,/missing-kb \(currently unavailable\)/);
    assert.equal(f.view.daily.choice.value,'custom');
    const result=f.view.payload();
    assert.equal(result.working_directory,'/project/sub'); assert.equal(result.interval_seconds,1234);
    assert.deepEqual(result.source_routes,role.source_routes); assert.deepEqual(result.long_term_objectives,role.long_term_objectives);
    assert.deepEqual(result.budget,role.budget);
    assert.deepEqual(result.task_spec,{...role.task_spec,session_environment:{plugin:'session',resources:[
      {id:'workspace',kind:'directory',uri:'/project/sub',access:'read-write'}]}});
    assert.deepEqual(role.task_spec,{schema_version:1});
    assert.deepEqual(result.knowledge_base_ids,['missing-kb']); assert.deepEqual(result.external_tool_ids,['missing-tool']);
    f.view.knowledgeList.querySelector('input[value="missing-kb"]').checked=false;
    assert.deepEqual(f.view.payload().source_routes[0].knowledge_base_ids,[]);
    assert.deepEqual(role.source_routes[0].knowledge_base_ids,['missing-kb']);
  } finally {f.close();}
});

test('Loading failure disables saving and retry keeps entered text',async () => {
  let fail=true;
  const f=fixture({api:{json:async path => {if(fail)throw new Error('Offline');return path.endsWith('/catalog')?{models:[]}:{knowledge_bases:[]};}}});
  try {
    f.view.form.elements.objective.value='Keep my draft'; await f.view.load();
    assert.equal(f.view.submit.disabled,true); assert.equal(f.view.retry.hidden,false); assert.match(f.view.error.textContent,/Offline/);
    fail=false; await f.view.load();
    assert.equal(f.view.submit.disabled,false); assert.equal(f.view.form.elements.objective.value,'Keep my draft');
    assert.match(f.view.knowledgeList.textContent,/No knowledge bases yet/);
  } finally {f.close();}
});

test('Closing while loading prevents a stale form update',async () => {
  const pending=[]; const f=fixture({api:{json:()=>new Promise(resolve=>pending.push(resolve))}});
  try {
    const loading=f.view.load(); f.view.close(); f.view.root.textContent='Another page';
    pending[0]({models:[]}); pending[1]({knowledge_bases:[]}); await loading;
    assert.equal(f.view.root.textContent,'Another page'); assert.equal(f.view.submit.disabled,true);
  } finally {f.close();}
});


test('Only the shared daily token allowance is configurable, including custom values',async()=>{
 const f=fixture();
 try{
  await f.view.load();
  assert.deepEqual(f.view.payload().budget,{daily_tokens:1000000});
  for(const name of ['work','goal_budget','learning','max_calls','stage_budget','work_ceiling','objective_ceiling'])
   assert.equal(f.view.form.elements[name],undefined);
  assert.match(f.view.form.textContent,/Usage resets at midnight in the chosen timezone/);
  f.view.daily.choice.value='100000000';f.change(f.view.daily.choice);
  assert.deepEqual(f.view.payload().budget,{daily_tokens:100000000});
  f.view.daily.choice.value='custom';f.change(f.view.daily.choice);f.view.form.elements.daily_custom.value='1234567';
  f.view.form.elements.daily_custom.dispatchEvent(new f.dom.window.Event('input',{bubbles:true}));
  assert.deepEqual(f.view.payload().budget,{daily_tokens:1234567});
 }finally{f.close();}
});

const EXEC_CATALOG={models:[{id:'fast',label:'Fast model'}],setup_recommendation:true,
 tool_collections:[{id:'filesystem',label:'文件',description:'读写工作区文件'},{id:'shell',label:'命令',description:'执行命令'},{id:'task_control',label:'任务完成',description:'结束任务'}],
 plugins:{context:[{id:'bounded_context',label:'Bounded context'}],workflow:[{id:'dynamic',label:'Dynamic'},{id:'legacy_planning',label:'Legacy planning'}]},
 external_tools:[{id:'lookup',connector_name:'Research',name:'Lookup',kind:'mcp',description:'Find references',effect_kind:'read_only'}]};
const EXEC_BASES={knowledge_bases:[{id:'kb-project',name:'Project documents',description:'Design notes'}]};
const execBlock=view=>[...view.form.querySelectorAll('details.setup-block')].find(d=>d.textContent.includes('Execution configuration'));
const execChecked=view=>[...execBlock(view).querySelectorAll('input[type="checkbox"]:checked')].map(n=>n.value);

test('New Agent binds the entered workspace when submitting file and shell tools',async()=>{
 let saved;
 const f=fixture({save:async role=>{saved=role;},api:{json:async path=>path.endsWith('/catalog')?EXEC_CATALOG:EXEC_BASES}});
 try{
  await f.view.load();
  f.view.form.elements.name.value='Project agent';
  f.view.form.elements.role.value='Maintain the project';
  f.view.form.elements.objective.value='Keep tests passing';
  f.view.form.elements.workspace.value='/project';
  for(const id of ['filesystem','shell','task_control'])execBlock(f.view).querySelector(`input[value="${id}"]`).checked=true;
  await f.view.form.onsubmit({preventDefault(){}});
  assert.ok(saved);
  assert.equal(saved.working_directory,'/project');
  assert.deepEqual(saved.task_spec.session_environment,{plugin:'session',resources:[
   {id:'workspace',kind:'directory',uri:'/project',access:'read-write'}]});
  assert.deepEqual(saved.task_spec.tools.collections,['filesystem','shell','task_control']);
  f.view.form.elements.workspace.value='/another-project';
  assert.equal(f.view.payload().task_spec.session_environment.resources[0].uri,'/another-project');
 }finally{f.close();}
});

test('Recommend backfills execution config and manual edits reach the saved role',async()=>{
 const proposal={task_spec:{schema_version:1,tools:{collections:['filesystem','shell','task_control']},context:{plugin:'bounded_context'},workflow:{plugin:'dynamic'}},
  plan_mode:'force',knowledge_base_ids:['kb-project'],external_tool_ids:['lookup'],rationale:'需要读写源码并运行测试',collection_reasons:{filesystem:'读写源码',shell:'运行测试'}};
 const calls=[];
 const api={json:async(path,options)=>{calls.push([path,options?.body]);
  if(path.endsWith('/catalog'))return EXEC_CATALOG;
  if(path.endsWith('/knowledge-bases'))return EXEC_BASES;
  if(path==='/v1/agents/setup/recommend')return proposal;
  throw new Error('unexpected '+path);}};
 const f=fixture({api});
 try{
  await f.view.load();
  const block=execBlock(f.view);
  assert.ok(block);assert.equal(block.open,false);
  assert.match(block.querySelector('.setup-selection-summary').textContent,/Follow system defaults/);
  assert.equal(f.view.payload().task_spec,null);
  assert.equal(f.view.payload().plan_mode,'auto');
  f.view.form.elements.role.value='先查实现，再修改设计';
  f.view.form.elements.objective.value='维护项目文档';
  const recommend=[...block.querySelectorAll('button')].find(b=>b.textContent==='Recommend for me');
  assert.equal(recommend.disabled,false);
  recommend.click();await new Promise(r=>setTimeout(r,0));
  const request=calls.find(c=>c[0]==='/v1/agents/setup/recommend');
  assert.match(request[1].description,/先查实现，再修改设计/);
  assert.match(request[1].description,/维护项目文档/);
  assert.equal(request[1].model,null);
  assert.deepEqual(execChecked(f.view),['filesystem','shell','task_control']);
  assert.equal(f.view.form.elements.plan_mode.value,'force');
  assert.equal(f.view.form.elements.context_plugin.value,'bounded_context');
  assert.equal(f.view.form.elements.workflow_plugin.value,'dynamic');
  assert.equal(f.view.knowledgeList.querySelector('input[value="kb-project"]').checked,true);
  assert.equal(f.view.toolList.querySelector('input[value="lookup"]').checked,true);
  assert.match(block.textContent,/需要读写源码并运行测试/);
  assert.match(block.textContent,/读写源码/);
  assert.match(block.querySelector('.setup-selection-summary').textContent,/3 selected/);
  const role=f.view.payload();
  assert.deepEqual(role.task_spec,{schema_version:1,tools:{collections:['filesystem','shell','task_control']},context:{plugin:'bounded_context'},workflow:{plugin:'dynamic'}});
  assert.equal(role.plan_mode,'force');
  assert.deepEqual(role.knowledge_base_ids,['kb-project']);
  assert.deepEqual(role.external_tool_ids,['lookup']);
  execBlock(f.view).querySelector('input[value="shell"]').checked=false;
  f.view.form.elements.plan_mode.value='auto';
  assert.deepEqual(f.view.payload().task_spec.tools.collections,['filesystem','task_control']);
  assert.equal(f.view.payload().plan_mode,'auto');
 }finally{f.close();}
});

test('Recommend failure keeps manual configuration and reports the error',async()=>{
 const api={json:async(path,options)=>{
  if(path.endsWith('/catalog'))return EXEC_CATALOG;
  if(path.endsWith('/knowledge-bases'))return EXEC_BASES;
  throw new Error('模型不可用');}};
 const f=fixture({api});
 try{
  await f.view.load();
  const block=execBlock(f.view);
  execBlock(f.view).querySelector('input[value="filesystem"]').checked=true;
  f.view.form.elements.role.value='先查实现';
  const recommend=[...block.querySelectorAll('button')].find(b=>b.textContent==='Recommend for me');
  recommend.click();await new Promise(r=>setTimeout(r,0));
  assert.match(block.textContent,/模型不可用/);
  assert.equal(recommend.disabled,false);
  assert.deepEqual(execChecked(f.view),['filesystem']);
  assert.deepEqual(f.view.payload().task_spec.tools.collections,['filesystem']);
 }finally{f.close();}
});

test('Editing reads the role execution config and clearing it returns to system defaults',async()=>{
 const role={name:'Existing',role_text:'Evidence first',workspace:'/project',model:'fast',
  task_spec:{schema_version:1,tools:{collections:['filesystem','task_control']},workflow:{plugin:'dynamic'}},plan_mode:'force',
  long_term_objectives:[{id:'primary',mode:'ongoing',desired_outcome:'Review'}],source_routes:[],knowledge_base_ids:[],external_tool_ids:[]};
 const f=fixture({edit:{id:'a',revision:2,role},api:{json:async path=>path.endsWith('/catalog')?EXEC_CATALOG:EXEC_BASES}});
 try{
  await f.view.load();
  const block=execBlock(f.view);
  assert.deepEqual(execChecked(f.view),['filesystem','task_control']);
  assert.equal(f.view.form.elements.workflow_plugin.value,'dynamic');
  assert.equal(f.view.form.elements.context_plugin.value,'');
  assert.equal(f.view.form.elements.plan_mode.value,'force');
  assert.match(block.querySelector('.setup-selection-summary').textContent,/2 selected/);
  assert.equal(f.view.payload().task_spec.workflow.plugin,'dynamic');
  assert.equal(f.view.payload().plan_mode,'force');
  for(const check of execBlock(f.view).querySelectorAll('input[type="checkbox"]'))check.checked=false;
  f.view.form.elements.workflow_plugin.value='';f.view.form.elements.plan_mode.value='auto';
  assert.equal(f.view.payload().task_spec,null);
  assert.equal(f.view.payload().plan_mode,'auto');
 }finally{f.close();}
});

test('Saving preserves unknown task_spec keys while editing the managed ones',async()=>{
 const role={name:'Existing',role_text:'Evidence first',workspace:'/project',model:'fast',
  task_spec:{schema_version:1,session_environment:{plugin:'session',resources:[{id:'workspace',kind:'directory',uri:'/project',access:'read-only'}]},
   tools:{collections:['filesystem','task_control'],budget:{max_output_tokens:4000}},outputs:{directory:'out'},
   acceptance:{required:true},execution_runtime:{kind:'sandbox'},workflow:{plugin:'dynamic'}},
  plan_mode:'force',long_term_objectives:[{id:'primary',mode:'ongoing',desired_outcome:'Review'}],source_routes:[],knowledge_base_ids:[],external_tool_ids:[]};
 const f=fixture({edit:{id:'a',revision:2,role},api:{json:async path=>path.endsWith('/catalog')?EXEC_CATALOG:EXEC_BASES}});
 try{
  await f.view.load();
  execBlock(f.view).querySelector('input[value="shell"]').checked=true;
  f.view.form.elements.workflow_plugin.value='';f.view.form.elements.context_plugin.value='bounded_context';
  const spec=f.view.payload().task_spec;
  assert.deepEqual(spec.tools,{collections:['filesystem','shell','task_control'],budget:{max_output_tokens:4000}});
  assert.deepEqual(spec.session_environment,role.task_spec.session_environment);
  assert.deepEqual(spec.outputs,{directory:'out'});
  assert.deepEqual(spec.acceptance,{required:true});
  assert.deepEqual(spec.execution_runtime,{kind:'sandbox'});
  assert.deepEqual(spec.context,{plugin:'bounded_context'});
  assert.equal(spec.workflow,undefined);
  assert.equal(spec.schema_version,1);
  assert.deepEqual(role.task_spec.workflow,{plugin:'dynamic'});
 }finally{f.close();}
});

test('Learning shares the daily quota and retains only execution recovery controls',async()=>{
 const f=fixture();
 try{
  await f.view.load();
  assert.equal(f.view.payload().budget.learning_tokens,undefined);
  assert.deepEqual(f.view.payload().learning_policy,{max_resumes:3,max_attempts:6,max_evaluation_calls:500});
  f.view.daily.choice.value='250000';f.change(f.view.daily.choice);
  assert.equal(f.view.payload().learning_policy.max_learning_tokens,undefined);
  f.view.learningAttempts.choice.value='10';f.change(f.view.learningAttempts.choice);
  const saved=f.view.payload();
  const edit=fixture({edit:{role:saved}});
  try{await edit.view.load();assert.deepEqual(edit.view.payload().learning_policy,saved.learning_policy);}
  finally{edit.close();}
 }finally{f.close();}
});
