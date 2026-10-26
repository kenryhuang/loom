/** Read-only Goal → Stage → Action projection over saved incremental judgments.
 * Display attribution never changes the semantic graph or its cost owners.
 */
export function callSummary(ids, atoms) {
  const calls = [...new Set(ids)].map(id => atoms.get(id)).filter(Boolean);
  const timed = calls.filter(a => typeof a.duration_ms === 'number' && Number.isFinite(a.duration_ms) && a.duration_ms >= 0);
  return {model: calls.filter(a => a.kind === 'model').length, tool: calls.filter(a => a.kind === 'tool').length,
    duration: timed.reduce((sum, a) => sum + a.duration_ms, 0), unknown: calls.length - timed.length,
    calls: calls.length, timed: timed.length};
}

export function goalTree(data) {
  const cards = [...(data.extraction?.cards || [])].sort((a, b) => a.start_line - b.start_line || a.end_line - b.end_line || a.id.localeCompare(b.id));
  // Boundary records carry requirements/receipts, not execution. Keep them in
  // the source index, but do not give them Action rows or interrupt visits.
  const eventOnly = card => card.kind === 'boundary' && !card.call_ids.length;
  const actionOrdinals = new Map(cards.filter(card => !eventOnly(card)).map((card, i) => [card.id, i + 1]));
  const atoms = new Map((data.cost_ledger?.atoms || []).map(a => [a.id, a]));
  const assessments = new Map((data.record_assessments || []).map(a => [a.card_id, a]));
  const stages = new Map((data.stages || []).map(s => [s.id, s]));
  let goalNumber = 0;
  const goals = (data.goals || []).map(g => {
    const label = g.parent_goal_id ? `G${++goalNumber}` : 'Root';
    return {...g, display_id: g.display_id || label, children: [], stages: [], requirements: [], call_ids: [], card_ids: []};
  });
  const byGoal = new Map(goals.map(g => [g.id, g]));
  const roots = [];
  for (const g of goals) {
    const visited = new Set([g.id]); let parent = byGoal.get(g.parent_goal_id), cursor = parent;
    while (cursor && !visited.has(cursor.id)) {visited.add(cursor.id); cursor = byGoal.get(cursor.parent_goal_id);}
    if (cursor || !parent) roots.push(g);
    else parent.children.push(g);
  }
  // Missing root metadata must not hide recorded calls or invent a task instruction.
  if (!roots.length) {
    const root = {id: 'display:root', display_id: 'Root', objective: 'Task instruction unavailable', children: [],
      stages: [], requirements: [], call_ids: [], card_ids: [], conditions: []};
    roots.push(root); goals.push(root); byGoal.set(root.id, root);
  }
  const rootFor = card => roots.find(g => (g.origin_line ?? 0) <= card.start_line && (g.end_line ?? Infinity) >= card.start_line) || roots[0];
  const stageRows = new Map(), unresolved = new Set(), membership = new Map();
  const defaults = new Map((data.action_attributions || []).filter(r => r.basis === 'default_rule').map(r => [r.card_id, r]));
  const history = [];
  const scope = card => JSON.stringify([card.actor?.loop || card.actor?.trace || null, card.actor?.role ?? null]);
  function attach(goal, spec, card, assessment, extra = {}) {
    const key = JSON.stringify([goal.id, spec.id]);
    if (!stageRows.has(key)) {
      const row = {...spec, id: key, source_stage_id: spec.id, goal_id: goal.id, actions: [], call_ids: [], card_ids: [],
        first_line: card.start_line, last_line: card.end_line, attempt_ids: new Set()};
      goal.stages.push(row); stageRows.set(key, row);
    }
    const row = stageRows.get(key);
    row.actions.push({card, assessment, ordinal: actionOrdinals.get(card.id), ...extra});
    row.first_line = Math.min(row.first_line, card.start_line); row.last_line = Math.max(row.last_line, card.end_line);
    if (spec.attempt_id) row.attempt_ids.add(spec.attempt_id);
    row.card_ids.push(card.id); row.call_ids.push(...card.call_ids);
    membership.set(card.id, [...(membership.get(card.id) || []), row.id]);
  }
  for (const card of cards) {
    const root = rootFor(card), assessment = assessments.get(card.id);
    const requirement = card.kind === 'boundary' && /^(acceptance\.(plan|contract)|task\.(configured|defined))/.test(card.name);
    if (requirement) {
      root.requirements.push({card, assessment, framework_rule: true}); root.card_ids.push(card.id); root.call_ids.push(...card.call_ids); continue;
    }
    if (eventOnly(card)) continue;
    const previous = history.findLast(row => row.root === root.id && row.card.end_line <= card.start_line && scope(row.card) === scope(card));
    const declared = (assessment?.stage_ids || []).map(id => stages.get(id)).filter(s => s && byGoal.has(s.goal_id));
    const verification = card.actor?.role === 'verification' || card.call_ids.some(id => atoms.get(id)?.role === 'verification') ||
      /^(verification\.|acceptance\.|task\.(assessed|decision|outcome|completed)|run\.(completed|failed|stopped))/.test(card.name);
    const finish = card.kind === 'tool' && card.name === 'finish';
    const framework = requirement || verification || finish;
    // Framework ownership is established even when utility remains unknown.
    if (!declared.length && !framework) unresolved.add(card.id);
    if (verification) {
      attach(root, {id: 'recorded:acceptance', title: 'Acceptance & root decision', kind: 'verification', basis: 'framework_rule'},
        card, assessment, {framework_rule: true, root_override: declared.some(s => s.goal_id !== root.id)});
      continue;
    }
    if (finish) {
      attach(root, {id: 'recorded:delivery', title: 'Submit result', kind: 'conclusion', basis: 'framework_rule'},
        card, assessment, {framework_rule: true, root_override: declared.some(s => s.goal_id !== root.id)});
      continue;
    }
    if (declared.length) {
      const seenGoals = new Set();
      for (const stage of declared) {
        attach(byGoal.get(stage.goal_id), {...stage, basis: 'assessed_stage'}, card, assessment,
          {shared: seenGoals.size > 0 && !seenGoals.has(stage.goal_id)});
        seenGoals.add(stage.goal_id);
      }
      const goalIds = [...new Set(declared.map(s => s.goal_id))];
      if (card.kind !== 'boundary') history.push({goal: goalIds.length === 1 ? goalIds[0] : root.id, card, root: root.id});
    } else if (['finish', 'submit', 'submit_result'].includes(card.name)) {
      attach(root, {id: 'recorded:delivery', title: 'Submit result', kind: 'conclusion', basis: 'recorded'}, card, assessment);
    } else {
      const saved = defaults.get(card.id);
      const goal = byGoal.get(saved?.goal_id) || (assessment?.relation !== 'unrelated' && previous ? byGoal.get(previous.goal) : root);
      const anchorId = saved ? saved.anchor_card_id : assessment?.relation !== 'unrelated' ? previous?.card.id : null;
      const attribution = {basis: 'default_rule', rule: saved?.rule || (anchorId ? 'previous_action_same_scope' : 'current_root'),
        goal: goal.id, card_id: anchorId || null, refs: []};
      attach(goal, {id: `continue:${goal.id}`, title: `Continue: ${goal.objective}`, basis: 'default_rule'}, card, assessment, {attribution});
      if (card.kind !== 'boundary') history.push({goal: goal.id, card, root: root.id});
    }
  }
  let stageNumber = 0;
  for (const stage of [...stageRows.values()].sort((a, b) => a.first_line - b.first_line || a.id.localeCompare(b.id))) {
    // S labels identify semantic stages, not arbitrary UI containers. Framework
    // groups and default continuation never consume a stage number.
    if (stage.basis === 'assessed_stage') {stageNumber += 1; stage.display_id = stage.display_id || `S${stageNumber}`;}
    else stage.display_id = stage.basis === 'framework_rule' ? 'Root' : '';
    stage.call_ids = [...new Set(stage.call_ids)]; stage.card_ids = [...new Set(stage.card_ids)];
    stage.cost = callSummary(stage.call_ids, atoms);
    stage.attempt_ids = [...stage.attempt_ids];
    stage.visits = [];
    for (const action of stage.actions) {
      const previous = stage.visits.at(-1);
      if (previous && action.ordinal === previous.last_ordinal + 1) {
        previous.last_line = Math.max(previous.last_line, action.card.end_line);
        previous.last_ordinal = action.ordinal;
      } else stage.visits.push({first_line: action.card.start_line, last_line: action.card.end_line, last_ordinal: action.ordinal});
    }
  }
  function aggregate(g) {
    g.children.sort((a, b) => (a.origin_line ?? 0) - (b.origin_line ?? 0));
    g.stages.sort((a, b) => a.first_line - b.first_line || a.id.localeCompare(b.id));
    for (const child of g.children) aggregate(child);
    g.call_ids = [...new Set([...g.call_ids, ...g.children.flatMap(c => c.call_ids), ...g.stages.flatMap(s => s.call_ids)])];
    g.card_ids = [...new Set([...g.card_ids, ...g.children.flatMap(c => c.card_ids), ...g.stages.flatMap(s => s.card_ids)])];
    g.cost = callSummary(g.call_ids, atoms);
    const starts = [...g.stages.map(s => s.first_line), ...g.requirements.map(a => a.card.start_line),
      ...g.children.map(c => c.first_action_line)].filter(Number.isFinite);
    const ends = [...g.stages.map(s => s.last_line), ...g.requirements.map(a => a.card.end_line),
      ...g.children.map(c => c.last_action_line)].filter(Number.isFinite);
    g.first_action_line = starts.length ? Math.min(...starts) : null;
    g.last_action_line = ends.length ? Math.max(...ends) : null;
    g.parent_relation_basis = g.parent_goal_id ? (g.root_relation?.trim() ? 'inferred' : 'unreviewed') : 'root';
    g.children.sort(goalOrder);
  }
  const goalOrder = (a, b) => (a.first_action_line ?? a.origin_line ?? Infinity) - (b.first_action_line ?? b.origin_line ?? Infinity) ||
    (a.hierarchy_depth ?? 0) - (b.hierarchy_depth ?? 0) || a.id.localeCompare(b.id);
  roots.forEach(aggregate);
  roots.sort(goalOrder);
  goals.sort(goalOrder);
  // Root accounts for every invocation, including atoms without a projected card.
  if (roots.length === 1) {roots[0].call_ids = [...atoms.keys()]; roots[0].cost = callSummary(roots[0].call_ids, atoms);}
  return {roots, goals, byGoal, stages: [...stageRows.values()], cards, actionOrdinals, assessments, atoms, unresolved, membership};
}

