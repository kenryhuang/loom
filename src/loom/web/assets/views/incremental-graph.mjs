import {element, renderMarkdown} from '../markdown.mjs';
import {disclosure, panel, table, seconds, number} from './dashboard.mjs';
import {skeletonPresentation, scopeSkeleton, layoutSkeleton} from './skeleton-layout.mjs';

let viewSequence = 0;
const button = (text, action) => {
  const node = element('button', 'quiet', text); node.type = 'button'; node.addEventListener('click', action); return node;
};
const svgNode = (name, attributes = {}, text = '') => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text) node.textContent = text;
  return node;
};
function wrap(text, width, count) {
  const measure = value => [...value].reduce((n, c) => n + (c.codePointAt(0) >= 0x1100 ? 2 : 1), 0);
  const lines = [], words = String(text || '').split(/\s+/); let line = '';
  for (let word of words) {
    while (measure(word) > width) {
      if (line) {lines.push(line); line = '';}
      let part = ''; for (const char of word) {if (measure(part + char) > width) break; part += char;}
      lines.push(part); word = word.slice(part.length);
    }
    if (line && measure(line) + measure(word) + 1 > width) {lines.push(line); line = '';}
    line += (line ? ' ' : '') + word;
  }
  if (line) lines.push(line);
  if (lines.length > count) {lines.length = count; lines[count - 1] = lines[count - 1].slice(0, width - 1) + '…';}
  return lines;
}
function svgText(parent, text, x, y, width, count, size = 13, color = '#172b43') {
  const node = svgNode('text', {x, y, fill: color, 'font-size': size});
  wrap(text, width, count).forEach((line, i) => node.append(svgNode('tspan', {x, dy: i ? size + 4 : 0}, line)));
  parent.append(node); return node;
}
const conditionText = c => typeof c === 'string' ? c : c.description || c.id || JSON.stringify(c);

