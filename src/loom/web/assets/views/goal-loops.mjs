import {renderActionFlow} from "./action-flow.mjs";
import {element} from "../markdown.mjs";
import {badge, cards, disclosure, label, number, panel, seconds, table} from "./dashboard.mjs";

import {renderExecutionProcess, renderExecutionTime} from "./execution-process.mjs";

import {renderGoalProcess, renderCallTimeline} from "./goal-process.mjs";

export const GOAL_SCHEMA = "loom.goal-loop-analysis.v1";
const button = (title, action) => {
  const node = element("button", "quiet", title); node.type = "button"; node.addEventListener("click", action); return node;
};
const amount = (cost, key) => {
  const value = cost?.[key];
  if(value?.eligible_count > 0 && value.measured_count === 0) return "Unknown";
  return value ? `${key.endsWith("_ms") ? seconds(value.known_sum) : number(value.known_sum)}${value.measured_count < value.eligible_count ? " + unknown" : ""}` : "—";
};
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type})), link = element("a");
  link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function evidenceLinks(root, refs, evidence) {
  for (const ref of [...new Map((refs || []).map(r => [JSON.stringify(r), r])).values()])
    root.append(button(`L${ref.line_number}${ref.field_path ? ` · ${ref.field_path}` : ""}`, () => evidence(ref)));
}
export function renderGoalSummary(root, data, explore) {
  const c = data.coverage, cost = data.cost_ledger.totals;
  const block = panel(root, "Goal-loop analysis", true);
  cards(block, [["Run", data.source.run_id], ["Evidence reviewed", `${c.reviewed_packets} / ${c.source_packets}`],
    ["Calls attributed", `${c.assigned_calls} / ${c.eligible_calls}`], ["Known tokens", amount(cost, "total_tokens")]]);
  block.append(badge(c.status === "complete" ? "Pipeline complete" : label(c.status)), element("p", "muted", `Structure ${c.structure_complete ? "reviewed" : "partial"} · Reconciliation ${c.reconciliation_complete ? "saved" : "pending"} · ${c.diagnosed_goals.length} goals diagnosed`));
  if (c.execution_protocol >= 2) cards(block, [["Protocol", `v${c.execution_protocol}`], ["Requests split", c.split_requests || 0], ["Review gaps", c.review_gaps?.length || 0]]);
  if (explore) block.append(button("Open goal process", explore));
  for (const limitation of c.limitations || []) block.append(element("p", "muted", limitation));
}

