import {element, renderMarkdown} from '../markdown.mjs';
import {disclosure, table, seconds, number, badge} from './dashboard.mjs';
import {goalTree, filterGoalTree, goalEntries, callSummary} from './goal-tree.mjs';
import {renderTraceMemory} from './trace-memory.mjs';

let viewSequence = 0;
const button = (text, action) => {
  const node = element('button', 'quiet', text); node.type = 'button'; node.addEventListener('click', action); return node;
};
const conditionText = c => typeof c === 'string' ? c : c.description || c.id || JSON.stringify(c);
const unique = refs => [...new Map(refs.filter(Boolean).map(r => [JSON.stringify(r), r])).values()];
export function durationLabel(cost) {
  if (!cost.calls) return '—';
  if (!cost.timed) return `Unknown (${cost.calls} ${cost.calls === 1 ? 'call' : 'calls'})`;
  const measured = cost.duration === 0 ? '0 ms' : seconds(cost.duration);
  return measured + (cost.unknown ? ` + ${cost.unknown} unknown` : '');
}
const basisLabel = {assessed_stage: 'Assessed stage', inferred_context: 'Default rule', default_rule: 'Default rule', recorded: 'Recorded', framework_rule: 'Framework required',
  operation_grouping: 'Display grouping', unassigned: 'Unassigned'};

