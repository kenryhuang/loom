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

test('incremental projection keeps unknown work, evidence and report in one revision without fetching',()=>{
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const root=document.querySelector('main'),data=fixture(),refs=[];
  data.evaluator_usage.reservations[0].input_profile={card_count:4,total_chars:10000,components_chars:{cards:4000,state:500,response_schema:2000,contract:0,extra_evidence:0}};
  data.skeleton.nodes[0].objective='<script>unsafe()</script>';
  const view=new IncrementalGraphView(root,{evidence:r=>refs.push(r)});view.update(data);
  assert.equal(root.querySelectorAll('svg g[role="button"]').length,4); // Root plus three occurrences.
  assert.deepEqual([...root.querySelectorAll('[role=tab]')].map(n=>n.textContent),['Skeleton','Report']);
  assert.doesNotMatch(root.textContent,/Evaluator input cost|Task tokens|Calls attributed|Cards reviewed|Evaluator tokens|Revision|Export|Entire run/);
  assert.equal(root.querySelector('.skeleton-toolbar').textContent,'SkeletonReport');
  assert.equal(root.querySelector('nav'),null);
  assert.equal(root.querySelector('script'),null);
  assert.equal([...root.querySelectorAll('button')].some(b=>b.textContent==='Show sequence edges'),false);
  assert.equal(root.querySelectorAll('svg path[stroke-dasharray]').length,2);
  root.querySelector('svg g[data-node-id="'+data.skeleton.nodes[0].id+'"]').dispatchEvent(new dom.window.MouseEvent('click'));
  assert.match(root.textContent,/Captured/);assert.match(root.textContent,/Presented/);assert.match(root.textContent,/Adopted/);
  const selected=view.selected;
  [...root.querySelectorAll('button')].find(b=>b.textContent.startsWith('L2')).click();
  assert.equal(refs.length,1);
  view.update({...data,graph_revision:2});assert.equal(view.selected,selected);
  click(root,'Report');assert.match(root.querySelector('.skeleton-report').textContent,/Goal process analysis/);
  assert.match(root.textContent,/Evaluator input cost/);
  assert.match(root.textContent,/Reported input \/ output tokens/);
  assert.match(root.textContent,/4,000|4000/);
  click(root,'Skeleton');assert.equal(view.selected,selected);
  root.querySelector('svg g[data-node-id="root:'+data.goals.find(g=>!g.parent_goal_id).id+'"]').dispatchEvent(new dom.window.MouseEvent('click'));
  const goal=data.goals.find(g=>g.parent_goal_id);
  click(root,`G1 · ${goal.objective}`);
  assert.match(root.querySelector('.skeleton-detail').textContent,/G1/);
  assert.match(root.querySelector('.skeleton-detail').textContent,/S1/);
  click(root,'Close details');assert.equal(view.goal,null);
  assert.equal(root.querySelector('.skeleton-inspector'),null);
  const reasoning=new ReasoningView(root,{baseProcess:null,evidence:()=>{},onEvaluate:()=>{},onRefresh:()=>{}});
  reasoning.update(data,{id:'graph'});
  assert.equal(root.querySelectorAll('svg[aria-label="Goal process graph"]').length,1);
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
  assert.equal(root.querySelectorAll('svg[aria-label="Goal process graph"]').length,1);
  const resume=[...root.querySelectorAll('button')].find(b=>b.textContent.includes('Resume'));
  assert.ok(resume);assert.equal(resume.disabled,false);
  assert.deepEqual(errors,[]);controller.abort();dom.window.close();
});
