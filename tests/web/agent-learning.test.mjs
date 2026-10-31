import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {AgentsView} from '../../src/loom/web/assets/views/agents.mjs';

const tick = () => new Promise(resolve=>setTimeout(resolve,0));
function fixture() {
  return {id:'a',revision:2,state:'enabled',activity:'waiting',service_enabled:true,goals:[],work_packages:[],learning:[],guidance:[],sessions:[],budget:[],
    pending_decisions:[],role:{name:'Agent',role_text:'Evidence first',long_term_objectives:[],working_directory:'/project',methods:['Cite evidence'],source_routes:[],budget:{daily_tokens:1000000}}};
}
function memory() {
  return {id:'fact',kind:'facts',title:'Project fact <img src=x>',summary:'Observed project fact',scope:'This project',confidence:'low',
    review_state:'unreviewed',effective_confidence:'low',basis:'inferred',citations:[{ref:'e1',quote:'original evidence'}],
    source:{session_id:'s',evaluation_id:'e',record_id:'fact',content_digest:'digest',annotation_revision:3}};
}
function setup(data, handle = async()=>undefined) {
  const dom = new JSDOM('<main><section></section></main><aside></aside>'); globalThis.document=dom.window.document;
  const calls=[];
  const api={json:async(path,options)=>{
    calls.push([path,options]); const answer=await handle(path,options);
    if(answer !== undefined)return answer;
    if(path.includes('/memory/library'))return {records:[],total:0,next_offset:null};
    return path==='/v1/agents'?{agents:[data]}:structuredClone(data);
  }};
  const view=new AgentsView(document.querySelector('section'),document.querySelector('aside'),{api:()=>api});
  return {dom,view,calls,async open(){await view.open();await view.select('a');view.tab='Learning';view.render();await tick();},
    close(){view.close();dom.window.close();}};
}

test('Learning library auto-loads evidence, filters all records, and persists human review',async()=>{
  const data=fixture();let record=memory(),annotation;
  const env=setup(data,async(path,options)=>{
    if(path.includes('/memory/library'))return {records:[structuredClone(record)],total:1,next_offset:null};
    if(path.endsWith('/memory/annotations') && options?.body){annotation=options.body;record={...record,review_state:'confirmed',effective_confidence:'high'};return {};}
  });
  try{
    await env.open();const root=env.view.root;
    assert.match(root.textContent,/已学知识与审核/);assert.match(root.textContent,/待人工审核/);assert.match(root.textContent,/评估置信度：低/);
    assert.match(root.textContent,/original evidence/);assert.equal(root.querySelector('img'),null);
    const filter=root.querySelector('.agent-learning-filters select[name=kind]');filter.value='facts';filter.dispatchEvent(new env.dom.window.Event('change',{bubbles:true}));await tick();
    assert.match(env.calls.at(-1)[0],/kind=facts/);
    const form=root.querySelector('.agent-memory-results form');
    form.dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
    assert.equal(annotation.kind,'confirm');assert.equal(annotation.expected_revision,3);assert.equal(annotation.content_digest,'digest');
    assert.match(root.textContent,/人工已确认/);assert.match(root.textContent,/高优先级参考/);assert.match(root.textContent,/评估置信度：低/);
    root.querySelector('.agent-memory-results details summary').click();await tick();
    data.activity='working';await env.view.refresh();await tick();
    assert.equal(root.querySelector('.agent-memory-results details').open,true);
    assert.ok(env.calls.filter(([path])=>path.includes('/memory/library')).every(([path])=>!path.includes('disclosures')));
  }finally{env.close();}
});

test('Learning review drafts survive polling and conflicts; reloading uses the latest revision',async()=>{
  const data=fixture();let record=memory(),sent,conflict=true;
  const env=setup(data,async(path,options)=>{
    if(path.includes('/memory/library'))return {records:[structuredClone(record)],total:1,next_offset:null};
    if(path.endsWith('/memory/annotations') && options?.body){sent=options.body;if(conflict)throw Object.assign(new Error('Revision conflict'),{status:409});return {};}
  });
  try{
    await env.open();let form=env.view.root.querySelector('.agent-memory-results form');
    form.elements.kind.value='correct';form.elements.value.value='Corrected project scope';
    form.elements.value.dispatchEvent(new env.dom.window.Event('input',{bubbles:true}));
    data.activity='working';await env.view.refresh();
    assert.equal(env.view.root.querySelector('.agent-memory-results form'),form);
    form.dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
    assert.match(form.textContent,/重新加载后核对/);assert.equal(form.elements.value.value,'Corrected project scope');
    record.source.annotation_revision=4;await env.view.learningView.loadLibrary();
    form=env.view.root.querySelector('.agent-memory-results form');assert.equal(form.elements.value.value,'Corrected project scope');
    conflict=false;form.dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
    assert.equal(sent.expected_revision,4);assert.equal(sent.value,'Corrected project scope');
  }finally{env.close();}
});

