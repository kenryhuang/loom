import {element} from '../markdown.mjs';
import {cards, label, number, seconds} from './dashboard.mjs';

const parts = [['model_only_ms','Model only','model'],['tool_only_ms','Tool only','tool'],['overlap_ms','Model + tool overlap','overlap'],['gap_ms','Gaps between calls','gap']];
const button = (text, action) => {const b=element('button','quiet',text); b.type='button'; b.addEventListener('click',action); return b;};
const known = (cost, field) => cost?.[field]?.measured_count ? seconds(cost[field].known_sum) : cost?.[field]?.eligible_count ? 'Unknown' : '0 s';
export function renderTimePartition(root, timing) {
  if(timing.wall_span_ms == null) {root.append(element('p','muted','No comparable call timestamps')); return;}
  const bar=element('div','execution-time-bar');bar.setAttribute('role','img');
  bar.setAttribute('aria-label',parts.map(([key,title])=>`${title}: ${seconds(timing[key])}`).join('; '));
  const legend=element('div','execution-time-legend');
  for(const [key,title,kind] of parts) {
    const text=`${title} · ${seconds(timing[key])}`;
    if(timing[key]>0) {const part=element('span',`execution-time-part ${kind}`);part.style.width=`${timing[key]/Math.max(1,timing.wall_span_ms)*100}%`;part.title=text;bar.append(part);}
    legend.append(element('span',`execution-time-key ${kind}`,text));
  }
  root.append(bar,legend);
  if(timing.missing_intervals) root.append(element('p','muted',`${timing.missing_intervals} calls without comparable timestamps; partition is partial`));
}
export function renderExecutionTime(root, view, inspect) {
  const run=view.run;
  cards(root,[['Call span',seconds(run.timing.wall_span_ms)],['Model total',known(run.costs,'model_duration_ms')],
    ['Tool total',known(run.costs,'tool_duration_ms')],['Unassigned calls',run.unassigned_call_ids.length]]);
  renderTimePartition(root,run.timing);
  const actions=element('div','execution-controls');
  actions.append(button(`Explore all ${run.call_ids.length} calls`,()=>inspect('all')),
    button(`Unassigned · ${run.unassigned_call_ids.length}`,()=>inspect('unassigned')),
    button(`Outside phase windows · ${run.outside_phase_call_ids.length}`,()=>inspect('outside')));
  root.append(actions);
}
function citation(root, refs, evidence) {
  for(const ref of refs || []) root.append(button(`L${ref.line_number}${ref.field_path ? ` · ${ref.field_path}` : ''}`,()=>evidence(ref)));
}
function excerpt(root, text, max=420) {
  root.append(element('p','execution-excerpt',text?.slice(0,max) || 'No public statement recorded'));
  if(text?.length>max) {const details=element('details');details.append(element('summary','','More recorded text'),element('p','execution-excerpt',text));root.append(details);}
}
function interval(call, origin) {
  const a=Date.parse(call.started_at), b=Date.parse(call.ended_at);
  return Number.isFinite(a) && Number.isFinite(b) && origin!=null ? `+${seconds(a-origin)} → +${seconds(b-origin)}` : 'Time unavailable';
}
function performance(root, call, view) {
  root.append(element('div','execution-call-meta',`${interval(call,view.run.timing.start_ms)} · ${seconds(call.duration_ms)} · ${label(call.status)}`));
  const measured=view.calls.filter(c=>c.duration_ms!=null), max=Math.max(1,...measured.map(c=>c.duration_ms));
  const track=element('div','execution-duration-track'),bar=element('span',`execution-duration-fill ${call.kind}`);
  if(call.duration_ms!=null) bar.style.width=`${call.duration_ms/max*100}%`;
  track.title='Call duration relative to the longest call in this run';track.append(bar);root.append(track);
  if(call.kind==='model') root.append(element('div','execution-call-meta',`TTFT ${seconds(call.ttft_ms)} · ${call.tps==null?'TPS unknown':`${call.tps.toFixed(1)} TPS`} · ${number(call.completion_tokens)} output tokens`));
}
export function renderExecutionProcess(root, view, {phaseId=null, initialScope, evidence, onScopeChange}) {
  const phase=view.phases[phaseId], controls=element('div','execution-controls'), select=element('select');
  select.setAttribute('aria-label','Execution detail scope');
  const scopes=phase ? [['window',`Phase evidence window · ${phase.call_ids.length}`],['linked',`Linked to phase · ${phase.related_call_ids.length}`]] : [];
  scopes.push(['all',`Whole run · ${view.run.call_ids.length}`],['unassigned',`Unassigned · ${view.run.unassigned_call_ids.length}`],['outside',`Outside phase windows · ${view.run.outside_phase_call_ids.length}`]);
  for(const [id,title] of scopes) {const option=element('option','',title);option.value=id;select.append(option);}
  select.value=initialScope || (phase?'window':'all');
  const content=element('div','execution-cycle-list');
  controls.append(select);root.append(controls,content);
  const render=()=>{
    content.replaceChildren();
    const ids=new Set(select.value==='window'?phase.call_ids:select.value==='linked'?phase.related_call_ids:
      select.value==='outside'?view.run.outside_phase_call_ids:select.value==='unassigned'?view.run.unassigned_call_ids:view.run.call_ids);
    const calls=new Map(view.calls.map(c=>[c.id,c]));
    const rounds=view.rounds.filter(r=>ids.has(r.model_call_id)||r.tool_call_ids.some(i=>ids.has(i)));
    const scopeText=select.value==='window'?'Calls in this evidence window; time proximity does not assign them to the phase.':
      select.value==='outside'?'Recorded calls outside all current phase windows.':'Solid links: recorded tool invocation / result. Between groups: source order, not proven evidence adoption.';
    content.append(element('p','muted small',scopeText));
    if(phase && select.value==='window') {
      cards(content,[['Evidence-window span',seconds(phase.timing.wall_span_ms)],['Linked calls',`${phase.related_call_ids.length} / ${phase.call_ids.length}`],
        ['Other calls in window',phase.other_call_ids.length]]);
      renderTimePartition(content,phase.timing);
    }
    const longest=[...ids].map(i=>calls.get(i)).filter(Boolean).sort((a,b)=>(b.duration_ms||0)-(a.duration_ms||0))[0]?.id;
    let shown=0, limit=30;
    const append=()=>{
      const page=rounds.slice(shown,shown+limit);
      for(const round of page) {
        const model=ids.has(round.model_call_id)?calls.get(round.model_call_id):null;
        const tools=round.tool_call_ids.filter(i=>ids.has(i)).map(i=>calls.get(i));
        const group=element('details','execution-cycle');group.open=shown<2||model?.id===longest||tools.some(t=>t.id===longest);
        const summary=element('summary'), name=model ? (model.role==='verification'?'Verification model':'Model response') : 'Tool activity';
        const title=tools.map(t=>t.target||t.tool).join(' · ') || model?.statement?.slice(0,110) || name;
        summary.append(element('strong','',`Round ${++shown} · ${name}`),element('span','execution-cycle-title',title),
          element('span','execution-cycle-cost',model ? `Model ${seconds(model.duration_ms)} · ${tools.length} tools` : `${tools.length} tools`));
        group.append(summary);
        const graph=element('div','execution-round-graph');graph.setAttribute('role','group');graph.setAttribute('aria-label',`Recorded execution round ${shown}`);
        const statement=element('section','execution-process-node model');statement.append(element('h4','',model?'Public statement / proposed action':'Caller outside selection or unavailable'));
        if(model) {
          performance(statement,model,view);excerpt(statement,model.statement);
          statement.append(element('span',`execution-owner${model.owner==='analysis:unassigned'?' unassigned':''}`,model.owner==='analysis:unassigned'?'Unassigned to a phase':'Has recorded cost owner'));
          citation(statement,model.statement_refs?.length?model.statement_refs:model.evidence_refs,evidence);
          if(model.statement_truncated) statement.append(element('small','muted','Excerpt; open source for full public response'));
        }
        graph.append(statement);
        const steps=element('div','execution-tool-steps');
        for(const tool of tools) {
          const pair=element('div','execution-tool-pair');
          const action=element('section',`execution-process-node tool${tool.status==='failed'?' failed':''}`);
          action.append(element('div','execution-link-label',model?'→ invokes · tool call ID':'Recorded action'),element('h4','',`${label(tool.category)} · ${tool.tool}`));
          performance(action,tool,view);excerpt(action,tool.target||tool.input,220);
          action.append(element('span',`execution-owner${tool.owner==='analysis:unassigned'?' unassigned':''}`,tool.owner==='analysis:unassigned'?'Unassigned to a phase':'Has recorded cost owner'));
          citation(action,tool.input_refs,evidence);
          const observation=element('section','execution-process-node observation');
          observation.append(element('div','execution-link-label','→ returns · same call'),element('h4','',`Observation · ${label(tool.status)}`));
          excerpt(observation,tool.observation,300);if(tool.observation_truncated) observation.append(element('small','muted','Excerpt; open source for full result'));citation(observation,tool.observation_refs,evidence);
          pair.append(action,observation);steps.append(pair);
        }
        if(!tools.length) steps.append(element('p','muted','No tool invocation linked in this selection'));
        graph.append(steps);group.append(graph);
        if(shown>1) content.append(element('div','execution-order-link','↓ next recorded call group'));
        content.append(group);
      }
    };
    append();
    if(!rounds.length) content.append(element('p','muted','No calls in this selection'));
    if(shown<rounds.length) {const more=button(`Show more · ${rounds.length-shown} remaining`,()=>{more.remove();append();if(shown<rounds.length){more.textContent=`Show more · ${rounds.length-shown} remaining`;content.append(more);}});content.append(more);}
  };
  select.addEventListener('change',()=>{onScopeChange?.(select.value);render();});render();
}