export class IncrementalGraphView {
  constructor(root, {evidence = () => {}} = {}) {
    Object.assign(this, {root, evidence, mode: 'skeleton', limit: 60, selected: null, goal: null, uid: ++viewSequence});
  }
  update(data) {
    if (this.data && (this.data.analysis_id !== data.analysis_id || this.data.source.digest !== data.source.digest)) {
      this.selected = null; this.goal = null; this.limit = 60;
    }
    this.data = data; this.graph = skeletonPresentation(data); this.render();
  }
  refs(root, refs = []) {
    const actions = element('div', 'action-utility-refs');
    for (const ref of [...new Map(refs.map(r => [JSON.stringify(r), r])).values()])
      actions.append(button(`L${ref.line_number}${ref.field_path ? ' · ' + ref.field_path : ''}`, () => this.evidence(ref)));
    root.append(actions);
  }
  selectGoal(id) {this.goal = id; this.selected = null; this.limit = 60; this.render(); this.root.querySelector('.skeleton-inspector button')?.focus({preventScroll: true});}
  selectNode(id) {
    const node = this.graph.nodes.find(n => n.id === id);
    if (node?.kind === 'root') return this.selectGoal(node.goal_id);
    this.selected = id; this.render(); this.root.querySelector('.skeleton-inspector button')?.focus({preventScroll: true});
  }
  render() {
    const scroll = this.root.scrollTop;
    const opened = new Set([...this.root.querySelectorAll('details[open]')].map(n => n.dataset.key).filter(Boolean));
    this.root.replaceChildren(); this.root.classList.add('incremental-graph');
    const header = element('div', 'dashboard-toolbar skeleton-toolbar');
    const tabs = element('div', 'trajectory-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Analysis result');
    for (const [mode, title] of [['skeleton', 'Skeleton'], ['report', 'Report']]) {
      const tab = button(title, () => {this.mode = mode; this.render();});
      tab.id = `skeleton-tab-${this.uid}-${mode}`; tab.tabIndex = this.mode === mode ? 0 : -1;
      tab.setAttribute('aria-controls', `skeleton-panel-${this.uid}`);
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(this.mode === mode));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); this.mode = event.key === 'Home' ? 'skeleton' : event.key === 'End' ? 'report' : this.mode === 'skeleton' ? 'report' : 'skeleton';
        this.render(); this.root.querySelector('[role=tab][aria-selected=true]')?.focus();
      });
      tab.classList.toggle('active', this.mode === mode); tabs.append(tab);
    }
    header.append(tabs);
    this.root.append(header);
    const content = element('section', 'skeleton-tab-panel'); content.setAttribute('role', 'tabpanel');
    content.id = `skeleton-panel-${this.uid}`; content.setAttribute('aria-labelledby', `skeleton-tab-${this.uid}-${this.mode}`);
    content.setAttribute('aria-label', this.mode === 'skeleton' ? 'Skeleton' : 'Report'); this.root.append(content);
    if (this.mode === 'report') this.renderReport(content);
    else this.renderSkeleton(content);
    for (const node of this.root.querySelectorAll('details[data-key]')) if (opened.has(node.dataset.key)) node.open = true;
    this.root.scrollTop = scroll;
  }
  renderSkeleton(root) {
    const layout = element('div', 'skeleton-workspace'), main = element('div', 'skeleton-main');
    layout.append(main); root.append(layout);
    const scope = scopeSkeleton(this.graph, this.goal), shown = scope.nodes.slice(0, this.limit), ids = new Set(shown.map(n => n.id));
    const edges = scope.edges.filter(e => ids.has(e.source_id) && ids.has(e.target_id));
    const diagram = element('section', 'skeleton-diagram'); main.append(diagram);
    const legend = element('div', 'skeleton-legend');
    legend.append(element('span', '', 'Solid: result / feedback'), element('span', '', 'Dashed: objective scope'));
    if (this.goal) legend.append(element('span', '', 'Faded: referenced work from another goal'));
    diagram.append(legend);
    this.drawDag(diagram, shown, edges, scope.focus);
    if (!this.data.skeleton.edges.length) diagram.append(element('p', 'muted small', 'Result dependencies have not been established in this revision.'));
    if (scope.nodes.length > shown.length) diagram.append(button(`Show more · ${scope.nodes.length - shown.length} remaining`, () => {this.limit += 60; this.render();}));
    const chosen = this.graph.nodes.find(n => n.id === this.selected);
    if (chosen || this.goal) {
      const inspector = element('aside', 'skeleton-inspector'); inspector.setAttribute('role', 'dialog');
      inspector.setAttribute('aria-label', chosen ? `${chosen.display_id} details` : 'Goal details');
      inspector.addEventListener('keydown', event => {if (event.key === 'Escape') {this.selected = null; this.goal = null; this.render();}});
      main.append(inspector);
      if (chosen) this.renderStage(inspector, chosen);
      else this.renderGoal(inspector, this.graph.goals.find(g => g.id === this.goal));
    }
  }
  nodeTone(node) {
    const cards = this.data.extraction.cards.filter(c => node.card_ids.includes(c.id));
    if (cards.some(c => ['failed', 'cancelled'].includes(c.status))) return 'failure';
    const checks = this.data.facts.verification.filter(v => cards.some(c => c.kind === 'boundary' && c.start_line === v.line));
    const status = v => v.value?.status || v.value?.result?.status;
    if (checks.some(v => ['passed', 'accepted'].includes(status(v)) || v.value?.passed === true)) return 'accepted';
    if (node.kind === 'unassigned' || this.data.diagnostics.some(d => d.card_ids?.some(c => node.card_ids.includes(c))) ||
        this.data.record_assessments.some(a => node.card_ids.includes(a.card_id) && (a.necessity === 'avoidable' || a.redundancy === 'supported'))) return 'attention';
    return node.kind === 'root' ? 'root' : 'normal';
  }
  drawDag(root, nodes, edges, focus) {
    const layout = layoutSkeleton(nodes, edges), markerId = `skeleton-arrow-${this.uid}`;
    const scroller = element('div', 'skeleton-canvas'); scroller.tabIndex = 0;
    const svg = svgNode('svg', {viewBox: `0 0 ${layout.width} ${layout.height}`, role: 'img', 'aria-label': 'Goal process graph'});
    svg.style.minWidth = `${Math.min(layout.width, 1150)}px`;
    const defs = svgNode('defs'), marker = svgNode('marker', {id: markerId, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse'});
    marker.append(svgNode('path', {d: 'M 0 0 L 10 5 L 0 10 z', fill: '#738ca5'})); defs.append(marker); svg.append(defs);
    for (const edge of edges) {
      const a = layout.positions.get(edge.source_id), b = layout.positions.get(edge.target_id); if (!a || !b || b.rank <= a.rank) continue;
      const x = a.x + a.width / 2, y = a.y + a.height, xx = b.x + b.width / 2, yy = b.y;
      let d, labelX, labelY;
      if (b.rank - a.rank > 1) {
        const lane = layout.longEdges.findIndex(e => e === edge), side = layout.bodyWidth + 30 + (lane % 3) * 25;
        d = `M ${x} ${y} V ${y+28} H ${side} V ${yy-28} H ${xx} V ${yy}`;
        labelX = side + 10; labelY = y + 55 + (lane % 3) * 34;
      } else {
        const mid = (y + yy) / 2;
        d = `M ${x} ${y} C ${x} ${mid},${xx} ${mid},${xx} ${yy}`;
        labelX = (x + xx) / 2 + 10; labelY = mid - 16;
      }
      const group = svgNode('g', {'data-edge-id': edge.id || `${edge.source_id}:${edge.target_id}`});
      const path = svgNode('path', {d, fill: 'none', stroke: '#738ca5', 'stroke-width': 1.5, 'marker-end': `url(#${markerId})`});
      if (edge.layer === 'scope') path.setAttribute('stroke-dasharray', '4 4');
      group.append(path, svgNode('title', {}, `${edge.kind}: ${edge.result || ''}`));
      svgText(group, edge.result || edge.kind, labelX, labelY, 22, 2, 11, '#506b86'); svg.append(group);
    }
    const colors = {normal: ['#f5f7fa', '#607e9a'], root: ['#edf3fb', '#50749b'], attention: ['#fff2cc', '#b38a35'], failure: ['#ffe1db', '#b44331'], accepted: ['#ddf3e2', '#378056']};
    for (const node of nodes) {
      const p = layout.positions.get(node.id); if (!p) continue;
      const external = this.goal && !focus.has(node.id) && node.kind !== 'root', tone = this.nodeTone(node), [fill, stroke] = colors[tone];
      const g = svgNode('g', {role: 'button', tabindex: 0, 'aria-label': `${node.display_id} · ${node.objective}`, 'data-node-id': node.id, 'data-rank': p.rank,
        'data-goal-id': node.goal_id || '', 'data-tone': tone, opacity: external ? 0.65 : 1});
      g.append(svgNode('rect', {x: p.x, y: p.y, width: p.width, height: p.height, rx: 9, fill,
        stroke: this.selected === node.id ? '#215f9b' : stroke, 'stroke-width': this.selected === node.id ? 3 : 1.3}));
      svgText(g, `${node.display_id}${node.kind === 'root' ? '' : ' · ' + node.goal_display_id}${external ? ' · referenced' : ''}`, p.x+14, p.y+23, 40, 1, 12, '#496581');
      svgText(g, node.objective, p.x+14, p.y+47, 36, 2, 14);
      svgText(g, node.key_delta, p.x+14, p.y+91, 42, 2, 11, '#50677d');
      if (node.kind !== 'root') {
        const cost = node.cost, counts = cost ? `${cost.model_calls} model · ${cost.tool_calls} tool` : `${node.call_ids.length} calls`;
        svgText(g, `${counts} · ${seconds(cost?.observed_busy_union_ms)} busy`, p.x+14, p.y+134, 48, 1, 11, '#50677d');
      }
      g.append(svgNode('title', {}, `${node.display_id} · ${node.objective}\n${node.key_delta}\n${node.outcome_or_gap}`));
      g.addEventListener('click', () => this.selectNode(node.id));
      g.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); this.selectNode(node.id);}}); svg.append(g);
    }
    scroller.append(svg); root.append(scroller);
    if (layout.invalid.length) root.append(element('p', 'notice', `${layout.invalid.length} nodes have conflicting dependencies; inspect the saved report.`));
  }
  closure(root, goal) {
    if (goal.conditions?.length) table(root, ['Completion conditions'], goal.conditions.map(c => [conditionText(c)]));
    const closure = this.data.closures[goal.id];
    if (!closure) {root.append(element('p', 'muted', 'Closure not established')); return;}
    table(root, ['Completion claim', 'Evidence support', 'Disposition'], [[closure.claim, closure.evidence_state, closure.disposition]]);
    if (closure.reason) root.append(element('p', '', closure.reason));
    for (const q of closure.open_questions || []) root.append(element('p', '', `Open: ${q}`));
    this.refs(root, closure.evidence_refs);
  }
  renderGoal(root, goal) {
    if (!goal) return;
    const detail = panel(root, `${goal.display_id} · ${goal.objective}`, true); detail.classList.add('skeleton-detail');
    detail.querySelector('header').append(button('Close details', () => this.selectGoal(null))); this.closure(detail, goal);
    const children = this.graph.goals.filter(g => g.parent_goal_id === goal.id);
    if (children.length) table(detail, ['Subgoal'], children.map(g => [button(`${g.display_id} · ${g.objective}`, () => this.selectGoal(g.id))]));
    const stages = this.graph.nodes.filter(n => n.kind !== 'root' && n.goal_id === goal.id);
    table(detail, ['Stage', 'Key change', 'Source'], stages.map(n => [button(`${n.display_id} · ${n.objective}`, () => this.selectNode(n.id)), n.key_delta, `L${n.start_line}–${n.end_line}`]));
    const attempts = (this.data.attempts || []).filter(a => a.goal_id === goal.id && a.basis !== 'unknown');
    if (attempts.length) table(detail, ['Approach', 'Outcome'], attempts.map(a => [a.approach, a.outcome]));
    this.findings(detail, goal.id); this.refs(detail, goal.evidence_refs);
  }
  renderStage(root, node) {
    const detail = panel(root, `${node.display_id} · ${node.objective}`, true); detail.classList.add('skeleton-detail');
    const goal = this.graph.goals.find(g => g.id === node.goal_id);
    const controls = element('div', 'dashboard-toolbar');
    if (goal) controls.append(button(`${goal.display_id} · ${goal.objective}`, () => this.selectGoal(goal.id)));
    detail.querySelector('header').append(button('Close details', () => {this.selected = null; this.goal = null; this.render();})); detail.append(controls);
    detail.append(element('p', '', node.key_delta), element('p', 'muted', node.outcome_or_gap));
    const related = this.graph.edges.filter(e => e.layer === 'semantic' && (e.source_id === node.id || e.target_id === node.id));
    table(detail, ['Dependency', 'Result / feedback'], related.map(e => {
      const incoming = e.target_id === node.id, other = this.graph.nodes.find(n => n.id === (incoming ? e.source_id : e.target_id));
      return [button(`${incoming ? 'From' : 'To'} ${other.display_id} · ${other.objective}`, () => this.selectNode(other.id)), e.result];
    }));
    const actions = disclosure(detail, `Actions · ${node.card_ids.length}`); actions.open = true; actions.dataset.key = `actions:${node.id}`;
    for (const cid of node.card_ids) this.renderAction(actions, cid);
    const evidence = disclosure(detail, 'Stage evidence'); evidence.dataset.key = `evidence:${node.id}`; this.refs(evidence, node.evidence_refs);
    if (goal) this.findings(detail, goal.id, node.card_ids);
  }
  renderAction(root, cid) {
    const data = this.data, card = data.extraction.cards.find(c => c.id === cid); if (!card) return;
    const assessment = data.record_assessments.find(a => a.card_id === cid);
    const action = disclosure(root, `${card.kind} · ${card.name} · ${card.status} · L${card.start_line}–${card.end_line}`); action.dataset.key = `card:${cid}`;
    if (assessment) table(action, ['Assessment', 'Finding'], ['purpose', 'expected', 'delta', 'contribution', 'necessity', 'redundancy'].map(k => [k, assessment[k]]));
    const calls = data.cost_ledger.atoms.filter(a => card.call_ids.includes(a.id));
    table(action, ['Call', 'Duration', 'Tokens'], calls.map(a => [a.tool || a.model || a.id, seconds(a.duration_ms), a.total_tokens ?? 'Unknown']));
    table(action, ['Evidence state', 'Scope'], [['Captured', card.captured.length ? `${card.captured.length} recorded result(s)` : 'Not recorded'],
      ['Presented', card.presented.length ? `${card.presented.length} delivery record(s)` : 'Unknown'],
      ['Adopted', assessment?.uptake_card_ids?.length ? assessment.uptake_card_ids.join(', ') : 'Not established']]);
    const raw = disclosure(action, 'Evidence ranges'); raw.dataset.key = `ranges:${cid}`;
    raw.append(element('pre', 'goal-report', JSON.stringify({captured: card.captured, presented: card.presented}, null, 2)));
    this.refs(action, [...new Set([...card.refs, ...card.raw_refs])].map(id => data.extraction.registry[id]?.ref).filter(Boolean));
  }
  findings(root, goalId, cardIds = null) {
    const rows = this.data.diagnostics.filter(d => d.goal_id === goalId && (!cardIds || d.card_ids?.some(id => cardIds.includes(id))));
    if (!rows.length) return;
    const detail = disclosure(root, `Findings · ${rows.length}`); detail.dataset.key = `findings:${goalId}:${cardIds?.join(',') || ''}`;
    for (const row of rows) {detail.append(element('strong', '', row.observation)); for (const key of ['interpretation', 'alternative', 'improvement']) detail.append(element('p', '', `${key}: ${row[key]}`)); this.refs(detail, row.evidence_refs);}
  }
  renderReport(root) {
    const article = element('article', 'skeleton-report'); article.append(renderMarkdown(this.data.report.text)); root.append(article);
    const checks = disclosure(article, 'Acceptance evidence'); checks.dataset.key = 'report:acceptance';
    for (const v of this.data.facts.verification) {const row = disclosure(checks, `${v.type} · L${v.line}`); row.append(element('pre', 'goal-report', JSON.stringify(v.value, null, 2))); this.refs(row, v.evidence_refs);}
    const requests = (this.data.evaluator_usage.reservations || []).filter(r => r.input_profile);
    if (requests.length) {
      const costs = disclosure(article, 'Evaluator input cost'); costs.dataset.key = 'report:cost';
      table(costs, ['Request', 'Cards', 'Input chars · incl. schema', 'Cards / state / schema chars', 'Extra evidence chars', 'Reported input / output tokens'], requests.map(r => {
        const p = r.input_profile, c = p.components_chars;
        return [r.id, p.card_count, number(p.total_chars), `${c.cards} / ${c.state} / ${c.response_schema+c.contract}`, c.extra_evidence, r.usage ? `${r.usage.prompt_tokens} / ${r.usage.completion_tokens}` : 'Unknown'];
      }));
    }
    if (this.data.coverage.review_gaps.length) {
      const gaps = disclosure(article, `Unresolved groups · ${this.data.coverage.review_gaps.length}`); gaps.dataset.key = 'report:gaps';
      for (const gap of this.data.coverage.review_gaps) gaps.append(element('p', '', `${gap.batch_id}: ${gap.reason}`));
    }
  }
}
