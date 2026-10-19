/** Pure presentation over saved semantic relations. No new causal or sequence edges. */
export function skeletonPresentation(data) {
  let number = 0;
  const goals = data.goals.map(g => {const label = g.parent_goal_id ? `G${++number}` : 'Root'; return {...g, display_id: g.display_id || label};});
  const byGoal = new Map(goals.map(g => [g.id, g]));
  const nodes = data.skeleton.nodes.map((n, i) => ({...n, display_id: n.display_id || `S${i + 1}`,
    goal_display_id: byGoal.get(n.goal_id)?.display_id || 'Unassigned'}));
  const ids = new Set(nodes.map(n => n.id));
  const edges = data.skeleton.edges.filter(e => ids.has(e.source_id) && ids.has(e.target_id)).map(e => ({...e, layer: 'semantic'}));
  const incoming = new Set(edges.map(e => e.target_id));
  const roots = goals.filter(g => !g.parent_goal_id);
  function rootOf(gid) {
    const seen = new Set();
    while (byGoal.has(gid) && !seen.has(gid)) {
      seen.add(gid); const goal = byGoal.get(gid);
      if (!goal.parent_goal_id) return goal;
      gid = goal.parent_goal_id;
    }
    return null;
  }
  for (const root of roots) {
    const id = `root:${root.id}`;
    nodes.unshift({id, goal_id: root.id, display_id: 'Root', goal_display_id: 'Root', kind: 'root', objective: root.objective,
      key_delta: '', outcome_or_gap: '', start_line: root.origin_line, end_line: root.origin_line,
      call_ids: [], card_ids: [], member_ids: [], evidence_refs: root.evidence_refs || []});
    for (const node of nodes.filter(n => n.kind !== 'root' && !incoming.has(n.id) && rootOf(n.goal_id)?.id === root.id)) {
      // Scope edges say what the work serves, never that it follows another action.
      edges.push({id: `scope:${node.id}`, source_id: id, target_id: node.id, kind: 'objective', layer: 'scope', result: 'objective', evidence_refs: []});
    }
  }
  return {goals, nodes, edges};
}

export function scopeSkeleton(graph, goalId) {
  if (!goalId) return {...graph, focus: new Set(graph.nodes.map(n => n.id))};
  const goals = new Set([goalId]);
  for (let i = 0; i < graph.goals.length; i++) for (const g of graph.goals) if (goals.has(g.parent_goal_id)) goals.add(g.id);
  const focus = new Set(graph.nodes.filter(n => goals.has(n.goal_id)).map(n => n.id));
  const visible = new Set(focus);
  for (const edge of graph.edges) if (focus.has(edge.source_id) || focus.has(edge.target_id)) {
    visible.add(edge.source_id); visible.add(edge.target_id);
  }
  return {...graph, focus, nodes: graph.nodes.filter(n => visible.has(n.id)),
    edges: graph.edges.filter(e => visible.has(e.source_id) && visible.has(e.target_id))};
}

/** Longest-path DAG ranks; siblings share a layer and converging paths reunite. */
export function layoutSkeleton(nodes, edges) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const incoming = new Map(nodes.map(n => [n.id, []])), outgoing = new Map(nodes.map(n => [n.id, []]));
  for (const e of edges) if (byId.has(e.source_id) && byId.has(e.target_id) && e.source_id !== e.target_id) {
    incoming.get(e.target_id).push(e.source_id); outgoing.get(e.source_id).push(e.target_id);
  }
  const indegree = new Map(nodes.map(n => [n.id, incoming.get(n.id).length]));
  const ranks = new Map(nodes.map(n => [n.id, 0]));
  const queue = nodes.filter(n => indegree.get(n.id) === 0).map(n => n.id), visited = new Set();
  while (queue.length) {
    const id = queue.shift(); visited.add(id);
    for (const target of outgoing.get(id)) {
      ranks.set(target, Math.max(ranks.get(target), ranks.get(id) + 1));
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    }
  }
  const invalid = nodes.filter(n => !visited.has(n.id)).map(n => n.id);
  const isolated = nodes.filter(n => !incoming.get(n.id).length && !outgoing.get(n.id).length && n.kind !== 'root');
  const isolatedIds = new Set(isolated.map(n => n.id));
  const levels = new Map();
  for (const node of nodes) if (visited.has(node.id) && !isolatedIds.has(node.id)) {
    const rank = ranks.get(node.id); if (!levels.has(rank)) levels.set(rank, []); levels.get(rank).push(node.id);
  }
  let next = Math.max(-1, ...levels.keys()) + 1;
  for (let i = 0; i < isolated.length; i += 3) {
    const row = isolated.slice(i, i + 3).map(n => n.id); levels.set(next, row); for (const id of row) ranks.set(id, next); next++;
  }
  // Crossing reduction using parent order, with deterministic source-order ties.
  const columns = new Map();
  for (const rank of [...levels.keys()].sort((a, b) => a - b)) {
    const row = levels.get(rank);
    const center = id => {const xs = incoming.get(id).map(p => columns.get(p)).filter(x => x != null); return xs.length ? xs.reduce((a,b) => a+b,0)/xs.length : index.get(id);};
    row.sort((a,b) => center(a) - center(b) || index.get(a) - index.get(b));
    row.forEach((id, i) => columns.set(id, i));
  }
  const nodeWidth = 310, nodeHeight = 150, gapX = 125, gapY = 130;
  const widest = Math.max(1, ...[...levels.values()].map(r => r.length));
  const bodyWidth = Math.max(760, widest * (nodeWidth + gapX) - gapX + 80);
  const positions = new Map();
  for (const [rank, row] of levels) {
    const left = (bodyWidth - (row.length * (nodeWidth + gapX) - gapX)) / 2;
    row.forEach((id, i) => positions.set(id, {x: left + i * (nodeWidth + gapX), y: 30 + rank * (nodeHeight + gapY), rank, width: nodeWidth, height: nodeHeight}));
  }
  const longEdges = edges.filter(e => positions.has(e.source_id) && positions.has(e.target_id) && ranks.get(e.target_id) - ranks.get(e.source_id) > 1);
  return {positions, width: bodyWidth + (longEdges.length ? 230 : 0), height: Math.max(220, 30 + levels.size * (nodeHeight + gapY)),
    bodyWidth, invalid, isolated: isolatedIds, longEdges};
}
