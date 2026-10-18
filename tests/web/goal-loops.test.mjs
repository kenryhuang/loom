import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {GoalLoopView, renderGoalSummary} from '../../src/loom/web/assets/views/goal-loops.mjs';
import {EvaluationView} from '../../src/loom/web/assets/views/evaluation.mjs';
import {ReasoningView} from '../../src/loom/web/assets/views/reasoning.mjs';
const fixture = () => JSON.parse(readFileSync(new URL('./fixtures/goal-loops.json', import.meta.url)));
const click = (root, text) => [...root.querySelectorAll('button')].find(b => b.textContent === text).click();

test('goal graph separates semantic feedback, source order, and immutable shared cost', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const data=fixture(), root=document.querySelector('main'), refs=[];
  const view=new GoalLoopView(root,{evidence:r=>refs.push(r)}); view.update(data);
  assert.equal(root.querySelectorAll('.goal-graph-node').length,3);
  assert.equal(root.querySelectorAll('.goal-process-edge.semantic').length,1);
  assert.equal(root.querySelectorAll('.goal-process-edge.order').length,4);
  assert.match(root.querySelector('.goal-graph-endpoint.end').textContent,/Boundary/);
  assert.equal(root.querySelector('script'),null);
  const before=JSON.stringify(data.cost_ledger);
  root.querySelector('.goal-graph-node').dispatchEvent(new dom.window.MouseEvent('click'));
  assert.match(root.textContent,/Recorded calls/);
  const evidence=[...root.querySelectorAll('button')].find(b=>b.textContent.startsWith('L2'));
  evidence.click(); assert.equal(refs[0].line_number,2);
  click(root,'Repeated investigation needs review');
  assert.equal(root.querySelectorAll('.goal-graph-node.highlighted').length,3);
  assert.match(root.textContent,/not estimated savings/);
  assert.equal(JSON.stringify(data.cost_ledger),before);
  const selected=view.selected;
  view.update({...data,graph_revision:3,coverage:{...data.coverage,status:'partial'}});
  assert.equal(view.selected,selected);
  assert.match(root.textContent,/partial/i);
  dom.window.close();
});

test('partial goal structure renders evidence before semantic phases and retains selection through aliases', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const data=fixture(), root=document.querySelector('main'),view=new GoalLoopView(root,{evidence:()=>{}});
  data.phases=[];data.attempts=[];data.relations=[];data.diagnostics=[];data.coverage.status='partial';
  view.selected='old';data.aliases={old:data.goals[0].id};view.update(data);
  assert.equal(view.selected,data.goals[0].id);
  assert.match(root.textContent,/No semantic phases established/);
  assert.match(root.textContent,/Recorded timeline/);
  assert.equal(root.querySelectorAll('.goal-graph-node').length,0);
  const search=root.querySelector('[aria-label="Search goals"]');search.value='absent';search.dispatchEvent(new dom.window.Event('input'));
  assert.equal(root.querySelectorAll('[aria-label="Goal tree"] .goal-tree-row').length,0);
  dom.window.close();
});

test('new evaluation version sends explicit run scope and links to goal process', async () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const root=document.querySelector('main'), controller=new AbortController(), calls=[], errors=[], results=[], explore=[];
  const settings={max_calls:24,max_tokens:120000,max_seconds:600,max_evidence_chars:400000};
  const data=fixture(), job={id:'g',analysis_version:'goal_loops_v1',run_id:'second',state:'completed',settings,
    model:'judge',usage:{calls:3,total_tokens:90},evaluation:data,reviewed_packets:8,total_packets:8};
  const api={evaluations:async()=>({models:[{id:'judge',label:'Judge'}],default_model:'judge',run_ids:['first','second'],
    analysis_versions:['goal_loops_v1','v3'],default_analysis_version:'v3',defaults:settings,
    defaults_by_version:{goal_loops_v1:settings,v3:settings},bounds:Object.fromEntries(Object.keys(settings).map(k=>[k,[1,1000000]])),jobs:[]}),
    startEvaluation:async(...args)=>{calls.push(args[2]);return job;},evaluation:async()=>job};
  const view=new EvaluationView(root,{api,sessionId:'s',analysisId:'a',signal:controller.signal,evidence:()=>{},
    onError:e=>errors.push(e),onResult:r=>results.push(r),onExplore:()=>explore.push(true)});
  await view.open();
  assert.equal(view.runLabel.hidden,true);
  view.version.value='goal_loops_v1';view.version.dispatchEvent(new dom.window.Event('change'));
  assert.equal(view.runLabel.hidden,false);assert.equal(typeof view.run,'function');
  assert.equal(view.runSelect.value,'second');
  await view.run();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(calls[0].run_id,'second');assert.equal(calls[0].analysis_version,'goal_loops_v1');
  assert.equal(errors.length,0);assert.equal(results.at(-1).schema_version,'loom.goal-loop-analysis.v1');
  assert.equal(root.querySelector('[aria-label="Reviewed evidence coverage"]').value,8);
  click(root,'Open goal process');assert.equal(explore.length,1);
  controller.abort();dom.window.close();
});

