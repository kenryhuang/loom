import {element} from '../markdown.mjs';
import {label, seconds} from './dashboard.mjs';

const ns = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}
function caption(node, text, x, y, max = 42) {
  const lines = String(text || '').match(new RegExp(`.{1,${max}}`, 'gu')) || [];
  for (const [i, line] of lines.slice(0, 2).entries()) node.append(svg('text', {x, y: y + i * 17, class: 'goal-graph-caption'}, line));
  node.append(svg('title', {}, text || 'Unknown'));
}
function activate(node, action) {
  node.setAttribute('tabindex', '0'); node.setAttribute('role', 'button');
  node.addEventListener('click', action);
  node.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); action();}});
}
let serial = 0;
export function renderGoalProcess(root, {goal, attempts, phases, relations, limit, highlight, inspect, executionView}) {
  const id = `goal-arrow-${++serial}`, width = 1060, positions = new Map();
  const chart = svg('svg', {viewBox: `0 0 ${width} 200`, class: 'goal-process-graph', 'aria-label': 'Goal attempts and phase occurrences', role: 'group'});
  const defs = svg('defs'), marker = svg('marker', {id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse'});
  marker.append(svg('path', {d: 'M 0 0 L 10 5 L 0 10 z', fill: 'currentColor'})); defs.append(marker); chart.append(defs);
  const edges = svg('g'), nodes = svg('g'); chart.append(edges, nodes);
  let y = 20;
  const endpoint = (text, klass) => {
    const group = svg('g', {class: `goal-graph-endpoint ${klass}`});
    group.append(svg('rect', {x: 24, y, width: width - 48, height: 60, rx: 10}));
    caption(group, text, 40, y + 24, 100); nodes.append(group); const position = {x:24, y, width:width-48, height:60}; y += 90; return position;
  };
  const beginning = endpoint(goal.objective, 'start');
  const arrow = (a, b, text, semantic = false, curve = 0) => {
    const x1 = a.x + (a.width || 292)/2, y1 = a.y + (a.height || 110), x2 = b.x + (b.width || 292)/2, y2 = b.y;
    const group = svg('g', {class: `goal-process-edge ${semantic ? 'semantic' : 'order'}`});
    const route = curve ? `M ${a.x+292} ${a.y+(a.height||110)/2} C ${width-8-curve} ${a.y+(a.height||110)/2}, ${width-8-curve} ${b.y+(b.height||110)/2}, ${b.x+292} ${b.y+(b.height||110)/2}` :
      `M ${x1} ${y1} C ${x1} ${y1+26}, ${x2} ${y2-26}, ${x2} ${y2}`;
    group.append(svg('path', {d: route, 'marker-end': `url(#${id})`}));
    const tx = curve ? width-140-curve/2 : (x1+x2)/2, ty = curve ? (a.y+b.y)/2+54 : (y1+y2)/2-4;
    group.append(svg('text', {x: tx, y: ty, 'text-anchor': 'middle'}, text));
    group.append(svg('title', {}, semantic ? text : 'Source archive order; not evidence of causal adoption'));
    edges.append(group); return group;
  };
  let previous = beginning;
  for (const [i, attempt] of attempts.entries()) {
    const rows = phases.filter(p => p.attempt_id === attempt.id).slice(0, limit);
    nodes.append(svg('text', {x: 24, y, class: 'goal-graph-attempt'}, `Attempt ${i+1} · ${attempt.approach.slice(0, 100)}`)); y += 28;
    for (const [j, p] of rows.entries()) {
      // Alternating columns expose feedback occurrences without reversing source time.
      const column = j % 3, x = 24 + column * 328;
      const pos = {x, y, height: 156}; positions.set(p.id, pos);
      const group = svg('g', {class: `goal-graph-node ${p.kind}${highlight?.has(p.id) ? ' highlighted' : ''}`, 'data-phase-id': p.id});
      group.append(svg('rect', {x, y, width: 292, height: 156, rx: 9}));
      group.append(svg('text', {x: x+12, y: y+21, class: 'goal-graph-kind'}, label(p.kind)));
      caption(group, p.question || p.delta || p.actions, x+12, y+45, 35);
      group.append(svg('text', {x: x+12, y: y+96, class: 'goal-graph-meta'}, `L${p.start_line}–${p.end_line} · ${p.confidence || 'uncalibrated'} inference`));
      const scope=executionView?.phases[p.id];
      if(scope) {
        const cost=scope.related_costs, measured=(field)=>cost[field].measured_count?seconds(cost[field].known_sum):cost[field].eligible_count?'Unknown':'0 s';
        group.append(svg('text',{x:x+12,y:y+115,class:'goal-graph-meta'},`Linked: model ${measured('model_duration_ms')} · tool ${measured('tool_duration_ms')}`));
        group.append(svg('text',{x:x+12,y:y+133,class:'goal-graph-meta'},`Window ${seconds(scope.timing.wall_span_ms)} · ${scope.other_call_ids.length} other calls`));
        const max=Math.max(1,...Object.values(executionView.phases).map(w=>w.timing.wall_span_ms||0));
        group.append(svg('rect',{x:x+12,y:y+143,width:268*(scope.timing.wall_span_ms||0)/max,height:5,rx:2,class:'goal-phase-time-bar'}));
      } else group.append(svg('text',{x:x+12,y:y+125,class:'goal-graph-meta'},'Timing unavailable'));
      group.setAttribute('aria-label', `${label(p.kind)}: ${p.question || p.delta || p.actions}`);
      activate(group, () => inspect(p)); nodes.append(group);
      if (previous) arrow(previous, pos, 'source order');
      previous = pos; y += 200;
    }
    if (!rows.length) {nodes.append(svg('text', {x: 24, y}, 'No supported phase occurrences')); y += 35;}
  }
  for (const [i, relation] of relations.entries()) {
    const a = positions.get(relation.source_id), b = positions.get(relation.target_id);
    if (a && b) {const edge = arrow(a, b, label(relation.kind), true, 12+(i%3)*18); activate(edge, () => inspect(relation));}
  }
  const hidden = phases.filter(p => !positions.has(p.id)).length;
  if (hidden) {nodes.append(svg('text', {x: 24, y}, `${hidden} additional phases — expand the phase limit below`)); y += 35;}
  const state = `${label(goal.execution_state)} · ${label(goal.goal_disposition)} · ${label(goal.end_boundary)}`;
  const ending = endpoint(`Boundary: ${state}`, 'end');
  arrow(previous, ending, 'snapshot boundary');
  chart.setAttribute('viewBox', `0 0 ${width} ${y}`);
  root.append(element('p', 'muted small', 'Solid: source order · Dashed: cited relationship · Bars: evidence-window span, not exclusive phase cost.'));
  const scroll = element('div', 'goal-graph-scroll'); scroll.append(chart); root.append(scroll);
}

export function renderCallTimeline(root, atoms, inspect) {
  const measured = atoms.map(a => ({...a, start: Date.parse(a.started_at), end: Date.parse(a.ended_at)}))
    .filter(a => Number.isFinite(a.start) && Number.isFinite(a.end) && a.end >= a.start);
  const unknown = atoms.length - measured.length;
  if (!measured.length) {root.append(element('p', 'muted', `No comparable timestamps · ${unknown} calls without measured intervals`)); return;}
  const start = Math.min(...measured.map(a => a.start)), end = Math.max(...measured.map(a => a.end)), span = Math.max(1, end-start);
  root.append(element('p', 'muted small', `${new Date(start).toISOString()} → ${new Date(end).toISOString()} · ${unknown} calls without measured intervals`));
  for (const a of measured.slice(0, 100)) {
    const row = element('button', 'goal-timeline-row'); row.type = 'button';
    row.append(element('span', '', `${a.tool || a.model} · ${a.role || 'unknown'}`));
    const track = element('span', 'goal-timeline-track'), bar = element('span', 'goal-timeline-bar');
    bar.style.marginLeft = `${(a.start-start)/span*100}%`; bar.style.width = `${Math.max(.3, (a.end-a.start)/span*100)}%`;
    bar.title = `${new Date(a.start).toISOString()} · ${seconds(a.duration_ms)}`; track.append(bar); row.append(track);
    row.addEventListener('click', () => inspect(a)); root.append(row);
  }
  if (measured.length > 100) root.append(element('p', 'muted', `${measured.length-100} additional intervals in the call ledger / JSON export`));
}
