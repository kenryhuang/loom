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
    assert.equal(f.view.form.querySelectorAll('.setup-block').length,8);
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
    assert.match(f.view.model.textContent,/missing（当前不可用）/);
    assert.match(f.view.knowledgeList.textContent,/missing-kb（当前不可用）/);
    assert.equal(f.view.daily.choice.value,'custom');
    const result=f.view.payload();
    assert.equal(result.working_directory,'/project/sub'); assert.equal(result.interval_seconds,1234);
    assert.deepEqual(result.source_routes,role.source_routes); assert.deepEqual(result.long_term_objectives,role.long_term_objectives);
    assert.deepEqual(result.budget,role.budget); assert.deepEqual(result.task_spec,role.task_spec);
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
    assert.match(f.view.knowledgeList.textContent,/暂无知识库/);
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


test('Work budget defaults to a tenth of daily allowance with selectable overrides and 2000 calls',async()=>{
 const f=fixture();
 try{
  await f.view.load();
  assert.equal(f.view.work.choice.tagName,'SELECT');assert.equal(f.view.work.choice.value,'daily_fraction');
  assert.equal(f.view.payload().budget.work_tokens,100000);assert.equal(f.view.payload().budget.max_calls,2000);
  assert.equal(f.view.payload().budget.goal_tokens,100000000);
  f.view.daily.choice.value='100000000';f.change(f.view.daily.choice);
  assert.equal(f.view.payload().budget.work_tokens,10000000);
  assert.doesNotMatch(f.view.form.querySelector('[data-budget-summary]').textContent,/单目标累计上限限制/);
  f.view.goalBudget.choice.value='1000000';f.change(f.view.goalBudget.choice);
  assert.match(f.view.form.querySelector('[data-budget-summary]').textContent,/单目标累计上限限制/);
  f.view.work.choice.value='500000';f.change(f.view.work.choice);
  f.view.daily.choice.value='5000000';f.change(f.view.daily.choice);
  assert.equal(f.view.payload().budget.work_tokens,500000);
  f.view.work.choice.value='custom';f.change(f.view.work.choice);f.view.form.elements.work_custom.value='2345678';
  f.view.maxCalls.choice.value='custom';f.change(f.view.maxCalls.choice);f.view.form.elements.max_calls_custom.value='3000';
  assert.equal(f.view.payload().budget.work_tokens,2345678);assert.equal(f.view.payload().budget.max_calls,3000);
  f.view.work.choice.value='daily_fraction';f.change(f.view.work.choice);
  f.view.daily.choice.value='custom';f.change(f.view.daily.choice);f.view.form.elements.daily_custom.value='1234567';
  f.view.form.elements.daily_custom.dispatchEvent(new f.dom.window.Event('input',{bubbles:true}));
  assert.equal(f.view.payload().budget.work_tokens,123456);
 }finally{f.close();}
});

test('Learning policy uses selectable bounds and preserves explicit settings on edit',async()=>{
  const f=fixture();
  try {
    await f.view.load();
    assert.equal(f.view.payload().budget.learning_tokens,250000);
    assert.deepEqual(f.view.payload().learning_policy,{auto_expand:true,max_learning_tokens:1000000,max_expansions:2,max_resumes:3});
    f.view.daily.choice.value='250000';f.change(f.view.daily.choice);
    assert.equal(f.view.payload().learning_policy.max_learning_tokens,250000);
    f.view.learningAuto.value='false';
    f.view.learningExpansions.choice.value='0';f.change(f.view.learningExpansions.choice);
    assert.equal(f.view.payload().learning_policy.auto_expand,false);
    assert.equal(f.view.payload().learning_policy.max_expansions,0);
    const saved=f.view.payload();
    const edit=fixture({edit:{role:saved}});
    try {await edit.view.load();assert.deepEqual(edit.view.payload().learning_policy,saved.learning_policy);}
    finally {edit.close();}
  } finally {f.close();}
});