/** Mix child goals and direct stages by first visible activity, not row type.
 * Declaration evidence may arrive later than the action it describes. A goal
 * with no activity uses its declaration line; sequence never rewrites hierarchy.
 */
export function goalEntries(goal) {
  const firstActivity = g => {
    const lines = [...g.stages.map(s => s.first_line), ...g.requirements.map(a => a.card.start_line),
      ...g.children.map(firstActivity)].filter(Number.isFinite);
    return lines.length ? Math.min(...lines) : Number.isFinite(g.origin_line) ? g.origin_line : Infinity;
  };
  return [...goal.children.map(value => ({kind: 'goal', value, line: firstActivity(value)})),
    ...goal.stages.map(value => ({kind: 'stage', value, line: Number.isFinite(value.first_line) ? value.first_line : Infinity}))]
    .sort((a, b) => a.line - b.line || (a.kind === b.kind ? 0 : a.kind === 'stage' ? -1 : 1) || a.value.id.localeCompare(b.value.id));
}

export function filterGoalTree(tree, filter = 'all') {
  if (filter === 'all') return tree.roots;
  function visit(goal, underSelected = false) {
    const selected = underSelected || goal.id === filter;
    const children = goal.children.map(g => visit(g, selected)).filter(Boolean);
    const stages = goal.stages.map(s => filter === 'unassigned' ?
      {...s, actions: s.actions.filter(a => tree.unresolved.has(a.card.id))} : s)
      .filter(s => filter === 'unassigned' ? s.actions.length : selected);
    const requirements = goal.requirements.filter(a => filter === 'unassigned' ? tree.unresolved.has(a.card.id) : selected);
    const callIds = [...new Set([...children.flatMap(g => g.call_ids), ...stages.flatMap(s => s.actions.flatMap(a => a.card.call_ids)),
      ...requirements.flatMap(a => a.card.call_ids)])];
    return selected || children.length || stages.length || requirements.length ?
      {...goal, children, stages, requirements, call_ids: callIds, cost: callSummary(callIds, tree.atoms)} : null;
  }
  return tree.roots.map(g => visit(g)).filter(Boolean);
}
