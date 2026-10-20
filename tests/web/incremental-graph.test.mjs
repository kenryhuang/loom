import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {IncrementalGraphView} from '../../src/loom/web/assets/views/incremental-graph.mjs';
import {EvaluationView} from '../../src/loom/web/assets/views/evaluation.mjs';
import {ReasoningView} from '../../src/loom/web/assets/views/reasoning.mjs';
import {layoutSkeleton, scopeSkeleton} from '../../src/loom/web/assets/views/skeleton-layout.mjs';
const fixture=()=>JSON.parse(readFileSync(new URL('./fixtures/incremental-graph.json',import.meta.url)));
const click=(root,text)=>[...root.querySelectorAll('button')].find(b=>b.textContent===text).click();

test('goal tree replaces DAG, lazily expands actions and retains state across revisions',()=>{
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const root=document.querySelector('main'),data=fixture(),refs=[];
  data.evaluator_usage.reservations[0].input_profile={card_count:4,total_chars:10000,components_chars:{cards:4000,state:500,response_schema:2000,contract:0,extra_evidence:0}};
  data.goals[1].objective='<script>unsafe()</script>';
  const view=new IncrementalGraphView(root,{evidence:r=>refs.push(r)});view.update(data,'job-1');
  assert.ok(root.querySelector('[aria-label="Goal process tree"]'));
  assert.equal(root.querySelector('svg'),null);
  assert.deepEqual([...root.querySelectorAll('[role=tab]')].map(n=>n.textContent),['Skeleton','Acceptance','Report']);
  assert.doesNotMatch(root.textContent,/Task tokens|Calls attributed|Cards reviewed|Evaluator tokens|Export|Entire run/);
  assert.equal(root.querySelector('script'),null);
  assert.equal(root.querySelectorAll('[data-action-detail]').length,0);
  assert.equal(root.querySelectorAll('.goal-tree-stage').length,2); // assessed stage + inferred continuation
  const stage=root.querySelector('.goal-tree-stage');
  stage.open=true;stage.dispatchEvent(new dom.window.Event('toggle'));
  const action=stage.querySelector('.goal-tree-action');
  action.open=true;action.dispatchEvent(new dom.window.Event('toggle'));
  assert.match(action.textContent,/Action intentObserved progress/);
  const original=[...action.querySelectorAll('details')].find(d=>d.querySelector('summary').textContent==='This action’s original records');
  original.open=true;original.dispatchEvent(new dom.window.Event('toggle'));
  original.querySelector('button').click();assert.equal(refs.length,1);
  view.update({...data,graph_revision:2},'job-1');
  assert.ok(root.querySelector('.goal-tree-stage').open);
  assert.ok(root.querySelector('.goal-tree-action').open);
  const goal=root.querySelector('.goal-tree-goal'); goal.open=false;
  view.update({...data,graph_revision:3},'job-1');assert.equal(root.querySelector('.goal-tree-goal').open,false);
  click(root,'Acceptance');assert.match(root.textContent,/No verification receipts recorded/);
  click(root,'Report');assert.match(root.querySelector('.skeleton-report').textContent,/Goal process analysis/);
  assert.match(root.textContent,/Evaluator input cost/);
  click(root,'Skeleton');assert.equal(root.querySelector('.goal-tree-goal').open,false);
  view.update(data,'job-2');assert.equal(root.querySelector('.goal-tree-goal').open,true);
  assert.equal(root.querySelectorAll('[data-action-detail]').length,0);
  const reasoning=new ReasoningView(root,{baseProcess:null,evidence:()=>{},onEvaluate:()=>{},onRefresh:()=>{}});
  reasoning.update(data,{id:'graph'});
  assert.ok(root.querySelector('[aria-label="Goal process tree"]'));
  dom.window.close();
});

