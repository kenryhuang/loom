import {element} from '../markdown.mjs';
import {cards, disclosure, label, panel, seconds, table} from './dashboard.mjs';
import {utilityDetails, utilityLabel, utilitySummary} from './action-utility.mjs';

const button=(text, action, cls='quiet')=>{const b=element('button',cls,text);b.type='button';b.addEventListener('click',action);return b;};
const elapsed=row=>`${seconds(row.elapsed_ms)}${row.shared_ms ? ` + ${seconds(row.shared_ms)} shared` : ''}${row.missing_intervals ? ' + unknown' : ''}`;
const duration=action=>action.duration_ms===0 && action.duration_basis==='timestamps' ? '<1s (timestamp precision)' :
  action.duration_ms==null ? '—' : `${(action.duration_ms/1000).toLocaleString(undefined,{maximumFractionDigits:2})} s`;
const clock=value=>{
  const node=element('time');
  if(value==null){node.textContent='—';return node;}
  const date=new Date(value);
  if(!Number.isFinite(date.getTime())){node.textContent='—';return node;}
  node.dateTime=date.toISOString();node.title=date.toLocaleString();
  node.textContent=date.toLocaleTimeString(undefined,{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});return node;
};

export function renderActionFlow(root, data, {evidence, advanced, selected, select, expandedActions = new Set()}) {
  const view=data.execution_view, flow=view.action_flow;
  const header=element('div','dashboard-toolbar');
  header.append(element('strong','','Execution flow'),button('Detailed analysis',advanced));root.append(header);
  const objective=data.goals.find(g=>!g.parent_goal_id)?.objective;
  if(objective) root.append(element('p','action-flow-objective',objective));
  const summary=panel(root,'Execution time',true);
  cards(summary,[['Call span',seconds(flow.wall_span_ms)],
    ['Working · model',seconds(flow.segments.reduce((n,s)=>n+s.model_ms,0))],
    ['Tool execution',seconds(flow.segments.reduce((n,s)=>n+s.tool_ms,0))],['Between calls',seconds(flow.gap_ms)]]);
  utilitySummary(summary,data.action_utility);
  if(flow.parallel_ms) summary.append(element('p','muted',`Concurrent actions · ${seconds(flow.parallel_ms)} shared`));
  if(flow.missing_intervals) summary.append(element('p','muted',`${flow.missing_intervals} calls have no complete timing; totals are partial`));
  const rail=element('nav','action-flow-rail');rail.setAttribute('aria-label','Execution stages');
  const chosen=flow.segments.find(s=>s.id===selected) || [...flow.segments].sort((a,b)=>b.elapsed_ms-a.elapsed_ms)[0];
  const max=Math.max(1,...flow.segments.map(s=>s.elapsed_ms));
  for(const [i,stage] of flow.segments.entries()) {
    if(i) rail.append(element('span','action-flow-arrow','→'));
    const node=button('',()=>select(stage.id),`action-flow-stage ${stage.kind}`);
    node.setAttribute('aria-pressed',String(stage.id===chosen?.id));
    node.append(element('strong','',stage.label),element('span','action-flow-time',elapsed(stage)),
      element('span','muted',`Working ${seconds(stage.model_ms)} · Tools ${seconds(stage.tool_ms)}`),
      element('span','muted',`${stage.model_calls} model · ${stage.tool_calls} tool${stage.failed_actions?` · ${stage.failed_actions} failed`:''}`));
    if(stage.inferred_actions) node.append(element('span','muted',`${stage.inferred_actions} actions · inferred purpose`));
    const meter=element('span','action-flow-meter'),fill=element('span');fill.style.width=`${stage.elapsed_ms/max*100}%`;meter.append(fill);node.append(meter);
    rail.append(node);
  }
  root.append(rail);
  if(!chosen){root.append(element('p','muted','No recorded actions'));return;}
  const pane=panel(root,`${chosen.label} · ${elapsed(chosen)}`,true);
  const actions=flow.actions.filter(a=>chosen.action_ids.includes(a.id)),calls=new Map(view.calls.map(c=>[c.id,c]));
  const inspect=(action,details)=>{
    details.append(element('h3','',`Action ${action.ordinal} · ${action.title}`));
    const call=calls.get(action.id);
    if(call.kind==='model'){
      const after=call.duration_ms!=null && call.ttft_ms!=null && call.duration_ms>=call.ttft_ms ? call.duration_ms-call.ttft_ms : null;
      cards(details,[['Model call',duration(action)],['First response · TTFT',seconds(call.ttft_ms)],['After first response',seconds(after)],
        ['Input / output tokens',`${call.prompt_tokens??'—'} / ${call.completion_tokens??'—'}`],['TPS',call.tps==null?'—':call.tps.toFixed(1)]]);
      details.append(element('p','muted','Model time includes waiting, reasoning and response generation. After first response is not a separate measurement of pure reasoning.'));
      const metrics=call.stream_metrics;
      if(metrics){
        cards(details,[['HTTP stream received',seconds(metrics.http_receive_ms)],['Receive queue · max lag',seconds(metrics.receive_queue_max_lag_ms)],
          ['Delta emission wait',seconds(metrics.delta_emit_ms)],['Final persistence wait',seconds(metrics.completion_emit_ms)]]);
        details.append(element('p','muted','HTTP time includes connection, provider and network time. Queue lag and emission wait may overlap; do not add them. Final persistence wait follows the measured model duration.'));
      }
    }
    utilityDetails(details,call.utility,evidence);
    const refs=disclosure(details,'Recorded statements and results');refs.open=true;
    const block=element('section','action-flow-source');block.append(element('strong','',`${call.tool||call.model} · ${duration(action)}`));
    block.append(element('p','execution-excerpt',call.kind==='model'?call.statement:call.observation));
    const links=call.kind==='model'?call.statement_refs:call.observation_refs;
    for(const ref of links?.length?links:call.evidence_refs) block.append(button(`L${ref.line_number}`,()=>evidence(ref)));
    refs.append(block);
    const basis=disclosure(details,'Classification and timing');
    basis.append(element('p','',action.basis.join(' · ')),element('p','muted',`Exclusive timeline time: ${elapsed(action)}. Rows show recorded call durations; stage totals partition timestamps. Nested model time is excluded from the enclosing tool; overlapping rows must not be summed.`));
    if(action.classification_inferred) basis.append(element('p','muted','Purpose is inferred; it does not establish that the action succeeded or the check was sufficient.'));
    for(const reason of action.classification_reasons||[]) basis.append(element('p','',reason));
    for(const ref of action.classification_evidence||[]) basis.append(button(`L${ref.line_number}`,()=>evidence(ref)));
  };
  const rows=[],expanders=[];
  for(const action of actions){
    for(const gap of flow.gaps||[]) if(gap.next_action_ids[0]===action.id){
      rows.push(['—',element('span','action-flow-gap','Gap · unclassified'), 'Between recorded calls', clock(gap.started_ms),clock(gap.ended_ms),seconds(gap.duration_ms),'—','—']);
    }
    let detailRow;
    const toggle=button(action.title,()=>{
      if(expandedActions.has(action.id)) {expandedActions.delete(action.id);detailRow?.remove();detailRow=null;}
      else {expandedActions.add(action.id);show();}
      toggle.setAttribute('aria-expanded',String(expandedActions.has(action.id)));
    },`quiet action-flow-toggle action-flow-${action.activity}`);
    toggle.setAttribute('aria-expanded',String(expandedActions.has(action.id)));
    toggle.setAttribute('aria-controls',`action-detail-${action.ordinal}`);
    const show=()=>{
      detailRow=element('tr','action-flow-detail-row');
      const cell=element('td'),details=element('section','action-flow-detail');
      cell.colSpan=8;details.id=`action-detail-${action.ordinal}`;
      inspect(action,details);cell.append(details);detailRow.append(cell);
      toggle.closest('tr').after(detailRow);
    };
    expanders.push(()=>{if(expandedActions.has(action.id)) show();});
    rows.push([action.ordinal,toggle,action.target||'—',
      clock(action.started_at),clock(action.ended_at),duration(action),utilityLabel(calls.get(action.id)?.utility),label(action.status)]);
  }
  const timeline=table(pane,['#','Action','Model / target','Started','Ended','Call time','Utility','Status'],rows);
  timeline.classList.add('action-flow-table');
  for(const row of timeline.tBodies[0].rows) if(row.querySelector('.action-flow-gap')) row.classList.add('action-flow-gap-row');
  for(const expand of expanders) expand();
  const notes=disclosure(root,'How this view is grouped');
  notes.append(element('p','muted','Each Working model call and tool execution has its own row. Model purpose follows recorded native or JSON action links. Classification uses recorded operations, then cited saved assessments or declared action intent; inferred purposes are marked. Gaps are uncovered timestamp intervals, not a diagnosis of framework overhead. No additional evaluation is needed.'));
}