test('new and legacy Reasoning renderers switch without mixing results', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const root=document.querySelector('main'), view=new ReasoningView(root,{baseProcess:null,evidence:()=>{},onEvaluate:()=>{},onRefresh:()=>{}});
  view.update(fixture(),{id:'new'});assert.equal(root.querySelectorAll('.goal-process-graph').length,1);
  view.update({semantic:{}},{id:'old'});assert.equal(root.querySelector('.goal-process-graph'),null);
  view.update(fixture(),{id:'new'});assert.equal(root.querySelectorAll('.goal-process-graph').length,1);
  const summary=document.createElement('section');renderGoalSummary(summary,fixture(),()=>{});
  assert.equal(summary.querySelector('svg'),null);
  dom.window.close();
});

test('exports preserve the versioned ledger and render a standalone escaped SVG summary', async () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const create=URL.createObjectURL, revoke=URL.revokeObjectURL, blobs=[];
  URL.createObjectURL=blob=>{blobs.push(blob);return 'blob:fixture';};URL.revokeObjectURL=()=>{};
  dom.window.HTMLAnchorElement.prototype.click=()=>{};
  try {
    const root=document.querySelector('main'),data=fixture();new GoalLoopView(root,{evidence:()=>{}}).update(data);
    click(root,'Export JSON');click(root,'Export static view');
    assert.deepEqual(JSON.parse(await blobs[0].text()).cost_ledger,data.cost_ledger);
    const html=await blobs[1].text();assert.match(html,/<style>/);assert.match(html,/<svg/);
    assert.match(html,/Summary, not a complete evidence package/);assert.match(html,/UTC/);
    assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  } finally {URL.createObjectURL=create;URL.revokeObjectURL=revoke;dom.window.close();}
});

test('protocol coverage gaps and diagnostic windows stay visible after pipeline completion', () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root=document.querySelector('main'), data=fixture();
  Object.assign(data.coverage,{status:'partial',pipeline_complete:true,execution_protocol:2,split_requests:3,
    review_gaps:[{stage:'structure:1',unit:'0.a',reason:'Atomic output truncated'}],
    diagnosis_windows:{[data.goals[0].id]:{delivered_packet_ids:['p1'],available_packets:9,partial:true}},
    reconciliation_decisions:[{candidate_id:'candidate:x',resolution:'unresolved',rationale:'Missing evidence'}]});
  new GoalLoopView(root,{evidence:()=>{}}).update(data);
  assert.match(root.textContent,/Partial window/); assert.match(root.textContent,/Atomic output truncated/);
  assert.match(root.textContent,/unresolved/i); assert.doesNotMatch(root.textContent,/Pipeline complete/);
  const summary=document.createElement('section'); renderGoalSummary(summary,data);
  assert.match(summary.textContent,/Protocolv2/); assert.match(summary.textContent,/Review gaps1/);
  dom.window.close();
});

test('phase drilldown shows timed public statement/action/observation groups and all unassigned calls', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const data=JSON.parse(readFileSync(new URL('./fixtures/execution-view.json',import.meta.url)));
  const root=document.querySelector('main'),refs=[];
  const view=new GoalLoopView(root,{evidence:r=>refs.push(r)});view.mode="detailed";view.update(data);
  assert.match(root.textContent,/Run time · all recorded calls/);
  assert.match(root.textContent,/2 calls outside the phase graph/);
  const node=root.querySelector('.goal-graph-node');
  assert.match(node.textContent,/Linked: model/);assert.match(node.textContent,/Window .*3 other calls/);
  assert.ok(node.querySelector('.goal-phase-time-bar'));
  node.dispatchEvent(new dom.window.MouseEvent('click'));
  assert.equal(root.querySelectorAll('.execution-cycle').length,2);
  assert.match(root.textContent,/Public statement \/ proposed action/);
  assert.match(root.textContent,/Need evidence for file 1/);
  assert.match(root.textContent,/→ invokes · tool call ID/);
  assert.match(root.textContent,/→ returns · same call/);
  assert.match(root.textContent,/Unassigned to a phase/);
  assert.equal([...root.querySelectorAll('details')].find(d=>d.querySelector('summary')?.textContent==='Semantic assessment · saved interpretation').open,false);
  const evidence=[...root.querySelectorAll('.execution-process-node.model button')].find(b=>b.textContent.includes('response.content'));
  evidence.click();assert.equal(refs.at(-1).field_path,'response.content');
  const select=root.querySelector('[aria-label="Execution detail scope"]');select.value='linked';select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(root.querySelectorAll('.execution-cycle').length,1);
  click(root,'Explore unmapped execution');
  assert.equal(root.querySelector('[aria-label="Execution detail scope"]').value,'outside');
  assert.equal(root.querySelectorAll('.execution-cycle').length,1);
  assert.match(root.querySelector('.execution-cycle').textContent,/Need evidence for file 3/);
  click(root,'Explore all 6 calls');assert.equal(root.querySelectorAll('.execution-cycle').length,3);
  dom.window.close();
});