test('DAG layout follows result dependencies with branching, joins and no invented ordering',()=>{
  const nodes=Array.from({length:8},(_,i)=>({id:`s${i+1}`,start_line:i*10,kind:'action'}));
  const links=[['s1','s2'],['s2','s3'],['s3','s4'],['s1','s5'],['s2','s5'],['s3','s5'],['s4','s5'],['s5','s6'],['s5','s7'],['s6','s8'],['s7','s8']];
  const edges=links.map(([source_id,target_id])=>({source_id,target_id}));
  const layout=layoutSkeleton(nodes,edges);
  assert.equal(layout.invalid.length,0);
  for(const e of edges) assert.ok(layout.positions.get(e.source_id).y<layout.positions.get(e.target_id).y);
  assert.equal(layout.positions.get('s6').y,layout.positions.get('s7').y);
  assert.notEqual(layout.positions.get('s6').x,layout.positions.get('s7').x);
  assert.ok(layout.positions.get('s8').y>layout.positions.get('s6').y);
  assert.equal(edges.length,11);
  const cycle=layoutSkeleton(nodes,[...edges,{source_id:'s8',target_id:'s5'}]);
  assert.ok(cycle.invalid.includes('s5'));
});

test('goal drilldown retains cross-goal evidence neighbors without cloning their calls',()=>{
  const graph={goals:[{id:'g1'},{id:'g2'},{id:'g3'}],nodes:[
    {id:'a',goal_id:'g1',call_ids:['call-a']},{id:'b',goal_id:'g2',call_ids:['call-b']},{id:'c',goal_id:'g3',call_ids:['call-c']},
    {id:'d',goal_id:'g3',call_ids:['call-d']}],edges:[{source_id:'a',target_id:'b'},{source_id:'b',target_id:'c'},{source_id:'c',target_id:'d'}]};
  const scoped=scopeSkeleton(graph,'g2');
  assert.deepEqual(scoped.nodes.map(n=>n.id),['a','b','c']);
  assert.deepEqual([...scoped.focus],['b']);
  assert.equal(scoped.nodes[2],graph.nodes[2]);
  assert.equal(scoped.edges.length,2);
});

test('new default starts an explicitly scoped graph evaluation and incomplete result can resume',async()=>{
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const controller=new AbortController(),root=document.querySelector('main'),requests=[],errors=[];
  const settings={max_calls:24,max_tokens:240000,max_seconds:600,max_evidence_chars:400000};
  const data=fixture(),job={id:'g',analysis_version:'goal_graph_v1',run_id:'run',state:'incomplete',settings,
    model:'judge',usage:{calls:1,total_tokens:150},evaluation:data,error:'One group unresolved'};
  const api={evaluations:async()=>({models:[{id:'judge',label:'Judge'}],default_model:'judge',run_ids:['run'],
    analysis_versions:['goal_graph_v1','goal_loops_v1','v3'],default_analysis_version:'goal_graph_v1',defaults:settings,
    defaults_by_version:{goal_graph_v1:settings},bounds:Object.fromEntries(Object.keys(settings).map(k=>[k,[1,1000000]])),jobs:[]}),
    startEvaluation:async(...args)=>{requests.push(args[2]);return job;},evaluation:async()=>job};
  const view=new EvaluationView(root,{api,sessionId:'s',analysisId:'a',signal:controller.signal,evidence:()=>{},
    onError:e=>errors.push(e),onResult:()=>{},onExplore:()=>{}});
  await view.open();assert.equal(view.version.value,'goal_graph_v1');assert.equal(view.runLabel.hidden,false);
  assert.equal(view.fields.max_tokens.parentElement.hidden,true);
  await view.run();await new Promise(r=>setTimeout(r,0));
  assert.equal(requests[0].analysis_version,'goal_graph_v1');assert.equal(requests[0].run_id,'run');
  assert.ok(root.querySelector('[aria-label="Goal process tree"]'));
  const resume=[...root.querySelectorAll('button')].find(b=>b.textContent.includes('Resume'));
  assert.ok(resume);assert.equal(resume.disabled,false);
  assert.deepEqual(errors,[]);controller.abort();dom.window.close();
});

test('goal structure remains visibly pending when all cards have judgments',()=>{
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const root=document.querySelector('main'),data=fixture();
  data.coverage.pending_cards=0;
  data.coverage.pending_structure_units=['batch:1','batch:2'];
  data.coverage.review_gaps=[{batch_id:'batch:1',reason:'Undelivered evidence'}];
  const view=new IncrementalGraphView(root);view.update(data,'pending-structure');
  assert.match(root.textContent,/Goal structure pending · 2 windows/);
  click(root,'Report');
  assert.match(root.textContent,/Validation issues · 1/);
  assert.doesNotMatch(root.textContent,/Unresolved groups/);
  dom.window.close();
});