test('Human principles move from Profile to Learning and submit current-task scope',async()=>{
  const data=fixture();data.work_packages=[{id:'w',objective:'Current work',state:'running'}];let sent;
  const env=setup(data,async(path,options)=>{if(path.endsWith('/guidance') && options?.body){sent=options.body;data.guidance=[{id:'g',text:sent.text,state:'active',scope:sent.scope,revision:1}];return {};}});
  try{
    await env.open();env.view.tab='Profile';env.view.render();assert.doesNotMatch(env.view.root.textContent,/保存原则/);
    env.view.tab='Learning';env.view.render();await tick();
    const form=env.view.root.querySelector('.agent-human-guidance .agent-form');
    form.elements.text.value='Verify assumptions before changing code';form.elements.scope.value='current_task';
    form.elements.scope.dispatchEvent(new env.dom.window.Event('change',{bubbles:true}));
    assert.equal(form.elements.work.parentElement.hidden,false);
    [...form.querySelectorAll('button')].find(node=>node.textContent==='保存原则').click();await tick();await tick();
    assert.equal(sent.scope,'current_task');assert.equal(sent.work_package_id,'w');
    assert.equal(sent.text,'Verify assumptions before changing code');assert.match(env.view.root.textContent,/生效中/);
  }finally{env.close();}
});

test('Live decisions use existing answer commands, preserve drafts on stale input, and do not auto-submit choices',async()=>{
  const data=fixture();data.pending_decisions=[{session_id:'s/1',task_revision:7,objective:'Choose interface',request:{id:'q',kind:'clarification',question:'Which strategy?',options:['Keep API','Replace API']}}];
  let sent,conflict=true;
  const env=setup(data,async(path,options)=>{
    if(path==='/v1/sessions/s%2F1/commands'){sent=options.body;if(conflict)throw Object.assign(new Error('Input request is stale'),{status:409});data.pending_decisions=[];return {};}
  });
  try{
    await env.open();let form=env.view.root.querySelector('.agent-decisions form');
    [...form.querySelectorAll('button')].find(node=>node.textContent==='Keep API').click();
    assert.equal(sent,undefined);assert.equal(form.elements.answer.value,'Keep API');
    assert.equal(form.querySelector('[aria-pressed=true]').textContent,'Keep API');
    form.dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
    assert.equal(sent.type,'answer_input');assert.equal(sent.expected_task_revision,7);assert.deepEqual(sent.payload,{request_id:'q',answer:'Keep API'});
    assert.equal(form.elements.answer.value,'Keep API');assert.match(form.textContent,/Input request is stale/);
    conflict=false;form.dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
    assert.match(env.view.root.querySelector('.agent-decisions').textContent,/当前没有待回答的问题/);
  }finally{env.close();}
});

test('Workspace-less library selects one owned session and late loads cannot affect another tab',async()=>{
  const data=fixture();data.role.working_directory=null;data.sessions=[{session_id:'one'},{session_id:'two'}];let resolve;
  const env=setup(data,path=>path.includes('/memory/library')?new Promise(done=>{resolve=done;}):undefined);
  try{
    await env.open();assert.match(env.calls.at(-1)[0],/session_id=one/);
    const select=env.view.root.querySelector('select[name=session_id]');select.value='two';select.dispatchEvent(new env.dom.window.Event('change',{bubbles:true}));
    assert.match(env.calls.at(-1)[0],/session_id=two/);
    env.view.tab='Profile';env.view.render();resolve({records:[memory()],total:1});await tick();
    assert.doesNotMatch(env.view.root.textContent,/Observed project fact/);assert.match(env.view.root.textContent,/角色定位/);
  }finally{env.close();}
});