test('execution excerpts are escaped and incomplete intervals remain unknown', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const data=JSON.parse(readFileSync(new URL('./fixtures/execution-view.json',import.meta.url)));
  data.execution_view.calls[0].statement='<script>alert(1)</script>';
  data.execution_view.calls[0].statement_truncated=true;
  data.execution_view.run.timing.missing_intervals=1;
  const root=document.querySelector('main'),view=new GoalLoopView(root,{evidence:()=>{}});view.mode='detailed';view.update(data);
  click(root,'Explore all 6 calls');
  assert.equal(root.querySelector('script'),null);assert.match(root.textContent,/<script>alert\(1\)<\/script>/);
  assert.match(root.textContent,/partition is partial/);assert.match(root.textContent,/open source for full public response/);
  dom.window.close();
});

test('action flow is the default: compact stages and one row per recorded action', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const data=JSON.parse(readFileSync(new URL('./fixtures/execution-view.json',import.meta.url)));
  data.execution_view.calls[0].stream_metrics={http_receive_ms:9000,receive_queue_max_lag_ms:20,delta_emit_ms:30,completion_emit_ms:50};
  const root=document.querySelector('main'),refs=[],view=new GoalLoopView(root,{evidence:r=>refs.push(r)});view.update(data);
  assert.equal(root.querySelector('.goal-process-graph'),null);
  assert.equal(root.querySelectorAll('.action-flow-stage').length,1);
  assert.match(root.querySelector('.action-flow-stage').textContent,/Research/);
  assert.match(root.querySelector('.action-flow-stage').textContent,/3 model · 3 tool/);
  assert.equal(root.querySelectorAll('tbody tr').length,9);
  assert.equal(root.querySelector('.action-flow-detail'),null);
  click(root,'Working · Preparing read_file');
  let toggle=root.querySelector('.action-flow-toggle[aria-expanded="true"]');
  assert.equal(toggle.closest('tr').nextElementSibling.querySelector('.action-flow-detail'),root.querySelector('.action-flow-detail'));
  assert.equal(root.querySelector('.action-flow-detail').id,toggle.getAttribute('aria-controls'));
  view.update(data);
  toggle=root.querySelector('.action-flow-toggle[aria-expanded="true"]');
  assert.ok(toggle.closest('tr').nextElementSibling.querySelector('.action-flow-detail'));
  assert.match(root.querySelector('.action-flow-detail').textContent,/Need evidence for file 1/);
  assert.match(root.textContent,/Gap · unclassified/);
  assert.match(root.querySelector('.action-flow-detail').textContent,/First response · TTFT/);
  assert.match(root.querySelector('.action-flow-detail').textContent,/After first response/);
  assert.match(root.querySelector('.action-flow-detail').textContent,/HTTP stream received/);
  assert.match(root.querySelector('.action-flow-detail').textContent,/Receive queue · max lag/);
  assert.match(root.querySelector('.action-flow-detail').textContent,/Final persistence wait/);
  click(root,'L2');assert.equal(refs.at(-1).line_number,2);
  toggle.click();assert.equal(toggle.getAttribute('aria-expanded'),'false');
  assert.equal(root.querySelector('.action-flow-detail'),null);
  view.update(data);assert.equal(root.querySelector('.action-flow-detail'),null);
  click(root,'Detailed analysis');assert.ok(root.querySelector('.goal-process-graph'));
  click(root,'Action flow');assert.ok(root.querySelector('.action-flow-rail'));
  assert.equal(root.querySelector('.goal-process-graph'),null);
  dom.window.close();
});

test('selecting another action stage replaces the action rows without showing old graph noise', () => {
  const dom=new JSDOM('<main></main>');globalThis.document=dom.window.document;
  const data=JSON.parse(readFileSync(new URL('./fixtures/execution-view.json',import.meta.url))),flow=data.execution_view.action_flow;
  const action=flow.actions.pop();flow.segments[0].action_ids.pop();
  action.kind='implementation';action.title='Edit file';action.segment_id='edited';flow.actions.push(action);
  flow.segments.push({id:'edited',kind:'implementation',label:'Edit / implement',elapsed_ms:1000,shared_ms:0,action_ids:[action.id],missing_intervals:0,model_ms:0,tool_ms:1000,model_calls:0,tool_calls:1});
  const root=document.querySelector('main'),view=new GoalLoopView(root,{evidence:()=>{}});view.update(data);
  const edit=[...root.querySelectorAll('.action-flow-stage')].find(b=>b.textContent.includes('Edit / implement'));edit.click();
  assert.equal(root.querySelectorAll('tbody tr').length,1);assert.match(root.textContent,/Edit file/);
  assert.equal(root.querySelector('.goal-process-graph'),null);assert.equal(view.selectedActionStage,'edited');
  view.update(data);assert.equal(root.querySelector('.action-flow-stage[aria-pressed="true"]').textContent.includes('Edit / implement'),true);
  dom.window.close();
});