export class IncrementalGraphView {
  constructor(root, {evidence = () => {}} = {}) {
    Object.assign(this, {root, evidence, mode: 'skeleton', filter: 'all', opened: new Map(), uid: ++viewSequence});
  }
  update(data, identity = '') {
    const key = JSON.stringify([identity || data.analysis_id, data.source?.digest, data.source?.run_id]);
    if (this.identity !== key) {this.opened.clear(); this.filter = 'all'; this.memoryGoal = 'all'; this.root.replaceChildren();}
    this.identity = key; this.data = data; this.tree = goalTree(data);
    if (!['all', 'unassigned'].includes(this.filter) && !this.tree.byGoal.has(this.filter)) this.filter = 'all';
    this.render();
  }
  refs(root, refs = [], label = '') {
    const actions = element('div', 'action-utility-refs');
    for (const ref of unique(refs)) {
      const event = this.data.facts?.events?.find(e => e.line === ref.line_number);
      const location = event?.seq != null ? `E${event.seq}` : `L${ref.line_number}`;
      const range = ref.start != null && ref.end != null ? ` · ${ref.start}–${ref.end}` : '';
      actions.append(button(`${label ? label + ' · ' : ''}${location}${range}${ref.field_path ? ' · ' + ref.field_path : ''}`, () => this.evidence(ref)));
    }
    if (actions.childNodes.length) root.append(actions);
  }
  details(root, key, title, render, initial = false, className = '') {
    const node = element('details', `goal-tree-disclosure ${this.mode === 'skeleton' ? 'setup-block' : ''} ${className}`), summary = element('summary');
    node.dataset.key = key; summary.append(typeof title === 'string' ? document.createTextNode(title) : title);
    const body = element('div', 'goal-tree-content'); node.append(summary, body); root.append(node);
    let rendered = false;
    const fill = () => {if (!rendered) {rendered = true; render(body);}};
    node.open = this.opened.has(key) ? this.opened.get(key) : initial;
    if (node.open) fill();
    node.addEventListener('toggle', () => {if (!node.isConnected) return; this.opened.set(key, node.open); if (node.open) fill();});
    return node;
  }
  render() {
    const viewport = this.root.closest('.trajectory-page') || this.root;
    const scroll = viewport.scrollTop;
    for (const node of this.root.querySelectorAll('details[data-key]')) this.opened.set(node.dataset.key, node.open);
    this.root.replaceChildren(); this.root.classList.add('incremental-graph');
    const header = element('div', 'dashboard-toolbar skeleton-toolbar'), tabs = element('div', 'trajectory-tabs');
    tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Analysis result');
    const modes = ['skeleton', 'acceptance', 'memory', 'report'];
    for (const mode of modes) {
      const tab = button(mode[0].toUpperCase() + mode.slice(1), () => {this.mode = mode; this.render();});
      tab.id = `skeleton-tab-${this.uid}-${mode}`; tab.tabIndex = this.mode === mode ? 0 : -1;
      tab.setAttribute('aria-controls', `skeleton-panel-${this.uid}`);
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(this.mode === mode));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); const i = modes.indexOf(this.mode);
        this.mode = event.key === 'Home' ? modes[0] : event.key === 'End' ? modes.at(-1) : modes[(i + (event.key === 'ArrowLeft' ? modes.length - 1 : 1)) % modes.length];
        this.render(); this.root.querySelector('[role=tab][aria-selected=true]')?.focus();
      });
      tabs.append(tab);
    }
    header.append(tabs); this.root.append(header);
    const content = element('section', 'skeleton-tab-panel'); content.setAttribute('role', 'tabpanel');
    content.id = `skeleton-panel-${this.uid}`; content.setAttribute('aria-labelledby', `skeleton-tab-${this.uid}-${this.mode}`);
    this.root.append(content);
    if (this.mode === 'report') this.renderReport(content);
    else if (this.mode === 'memory') renderTraceMemory(this, content);
    else if (this.mode === 'acceptance') this.renderAcceptance(content);
    else this.renderSkeleton(content);
    // Report disclosures are eager; restore their state as well.
    for (const node of this.root.querySelectorAll('details[data-key]')) if (this.opened.has(node.dataset.key)) node.open = this.opened.get(node.dataset.key);
    viewport.scrollTop = scroll;
  }
  metrics(cost, parentCost, parentLabel) {
    const node = element('span', 'goal-tree-metrics');
    const partial = !!(cost.unknown || parentCost.unknown);
    const ratio = cost.timed && parentCost.duration > 0 ? cost.duration / parentCost.duration : null;
    const percent = ratio === null ? '—' : ratio > 0 && ratio < .001 ? '<0.1%' : `${(ratio * 100).toFixed(1).replace(/\.0$/, '')}%`;
    const share = `${percent} of ${parentLabel}${partial ? ' · known time only' : ''}`;
    const values = element('span', 'goal-tree-time-values');
    values.append(element('span', 'goal-tree-duration', durationLabel(cost)), element('span', 'goal-tree-share', share));
    const track = element('span', `goal-tree-time-track${ratio === null ? ' is-unknown' : ''}${partial ? ' is-partial' : ''}`);
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `Duration: ${durationLabel(cost)}; ${share}`);
    if (ratio !== null) {
      const fill = element('span', 'goal-tree-time-fill');
      fill.style.width = `${Math.max(0, Math.min(100, ratio * 100))}%`; track.append(fill);
    }
    node.append(values, track, element('span', 'goal-tree-call-count', `${cost.model} model / ${cost.tool} tool`));
    node.title = `Duration: ${durationLabel(cost)}. Compared with ${parentLabel}: ${durationLabel(parentCost)}. Sum of recorded durations for unique calls, not wall time. Shared calls cannot be summed across branches. Parent totals stay unchanged when filtering. A dash means no measurable percentage.`;
    return node;
  }
  heading(label, title, cost, tags, parentCost, parentLabel) {
    const row = element('span', 'goal-tree-heading');
    const name = element('span', 'goal-tree-title');
    if (label) name.append(element('span', 'goal-tree-id', label));
    name.append(document.createTextNode(title)); row.append(name);
    const statuses = element('span', 'goal-tree-tags');
    for (const tag of tags.filter(Boolean)) statuses.append(badge(tag));
    row.append(statuses, this.metrics(cost, parentCost, parentLabel)); return row;
  }
  renderSkeleton(root) {
    const controls = element('div', 'goal-tree-controls'), label = element('label', '', 'Goal');
    const select = element('select'); select.setAttribute('aria-label', 'Filter goals');
    for (const [value, title] of [['all', 'All goals'], ...this.tree.goals.filter(g => g.parent_goal_id).map(g => [g.id, `${g.display_id} · ${g.objective}`]), ['unassigned', 'Unreviewed / unassigned']]) {
      const option = element('option', '', title); option.value = value; select.append(option);
    }
    select.value = this.filter; select.addEventListener('change', () => {this.filter = select.value; this.render();});
    label.append(select); controls.append(label); root.append(controls);
    root.append(element('p', 'goal-tree-time-legend', 'Time bars: goal / parent goal · stage / goal · action / stage. Recorded call time, not wall time; shared calls count once per branch. Filters retain full parent totals.'));
    const pending = this.data.coverage.pending_structure_units?.length || 0;
    if (pending) controls.append(badge(`Goal structure pending · ${pending} windows`));
    const tree = element('div', 'goal-process-tree'); tree.setAttribute('aria-label', 'Goal process tree'); root.append(tree);
    const roots = filterGoalTree(this.tree, this.filter);
    for (const goal of roots) this.renderGoal(tree, goal);
    if (!roots.length) tree.append(element('p', 'muted', 'No actions match this filter.'));
  }
  hostState(goal) {
    if (goal.execution_state && goal.execution_state !== 'unknown') return goal.execution_state;
    const events = (this.data.facts?.events || []).filter(e => e.line >= (goal.origin_line ?? 0) && e.line <= (goal.end_line ?? Infinity));
    const terminal = events.filter(e => ['run.completed', 'run.failed', 'run.stopped'].includes(e.type)).at(-1);
    return terminal ? terminal.type.slice(4) : 'Unknown';
  }
  renderGoal(root, goal) {
    const closure = this.data.closures?.[goal.id];
    const status = closure?.disposition || goal.goal_disposition || 'unknown';
    const tags = goal.parent_goal_id ? [status] : [`Process: ${status}`, `Host: ${this.hostState(goal)}`];
    if (Number.isFinite(goal.first_action_line)) tags.push(`L${goal.first_action_line}–${goal.last_action_line}`);
    if (this.tree.byGoal.get(goal.parent_goal_id)?.parent_goal_id && goal.parent_relation_basis === 'unreviewed') tags.push('Nesting unreviewed');
    const parent = this.tree.byGoal.get(goal.parent_goal_id);
    const parentCost = parent?.cost || callSummary([...this.tree.atoms.keys()], this.tree.atoms);
    const node = this.details(root, `goal:${goal.id}`, this.heading(goal.display_id, goal.objective, goal.cost, tags,
      parentCost, parent?.display_id || 'run'), body => {
      this.details(body, `goal-info:${goal.id}`, goal.parent_goal_id ? 'Goal details' : 'Task requirements', detail => {
        if (goal.parent_goal_id) {
          const parent = this.tree.byGoal.get(goal.parent_goal_id);
          table(detail, ['Parent goal', 'Decomposition basis', 'Judgment'], [[
            `${parent?.display_id || ''} · ${parent?.objective || goal.parent_goal_id}`,
            goal.root_relation || 'Not recorded; saved nesting is unreviewed',
            goal.parent_relation_basis === 'inferred' ? 'Inferred from cited intent' : 'Unreviewed',
          ]]);
        }
        if (goal.conditions?.length) table(detail, ['Condition', 'Required'], goal.conditions.map(c => [conditionText(c), typeof c === 'object' ? c.required ?? 'Unknown' : 'Unknown']));
        if (closure) {
          table(detail, ['Completion claim', 'Closure evidence', 'Disposition'], [[closure.claim, closure.evidence_state, closure.disposition]]);
          if (closure.reason) detail.append(element('p', '', closure.reason));
          this.refs(detail, closure.evidence_refs);
        } else detail.append(element('p', 'muted', 'Closure not established'));
        for (const q of closure?.open_questions || goal.open_questions || []) detail.append(element('p', '', `Remaining question / feedback: ${q}`));
        this.refs(detail, goal.evidence_refs);
        for (const item of goal.requirements) this.renderAction(detail, item, `requirements:${goal.id}`, this.tree.byGoal.get(goal.id).cost, goal.display_id);
        this.findings(detail, goal.id);
      });
      const children = element('div', 'goal-tree-children'); body.append(children);
      for (const entry of goalEntries(goal)) {
        if (entry.kind === 'goal') this.renderGoal(children, entry.value);
        else this.renderStage(children, entry.value);
      }
    }, true, 'goal-tree-goal');
    node.dataset.goalId = goal.id;
  }
  renderStage(root, stage) {
    const ids = [...new Set(stage.actions.flatMap(a => a.card.call_ids))], cost = callSummary(ids, this.tree.atoms);
    const tags = [basisLabel[stage.basis], `L${stage.first_line}–${stage.last_line}`];
    if (stage.visits?.length > 1) tags.push(`${stage.visits.length} visits`);
    const node = this.details(root, `stage:${stage.id}`, this.heading(stage.display_id, stage.title, cost,
      tags, this.tree.byGoal.get(stage.goal_id).cost, this.tree.byGoal.get(stage.goal_id).display_id), body => {
      const models = [...new Set(ids.map(id => this.tree.atoms.get(id)).filter(a => a?.kind === 'model').map(a => a.model || 'Unknown'))];
      const meta = element('div', 'goal-tree-stage-meta', `L${stage.first_line}–${stage.last_line}${models.length ? ' · Models: ' + models.join(', ') : ''}`); body.append(meta);
      this.details(body, `stage-info:${stage.id}`, 'Stage details & dependencies', detail => {
        if (stage.visits?.length > 1) table(detail, ['Visit', 'Action range'], stage.visits.map((visit, i) =>
          [i + 1, `L${visit.first_line}–${visit.last_line}`]));
        const changes = [...new Set(stage.actions.map(a => a.assessment?.delta).filter(Boolean))];
        for (const change of changes) detail.append(element('p', '', change));
        if (!changes.length) detail.append(element('p', 'muted', 'Progress not assessed'));
        const attempts = (this.data.attempts || []).filter(a => stage.attempt_ids.includes(a.id) && a.basis !== 'unknown');
        if (attempts.length) table(detail, ['Approach', 'Outcome'], attempts.map(a => [a.approach, a.outcome]));
        const nodes = this.data.skeleton?.nodes || [], edges = this.data.skeleton?.edges || [];
        const members = new Set(nodes.filter(n => n.stage_id === stage.source_stage_id && n.goal_id === stage.goal_id).map(n => n.id));
        const related = edges.filter(e => members.has(e.source_id) || members.has(e.target_id));
        if (related.length) table(detail, ['Dependency', 'Result / feedback'], related.map(e => {
          const incoming = members.has(e.target_id), other = nodes.find(n => n.id === (incoming ? e.source_id : e.target_id));
          const target = this.tree.stages.find(s => s.source_stage_id === other?.stage_id && s.goal_id === other?.goal_id);
          const value = element('div', '', e.result || e.kind); this.refs(value, e.evidence_refs);
          return [`${incoming ? 'From' : 'To'} ${target?.display_id || ''} · ${other?.objective || 'Unknown stage'}`, value];
        }));
        this.refs(detail, stage.evidence_refs);
        this.findings(detail, stage.goal_id, stage.card_ids);
      });
      const actions = element('div', 'goal-tree-actions'); body.append(actions);
      const fullStage = this.tree.stages.find(s => s.id === stage.id);
      for (const item of stage.actions) this.renderAction(actions, item, stage.id, fullStage.cost, stage.display_id || 'stage');
    }, false, 'goal-tree-stage');
    node.dataset.stageId = stage.source_stage_id;
  }
  featureRows(root, values) {
    const walk = (value, name = '') => {
      if (!value || typeof value !== 'object') return;
      if ('text' in value) {
        const row = element('div', 'goal-tree-feature');
        if (name) row.append(element('strong', '', name));
        row.append(element('pre', 'goal-report', String(value.text)));
        if (value.omitted_chars) row.append(element('span', 'muted', `${number(value.omitted_chars)} characters not shown`));
        this.refs(row, [this.data.extraction.registry[value.ref]?.ref]); root.append(row);
      } else for (const [key, child] of Object.entries(value)) walk(child, name ? `${name}.${key}` : key);
    };
    walk(values);
  }
  renderAction(root, item, context, parentCost, parentLabel) {
    const {card, assessment, attribution, shared, root_override, framework_rule} = item;
    const cost = callSummary(card.call_ids, this.tree.atoms);
    const heading = this.heading(`A${item.ordinal || this.tree.cards.findIndex(c => c.id === card.id) + 1}`, `${card.kind} · ${card.name} · L${card.start_line}–${card.end_line}`, cost,
      [card.status, shared ? 'Shared reference' : '', framework_rule ? 'Framework required' : attribution ? 'Default rule' : ''], parentCost, parentLabel);
    const node = this.details(root, `action:${context}:${card.id}`, heading, body => {
      body.dataset.actionDetail = card.id;
      body.append(element('p', 'goal-tree-stage-meta', `L${card.start_line}–${card.end_line} · Review: ${assessment?.status || 'Pending'}`));
      if (framework_rule) body.append(element('p', 'goal-tree-stage-meta', 'Root Goal association · required by the framework'));
      if (root_override) body.append(element('p', 'notice', 'Framework ownership takes precedence over the saved subgoal attribution.'));
      if (assessment) {
        table(body, ['Action intent', 'Observed progress'], [[assessment.purpose || 'Unknown', assessment.delta || 'Unknown']]);
        // Older reports may contain these evidence-backed fields. Metadata-only
        // reviews do not produce them; do not show permanent empty columns.
        if (assessment.expected?.trim() && assessment.expected.toLowerCase() !== 'unknown' && assessment.expected_evidence_refs?.length) {
          const expected = element('section'); body.append(expected);
          table(expected, ['Expected result'], [[assessment.expected]]); this.refs(expected, assessment.expected_evidence_refs);
        }
        if (assessment.uptake_card_ids?.length) {
          table(body, ['Adopted by'], assessment.uptake_card_ids.map(id => {
            const target = this.tree.cards.find(c => c.id === id);
            return [target ? `A${this.tree.cards.indexOf(target) + 1} · ${target.kind} · ${target.name} · L${target.start_line}–${target.end_line}` : id];
          }));
        }
      } else body.append(element('p', 'muted', 'Action review pending'));
      if (assessment) this.details(body, `review:${context}:${card.id}`, 'Review evidence', detail => {
        table(detail, ['Assessment', 'Finding'], ['purpose', 'contribution', 'necessity', 'redundancy'].map(k => [k, assessment[k]]));
        this.refs(detail, [...(assessment.evidence_refs || []), ...(assessment.expected_evidence_refs || [])]);
      });
      const calls = card.call_ids.map(id => this.tree.atoms.get(id)).filter(Boolean);
      if (calls.length) table(body, ['Model / tool', 'Duration', 'TTFT', 'TPS', 'Tokens in / out'], calls.map(a => [a.tool || a.model || a.id,
        a.duration_ms == null ? 'Unknown' : a.duration_ms === 0 ? '0 ms' : seconds(a.duration_ms), seconds(a.ttft_ms), number(a.tps), `${number(a.prompt_tokens)} / ${number(a.completion_tokens)}`]));
      if (card.kind === 'tool') {
        for (const [key, label, prefix] of [['arguments', 'Tool arguments', /^(input|arguments)/], ['result', 'Tool result', /^(output|result)/]]) {
          const section = element('section', 'goal-tree-record'); section.append(element('h4', '', label)); body.append(section);
          this.featureRows(section, card.features?.[key]);
          if (key === 'arguments' && card.features?.command) this.featureRows(section, {command: card.features.command});
          const refs = (card.raw_refs || []).map(id => this.data.extraction.registry[id]?.ref).filter(r => r && prefix.test(r.field_path || ''));
          this.refs(section, refs, 'Original record');
        }
      } else {
        this.featureRows(body, card.features);
        if (card.excerpts?.length) {
          const response = element('section', 'goal-tree-record'); response.append(element('h4', '', 'Recorded response')); body.append(response);
          for (const excerpt of card.excerpts) this.featureRows(response, excerpt);
        }
      }
      this.details(body, `original:${context}:${card.id}`, 'This action’s original records', detail => {
        const events = (this.data.facts?.events || []).filter(e => e.call_ids?.some(id => card.call_ids.includes(id)) || e.line === card.start_line || e.line === card.end_line);
        this.refs(detail, [...calls.flatMap(a => a.evidence_refs || []), ...events.map(e => e.source_ref),
          ...(card.raw_refs || []).map(id => this.data.extraction.registry[id]?.ref)]);
      });
      this.details(body, `visible:${context}:${card.id}`, 'Model-visible input', detail => {
        if (!card.presented?.length) detail.append(element('p', 'muted', 'No delivery record available'));
        for (const delivery of card.presented || []) {
          const ref = this.data.extraction.registry[delivery.ref]?.ref || delivery.ref;
          detail.append(element('p', '', `${delivery.mode === 'full' ? 'Same captured result presented' : 'Preview or transformed result presented'}${delivery.line ? ' · L' + delivery.line : ''}${delivery.chars != null ? ' · ' + delivery.chars + ' characters' : ''}`));
          this.refs(detail, typeof ref === 'object' ? [ref] : []);
        }
      });
      if (attribution) this.details(body, `attribution:${context}:${card.id}`, 'Attribution rule · no factual evidence', detail => {
        detail.append(element('p', 'muted', `Default rule: ${attribution.rule || 'previous_action_same_scope'}. ${attribution.card_id ? 'Previous action: ' + attribution.card_id + '. ' : ''}This is sequential tree placement, not evidence that the action serves this goal. Utility and completion remain separately assessed.`));
        this.refs(detail, attribution.refs);
      });
    }, false, 'goal-tree-action');
    node.dataset.cardId = card.id;
  }
  findings(root, goalId, cardIds = null) {
    const rows = (this.data.diagnostics || []).filter(d => d.goal_id === goalId && (!cardIds || d.card_ids?.some(id => cardIds.includes(id))));
    if (!rows.length) return;
    this.details(root, `findings:${goalId}:${cardIds?.join(',') || ''}`, `Findings · ${rows.length}`, detail => {
      for (const row of rows) {detail.append(element('strong', '', row.observation)); for (const key of ['interpretation', 'alternative', 'improvement']) detail.append(element('p', '', `${key}: ${row[key]}`)); this.refs(detail, row.evidence_refs);}
    });
  }
  renderAcceptance(root) {
    const section = element('div', 'goal-acceptance'); root.append(section);
    for (const goal of this.tree.roots) {
      section.append(element('h3', '', goal.objective));
      table(section, ['Condition', 'Required', 'Recorded status'], (goal.conditions || []).map(c => [conditionText(c), typeof c === 'object' ? c.required ?? 'Unknown' : 'Unknown', typeof c === 'object' ? c.status || 'Unknown' : 'Unknown']));
      this.refs(section, goal.evidence_refs);
    }
    const checks = this.data.facts?.verification || [];
    if (!checks.length) section.append(element('p', 'muted', 'No verification receipts recorded'));
    for (const check of checks) this.details(section, `acceptance:${check.line}:${check.type}`, `${check.type} · L${check.line}`, body => {
      // Retain recorded IDs/subjects without inventing cross-receipt identity matches.
      const value = check.value || {};
      table(body, ['Recorded field', 'Value'], Object.entries(value).filter(([, v]) => v == null || typeof v !== 'object'));
      for (const [key, item] of Object.entries(value).filter(([, v]) => v && typeof v === 'object')) {
        const nested = disclosure(body, key); nested.append(element('pre', 'goal-report', JSON.stringify(item, null, 2)));
      }
      this.refs(body, check.evidence_refs);
    });
    const decisions = (this.data.facts?.events || []).filter(e => /^(run\.(completed|failed|stopped)|task\.(decision|outcome|completed))$/.test(e.type));
    if (decisions.length) {
      const recorded = element('section'); recorded.append(element('h3', '', 'Host records')); section.append(recorded);
      for (const event of decisions) {recorded.append(element('p', '', `${event.type} · L${event.line}`)); this.refs(recorded, [event.source_ref]);}
    }
  }
  renderReport(root) {
    const article = element('article', 'skeleton-report');
    const sessionId = this.data.session_id || this.data.source?.session_id;
    if (sessionId && !this.data.report.text.includes(`Session: ${sessionId}`)) article.append(element('p', 'analysis-session-id', `Session: ${sessionId}`));
    article.append(renderMarkdown(this.data.report.text)); root.append(article);
    const checks = disclosure(article, 'Acceptance evidence'); checks.dataset.key = 'report:acceptance';
    for (const v of this.data.facts.verification) {const row = disclosure(checks, `${v.type} · L${v.line}`); row.append(element('pre', 'goal-report', JSON.stringify(v.value, null, 2))); this.refs(row, v.evidence_refs);}
    const requests = (this.data.evaluator_usage.reservations || []).filter(r => r.input_profile);
    if (requests.length) {
      const costs = disclosure(article, 'Evaluator input cost'); costs.dataset.key = 'report:cost';
      table(costs, ['Request', 'Cards', 'Input chars · incl. schema', 'Cards / state / schema chars', 'Extra evidence chars', 'Reported input / output tokens'], requests.map(r => {
        const p = r.input_profile, c = p.components_chars || {};
        const schemaChars = c.response_schema != null && c.contract != null ? c.response_schema + c.contract : null;
        return [r.id, number(p.card_count), number(p.total_chars), `${number(c.cards)} / ${number(c.state)} / ${number(schemaChars)}`, number(c.extra_evidence), r.usage ? `${number(r.usage.prompt_tokens)} / ${number(r.usage.completion_tokens)}` : 'Unknown'];
      }));
    }
    const validationIssues = this.data.coverage.review_gaps.filter(gap => gap.category ? gap.category !== 'coverage' : gap.record_type !== 'structure' || 'record' in gap);
    if (validationIssues.length) {
      const gaps = disclosure(article, `Validation issues · ${validationIssues.length}`); gaps.dataset.key = 'report:gaps';
      for (const gap of validationIssues) gaps.append(element('p', '', `${gap.code ? `[${gap.code}] ` : ''}${gap.unit_id || gap.batch_id}${gap.record_type ? ` · ${gap.record_type}` : ''}${gap.record_index ? ` #${gap.record_index}` : ''}${gap.path?.length ? ` · ${gap.path.join('/')}` : ''}: ${gap.reason}`));
    }
  }
}