export class GoalLoopView {
  constructor(root, {evidence}) { this.root = root; this.evidence = evidence; this.limit = 100; this.phaseLimit = 12; this.expanded = new Set(); this.expandedActions = new Set(); }
  update(data) {
    if(this.data && (this.data.analysis_id !== data.analysis_id || this.data.source?.run_id !== data.source?.run_id)) {
      this.expandedActions.clear(); this.selectedActionStage=null;
    }
    if(this.data?.analysis_id && this.data.analysis_id !== data.analysis_id) {this.inspectedId=null;this.highlight=null;}
    if(!this.data) for(const g of data.goals) if(g.parent_goal_id) this.expanded.add(g.id);
    this.data = data;
    const alias = data.aliases || {};
    const seen = new Set();
    while (alias[this.selected] && !seen.has(this.selected)) {seen.add(this.selected); this.selected = alias[this.selected];}
    if (!data.goals.some(g => g.id === this.selected)) this.selected = data.goals.find(g => !g.parent_goal_id)?.id;
    this.render();
  }
  render() {
    const data = this.data, c = data.coverage;
    const viewport = this.root.closest(".trajectory-page") || this.root.parentElement;
    const scrollTop = viewport?.scrollTop;
    const opened = new Set([...this.root.querySelectorAll("details[open] > summary")].map(n=>n.textContent));
    this.root.replaceChildren();
    if(data.execution_view?.action_flow && this.mode !== 'detailed') {
      renderActionFlow(this.root,data,{evidence:this.evidence,selected:this.selectedActionStage,expandedActions:this.expandedActions,
        select:id=>{this.selectedActionStage=id;this.render();},advanced:()=>{this.mode='detailed';this.render();}});
      if(viewport && scrollTop != null) viewport.scrollTop=scrollTop;
      return;
    }
    const header = element("div", "dashboard-toolbar");
    header.append(element("strong", "", "Goal & execution process"), badge(c.status === "complete" ? "Pipeline complete" : label(c.status)),
      button("Export JSON", () => download("goal-loop-analysis.json", JSON.stringify(data, null, 2), "application/json")),
      button("Export static view", () => {
        const clone = this.root.cloneNode(true); clone.querySelectorAll("button").forEach(b => {const span = element("span", "", b.textContent); b.replaceWith(span);});
        download("goal-loop-analysis.html", `<!doctype html><meta charset="utf-8"><title>Goal-loop analysis</title><style>
body{font:14px/1.5 system-ui;color:#17212b;background:white;max-width:1400px;margin:24px auto;padding:16px}
svg{width:100%;height:auto}svg rect{fill:#f7f9fc;stroke:#7b8797}svg text{fill:#17212b;font-size:12px}svg path{fill:none;stroke:#536575}
svg marker path{fill:#536575}.semantic path{stroke:#08786d;stroke-dasharray:5 4}.dashboard-panel{border:1px solid #cad1dc;padding:16px;margin:12px 0}
.dashboard-stats{display:flex;flex-wrap:wrap;gap:24px}.dashboard-stat{display:grid}.dashboard-stat strong{font-size:24px}
.goal-loop-layout{display:grid;grid-template-columns:25% 1fr;gap:16px}table{width:100%;border-collapse:collapse}td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd}
.goal-tree-row{margin:12px 0}details{margin:12px 0}.goal-timeline-row{display:grid;grid-template-columns:30% 1fr}.goal-timeline-bar{display:block;height:12px;background:#08786d}

.execution-time-bar{display:flex;height:20px;border-radius:6px;overflow:hidden}.execution-time-part.model,.execution-duration-fill.model{background:#557bd1}.execution-time-part.tool,.execution-duration-fill.tool{background:#2b9d8b}.execution-time-part.overlap{background:#9276bf}.execution-time-part.gap{background:#a6aab2}
.execution-time-legend,.execution-controls{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.execution-round-graph{display:grid;grid-template-columns:1fr 2fr;gap:12px;padding:12px}.execution-tool-pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}.execution-tool-steps{display:grid;gap:12px}
.execution-process-node{min-width:0;padding:10px;border:1px solid #cad1dc;border-top:3px solid #557bd1;border-radius:6px}.execution-process-node.tool{border-top-color:#2b9d8b}.execution-process-node.observation{border-top-color:#9276bf}.execution-excerpt{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.execution-duration-track{height:5px;background:#ddd}.execution-duration-fill{display:block;height:100%}.execution-cycle{border:1px solid #cad1dc;border-radius:8px}.execution-cycle>summary{padding:12px}.execution-cycle-title,.execution-cycle-cost{margin-left:12px}.execution-order-link{margin:10px}.goal-phase-time-bar{fill:#557bd1}
</style><p>Source ${data.source.digest} · ${data.coverage.status} · ${data.analyzer?.version || GOAL_SCHEMA} · UTC · Summary, not a complete evidence package</p>${clone.outerHTML}`, "text/html");
      }));
    if(data.execution_view?.action_flow) header.prepend(button('Action flow',()=>{this.mode='flow';this.render();}));
    this.root.append(header);
    cards(this.root, [["Source events", number(c.source_events)], ["Reviewed evidence", `${c.reviewed_packets} / ${c.source_packets}`],
      ["Calls attributed", `${c.assigned_calls} / ${c.eligible_calls}`], ["Diagnosed goals", number(c.diagnosed_goals.length)]]);
    const execution=data.execution_view;
    const runTime=execution?panel(this.root,"Run time · all recorded calls",true):null;
    const layout = element("div", "goal-loop-layout"), tree = element("aside", "dashboard-panel goal-loop-tree"), body = element("div", "goal-loop-main");
    const search = element("input"); search.type = "search"; search.placeholder = "Search goals"; search.setAttribute("aria-label", "Search goals"); search.value = this.search || "";
    const sort = element("select"); sort.setAttribute("aria-label", "Goal order");
    for (const [value, title] of [["tree", "Goal hierarchy"], ["cost", "Highest token cost"]]) {const opt = element("option", "", title); opt.value = value; sort.append(opt);}
    sort.value = this.sort || "tree";
    const list = element("nav"); list.setAttribute("aria-label", "Goal tree");
    tree.append(search, sort, list); layout.append(tree, body); this.root.append(layout);
    const goals = data.goals.filter(g => !g.superseded_by), byId = new Map(goals.map(g => [g.id, g]));
    const depth = g => {let n=0, p=g.parent_goal_id; const seen=new Set(); while (byId.has(p) && !seen.has(p)) {seen.add(p); n++; p=byId.get(p).parent_goal_id;} return n;};
    const children = new Map(); for (const g of goals) {const key = g.parent_goal_id || ""; children.set(key, [...(children.get(key)||[]), g]);}
    const ordered = [], walk = [...(children.get("") || [])].reverse();
    while (walk.length) {const g=walk.pop(); ordered.push(g); if (!this.expanded.has(g.id)) walk.push(...[...(children.get(g.id)||[])].reverse());}
    const renderTree = () => {
      list.replaceChildren();
      let rows = this.search ? goals.filter(g => g.objective.toLowerCase().includes(this.search.toLowerCase())) : ordered;
      if (this.sort === "cost") rows = [...rows].sort((a,b) => data.cost_ledger.by_owner[b.id].inclusive.total_tokens.known_sum-data.cost_ledger.by_owner[a.id].inclusive.total_tokens.known_sum);
      for (const g of rows) {
        const row=element("div", "goal-tree-row"); row.style.paddingInlineStart=`${Math.min(8,depth(g))*12}px`;
        if (children.has(g.id)) row.append(button(this.expanded.has(g.id)?"▸":"▾", () => {this.expanded.has(g.id)?this.expanded.delete(g.id):this.expanded.add(g.id); this.render();}));
        const pick=button(g.objective, () => {this.selected=g.id; this.limit=100; this.phaseLimit=12; this.render();}); pick.setAttribute("aria-current", String(g.id===this.selected));
        row.append(pick, element("small", "muted", amount(data.cost_ledger.by_owner[g.id].inclusive,"total_tokens"))); list.append(row);
      }
    };
    search.addEventListener("input", () => {this.search=search.value; renderTree();});
    sort.addEventListener("change", () => {this.sort=sort.value; renderTree();}); renderTree();
    const goal=byId.get(this.selected); if (!goal) return;
    const selected = new Set([goal.id]); let changed=true;
    while (changed) {changed=false; for (const g of goals) if (selected.has(g.parent_goal_id) && !selected.has(g.id)) {selected.add(g.id);changed=true;}}
    const attempts=data.attempts.filter(a=>a.goal_id===goal.id && !a.superseded_by), aids=new Set(attempts.map(a=>a.id));
    const phases=data.phases.filter(p=>aids.has(p.attempt_id)).sort((a,b)=>a.start_line-b.start_line);
    const costs=data.cost_ledger.by_owner[goal.id], summary=panel(body,goal.objective,true);
    summary.append(badge(label(goal.goal_disposition)),badge(`Execution: ${label(goal.execution_state)}`),badge(`Completion claim: ${label(goal.completion_claim)}`));
    cards(summary, [["Attempts",number(attempts.length)],["Phases",number(phases.length)],["Inclusive tokens",amount(costs.inclusive,"total_tokens")],
      ["Attributed model time",amount(costs.inclusive,"model_duration_ms")],["Attributed tool time",amount(costs.inclusive,"tool_duration_ms")]]);
    const accounting=disclosure(summary,"Exclusive / inclusive accounting");
    table(accounting,["Cost","Exclusive","Inclusive"],[...['prompt_tokens','completion_tokens','total_tokens','model_duration_ms','tool_duration_ms'].map(k=>[label(k),amount(costs.exclusive,k),amount(costs.inclusive,k)])]);
    if(goal.conditions?.length) {const conditions=disclosure(summary,"Completion conditions"); for(const condition of goal.conditions) conditions.append(element("p","",typeof condition === "string" ? condition : condition.description || JSON.stringify(condition)));}
    evidenceLinks(summary,goal.evidence_refs,this.evidence);
    const detail=panel(body,"Selected phase / activity",true); detail.hidden=true;
    const inspect = (p, initialScope) => {
      if(this.inspectedId!==p.id) this.detailScope=null;
      this.inspectedId=p.id; this.detailScope=initialScope||this.detailScope; detail.hidden=false;
      detail.replaceChildren(element("h3","",label(p.kind || "diagnostic")));
      const phaseScope=execution?.phases[p.id];
      if(execution && (phaseScope || p.kind==='execution')) {
        const flow=element('section','execution-process-detail');detail.append(flow);
        renderExecutionProcess(flow,execution,{phaseId:phaseScope?p.id:null,initialScope:this.detailScope,evidence:this.evidence,onScopeChange:scope=>{this.detailScope=scope;}});
      }
      const textPane=phaseScope?disclosure(detail,"Semantic assessment · saved interpretation"):detail;
      for (const key of ['question','known_before','actions','observations','delta','remaining_questions','next_reason','description','rationale','observation','interpretation','alternative','improvement'])
        if(p[key]) {textPane.append(element("strong","",label(key)),element("p","",p[key]));}
      evidenceLinks(textPane,p.evidence_refs,this.evidence);
      if(p.call_ids?.length) {
        const calls=data.cost_ledger.atoms.filter(a=>p.call_ids.includes(a.id));
        table(disclosure(detail,"Linked call ledger"),["Call","Status","Duration","Tokens","Accounted owner"],calls.map(a=>[a.tool||a.model,label(a.status),seconds(a.duration_ms),a.total_tokens,
          data.cost_ledger.assignments.find(r=>r.cost_atom_id===a.id)?.owner]));
        detail.append(element("p","muted","Related calls may be shared; their values are not additional phase costs."));
      }
    };
    if(runTime) renderExecutionTime(runTime,execution,scope=>{inspect({id:'execution',kind:'execution'},scope);detail.scrollIntoView?.({block:'start',behavior:'smooth'});});
    const inspected=phases.find(p=>p.id===this.inspectedId); if(inspected) inspect(inspected);
    else if(execution && this.inspectedId==='execution') inspect({id:'execution',kind:'execution'});
    const process=panel(body,"Attempts & phase occurrences",true); body.insertBefore(process,detail);
    if(execution?.run.outside_phase_call_ids.length) {
      const gap=element('div','execution-unmapped');
      gap.append(element('strong','',`${execution.run.outside_phase_call_ids.length} calls outside the phase graph`),
        button('Explore unmapped execution',()=>{inspect({id:'execution',kind:'execution'},'outside');detail.scrollIntoView?.({block:'start',behavior:'smooth'});}));
      process.append(gap);
    }
    const childGoals=goals.filter(g=>g.parent_goal_id===goal.id);
    if(childGoals.length){const sub=disclosure(process,`Subgoals · ${childGoals.length}`); for(const g of childGoals) sub.append(button(g.objective,()=>{this.selected=g.id;this.render();}));}
    if(!phases.length) process.append(element("p","muted","No semantic phases established. Recorded evidence remains available below."));
    const relations=(data.graph?.relations?.filter(r=>r.layer==="semantic") || data.relations).filter(r=>phases.some(p=>p.id===r.source_id||p.id===r.target_id));
    renderGoalProcess(process,{goal,attempts,phases,relations,limit:this.phaseLimit,highlight:this.highlight,inspect,executionView:execution});
    if(attempts.some(a=>phases.filter(p=>p.attempt_id===a.id).length>this.phaseLimit))
      process.append(button("Expand more phases",()=>{this.phaseLimit+=12;this.render();}));
    const readable = id => {const node=[...data.goals,...data.attempts,...data.phases,...data.activities].find(r=>r.id===id);return node?.question||node?.objective||node?.approach||node?.description||id;};
    if(relations.length) table(disclosure(process,"Cited relationships"),["From","Relation","To","Basis"],relations.map(r=>[
      button(readable(r.source_id),()=>inspect(phases.find(p=>p.id===r.source_id)||r)),label(r.kind),readable(r.target_id),r.rationale]));
    const relatedCalls = new Set(data.cost_ledger.assignments.filter(a=>selected.has(a.owner)||a.related_ids.some(id=>phases.some(p=>p.id===id))).map(a=>a.cost_atom_id));
    const timing = disclosure(process,"Actual call timeline · UTC");
    renderCallTimeline(timing,data.cost_ledger.atoms.filter(a=>relatedCalls.has(a.id)),a=>{
      inspect({kind:a.kind,description:a.tool||a.model,evidence_refs:a.evidence_refs,call_ids:[a.id]});
    });
    const activities=data.activities.filter(a=>selected.has(a.goal_id));
    if(activities.length) table(disclosure(body,"Framework activity"),["Activity","Description","Source"],activities.map(a=>[label(a.kind),button(a.description,()=>inspect(a)),`L${a.start_line}–${a.end_line}`]));
    const diagnostics=panel(body,"Execution efficiency findings",true);
    for(const d of data.diagnostics.filter(d=>selected.has(d.goal_id))) diagnostics.append(button(d.observation,()=>{
      this.highlight=new Set(phases.filter(p=>p.call_ids.some(id=>d.call_ids.includes(id))).map(p=>p.id));this.render();
      const target=this.root.querySelector('.goal-loop-main');target?.querySelector('.highlighted')?.scrollIntoView?.({block:'nearest'});
      // Keep original evidence accessible even when a finding concerns a non-phase record.
      const pane=panel(target,"Finding detail",true);
      const involved=data.cost_ledger.atoms.filter(a=>d.call_ids.includes(a.id));
      cards(pane,[["Involved calls",number(involved.length)],["Known involved tokens",number(involved.reduce((n,a)=>n+(a.total_tokens||0),0))]]);
      pane.append(element('p','muted','Involved cost, not estimated savings. Missing usage remains unknown.'));  for(const k of ['observation','interpretation','alternative','improvement']) pane.append(element('p','',`${label(k)}: ${d[k]}`)); evidenceLinks(pane,d.evidence_refs,this.evidence);
    }));
    if(!data.diagnostics.some(d=>selected.has(d.goal_id))) diagnostics.append(element("p","muted","No cited finding in this review scope; this is not a claim of efficient execution."));
    const windows=Object.entries(c.diagnosis_windows||{}).filter(([id])=>selected.has(id));
    if(windows.length) table(disclosure(diagnostics,"Diagnostic evidence scope"),["Goal","Read packets","Available packets","Scope"],windows.map(([id,w])=>[readable(id),w.delivered_packet_ids.length,w.available_packets,w.available_packets===0?"No evidence":w.partial?"Partial window":"Cited window"]));
    const checks=disclosure(body,"Run verification · authoritative source records");
    for(const v of data.facts.verification){
      const result=v.value.result||v.value, state=result.state||result.status||v.value.status||"unknown";
      checks.append(element('p','',`${label(v.type)} · ${typeof state==='string'?label(state):label(state.state)} · L${v.line}`));
      for(const k of ['description','criterion_id','subject','verifier','reason','summary']) if(result[k]) checks.append(element('p','',`${label(k)}: ${typeof result[k]==='string'?result[k]:JSON.stringify(result[k])}`));
      evidenceLinks(checks,v.evidence_refs,this.evidence);
    }
    const timeline=disclosure(body,"Recorded timeline / raw evidence");
    table(timeline,["Line / seq","Event","Time","Calls"],data.facts.events.slice(0,this.limit).map(r=>[
      r.refs?.length?button(`L${r.line} / ${r.seq??'—'}`,()=>this.evidence(r.refs[0])):`L${r.line} / ${r.seq??'—'}`,r.type,r.at||'Unknown',r.call_ids.length]));
    if(data.facts.events.length>this.limit) timeline.append(button("Load more events",()=>{this.limit+=100;this.render();}));
    const unassigned=panel(body,"Accounting coverage",true);
    const quality=c.source_quality||{};
    table(unassigned,["Coverage","Recorded"],[['Calls fully reviewed',`${c.reviewed_calls} / ${c.eligible_calls}`],['Calls unassigned',c.unassigned_calls??c.eligible_calls-c.assigned_calls],
      ['Execution protocol',c.execution_protocol || 1],['Requests split',c.split_requests || 0],['Review gaps',c.review_gaps?.length || 0],['Unknown evidence pages',c.unknown_packets],['Pending evidence pages',c.pending_packets],['Calls without final usage',quality.missing_usage_calls],
      ['Calls without terminal event',quality.calls_without_terminal],['Missing artifacts',quality.missing_artifacts?.length]]);
    table(unassigned,["Bucket","Calls","Known tokens"],['analysis:shared','analysis:unassigned'].map(id=>[label(id),data.cost_ledger.by_owner[id].inclusive.model_calls+data.cost_ledger.by_owner[id].inclusive.tool_calls,amount(data.cost_ledger.by_owner[id].inclusive,'total_tokens')]));
    table(disclosure(body,"Validation / missing evidence"),["Stage","Field","Reason"],data.validation_diagnostics.map(d=>[d.stage,d.field,d.reason]));
    if(c.review_gaps?.length) table(disclosure(body,"Unreviewed work"),["Stage","Unit","Reason"],c.review_gaps.map(g=>[g.stage,g.unit,g.reason]));
    if(c.reconciliation_decisions?.length) table(disclosure(body,"Reconciliation decisions"),["Candidate","Decision","Reason"],c.reconciliation_decisions.map(d=>[d.candidate_id,label(d.resolution),d.rationale]));
    for(const limitation of c.limitations||[]) body.append(element('p','muted',limitation));
    for(const node of this.root.querySelectorAll("details")) if(opened.has(node.querySelector("summary")?.textContent)) node.open=true;
    if(viewport && scrollTop != null) viewport.scrollTop=scrollTop;
  }
}
