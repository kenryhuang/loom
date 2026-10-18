import {element} from '../markdown.mjs';
import {badge, cards, label} from './dashboard.mjs';

export function utilityLabel(row) {
  if (!row || row.status !== 'reviewed') return badge('Not reviewed');
  const node=element('span','action-utility-label');
  node.append(badge(label(row.value)),element('small','muted',label(row.novelty)));
  return node;
}

export function utilitySummary(root, data) {
  if(!data) return;
  const c=data.coverage;
  cards(root,[['Action utility reviewed',`${c.reviewed} / ${c.eligible}`],
    ['Material / decisive', (data.value_counts.material||0)+(data.value_counts.decisive||0)],
    ['Avoidable · inferred',data.rows.filter(r=>r.status==='reviewed' && r.necessity==='avoidable').length],
    ['Insufficient evidence',c.unknown]]);
}

export function utilityDetails(root, row, evidence) {
  const pane=element('section','action-utility-detail');root.append(pane);
  pane.append(element('h4','','Action utility'));
  if(!row || row.status!=='reviewed') {
    pane.append(element('p','muted','Not reviewed. Run the current Deep evaluation to assess action utility; partial evaluations retain completed assessments.'));
    return;
  }
  cards(pane,[['Necessity',label(row.necessity)],['Contribution',label(row.value)],['Increment',label(row.novelty)],['Confidence',label(row.confidence)]]);
  pane.append(badge(label(row.role)));
  if(row.evidence_window?.partial) pane.append(element('p','muted','Partial evidence window · judgments are limited to the delivered evidence.'));
  const facts=row.facts;
  if(facts?.prior_same_input_ids?.length) pane.append(element('p','muted',
    `${facts.prior_same_input_ids.length} earlier calls with identical inputs · Recorded output ${facts.same_recorded_output===true?'identical':facts.same_recorded_output===false?'different':'unavailable'}`));
  for(const [key,title] of [['delta','What changed'],['reason','Contribution to the result'],['counterfactual','If omitted · inferred'],['improvement','Improvement / validation']]) {
    if(row[key]) pane.append(element('strong','',title),element('p','',row[key]));
  }
  for(const [key,title] of [['evidence_refs','Action evidence'],['contribution_refs','Result / downstream use'],['comparison_refs','Earlier comparison'],['acceptance_refs','Acceptance / verification']]) {
    if(!row[key]?.length) continue;
    const refs=element('div','action-utility-refs');refs.append(element('strong','',title));
    for(const ref of row[key]) {
      const b=element('button','quiet',`L${ref.line_number}${ref.field_path?` · ${ref.field_path}`:''}`);
      b.type='button';b.addEventListener('click',()=>evidence(ref));refs.append(b);
    }
    pane.append(refs);
  }
  pane.append(element('p','muted','Evidence-linked interpretation, not proven causality. Related action time is not an estimate of savings.'));
}
