import {element} from '../markdown.mjs';
import {badge, panel, table} from './dashboard.mjs';

const ratings = {key: ['★ Key contribution', 'passed'], supporting: ['↑ Supporting', ''], none: ['○ No observed progress', ''],
  hindering: ['↓ Hindering', 'failed'], mixed: ['↕ Mixed', ''], unknown: ['? Unknown', '']};
const fields = {scope: 'Applies to', confidence: 'Confidence', revalidate_when: 'Revalidate when', trigger: 'Trigger',
  failed_approach: 'Failed approach', failure_reason: 'Failure / known cause', better_approach: 'Better approach',
  preconditions: 'Preconditions', avoid: 'Avoid', expected_benefit: 'Expected benefit', preserve: 'Checks to preserve'};

export function renderTraceMemory(view, root) {
  const memory = view.data.memory;
  const title = element('div', 'dashboard-toolbar');
  title.append(element('h3', '', 'Memory · Experience from this trace'), badge(memory?.status || 'Not extracted'));
  root.append(title);
  if (!memory || memory.status === 'not_requested') {
    root.append(element('p', 'empty-state', 'Memory was not extracted for this evaluation. Start a new incremental graph evaluation with “Extract memory” enabled.'));
    return;
  }
  if (memory.status === 'pending') {
    root.append(element('p', 'notice', 'Memory extraction begins after the goal and stage review completes.'));
    return;
  }
  const coverage = memory.coverage;
  root.append(element('p', 'muted', `${coverage.completed_windows} / ${coverage.total_windows} memory windows complete · ${coverage.evidence_chars.toLocaleString()} evidence characters`));
  if (coverage.processed_actions != null) root.append(element('p', 'muted',
    `${coverage.processed_actions} / ${coverage.source_actions} actions processed · ${coverage.published_records} available records · ${coverage.quarantined_records} quarantined · ${coverage.stale_records} stale · ${coverage.unknown_contributions} unknown contributions`));
  if (memory.status !== 'complete') root.append(element('p', 'notice', memory.execution_complete
    ? 'Processing finished with quality gaps. Available records remain usable; quarantined and stale records are excluded from session memory. Use Retry failed items on this history record.'
    : 'Partial memory: saved records remain available. Resume the evaluation to finish remaining windows.'));
  const goals = new Map((view.data.goals || []).map(g => [g.id, g]));
  const stages = new Map((view.data.stages || []).map(s => [s.id, s]));
  const name = id => {const row = goals.get(id) || stages.get(id); return row ? `${row.display_id || ''} · ${row.objective || row.title}` : id;};
  const controls = element('div', 'goal-tree-controls'), select = element('select');
  select.setAttribute('aria-label', 'Filter memory by goal');
  for (const [value, text] of [['all', 'All goals'], ...[...goals].map(([id]) => [id, name(id)])]) {
    const option = element('option', '', text); option.value = value; select.append(option);
  }
  select.value = view.memoryGoal || 'all';
  select.addEventListener('change', () => {view.memoryGoal = select.value; view.render();});
  controls.append(select); root.append(controls);
  const selected = new Set([select.value]);
  let changed = true;
  while (changed) {changed = false; for (const g of goals.values()) if (selected.has(g.parent_goal_id) && !selected.has(g.id)) {selected.add(g.id); changed = true;}}
  const matches = ids => select.value === 'all' || ids.some(id => selected.has(id));
  const layout = element('div', 'trace-memory'); root.append(layout);
  const contribution = panel(layout, 'Stage contributions', true);
  contribution.append(element('p', 'muted small', 'Qualitative contribution to the owning goal. Ratings do not add up to a percentage; unknown and partial evidence remain explicit.'));
  for (const row of memory.contributions.filter(r => matches([r.goal_id]))) {
    const title = element('span', 'memory-card-heading');
    title.append(badge(...(ratings[row.rating] || ratings.unknown)), element('strong', '', name(row.stage_id)));
    if (row.partial) title.append(badge('Partial evidence'));
    view.details(contribution, `memory-stage:${row.stage_id}`, title, body => {
      body.append(element('p', '', `Contributes to: ${name(row.goal_id)}`));
      const goal = goals.get(row.goal_id);
      if (goal?.root_relation) body.append(element('p', 'muted', `Parent goal relevance: ${goal.root_relation}`));
      if (!row.slices.length) body.append(element('p', 'muted', 'No contribution assessment saved for this stage.'));
      for (const slice of row.slices) {
        body.append(element('p', '', slice.rationale));
        if (slice.key_action_ids.length) body.append(element('p', 'muted small', `Key actions: ${slice.key_action_ids.join(', ')}`));
        view.refs(body, slice.evidence_refs);
      }
    }, false, `memory-card setup-block memory-rating-${row.rating}`);
  }
  for (const [kind, heading] of [['facts', 'Key facts'], ['lessons', 'Failure lessons'], ['workflows', 'Efficient workflows']]) {
    const rows = memory[kind].filter(r => matches(r.goal_ids));
    const section = panel(layout, `${heading} · ${rows.length}`, true);
    if (!rows.length) section.append(element('p', 'muted', 'No supported memory saved in this scope.'));
    for (const row of rows) {
      const title = element('span', 'memory-card-heading'); title.append(element('strong', '', row.title));
      const status = row.basis === 'observed' ? 'Observed in trace' : row.basis === 'inferred' ? 'Inferred' :
        row.validation === 'proposed' ? 'Proposed · untested' : row.validation === 'observed_recovery' ? 'Recovery observed' : 'Observed route';
      title.append(badge(status));
      view.details(section, `memory:${row.id}`, title, body => {
        body.append(element('p', '', row.summary));
        if (row.category) body.append(badge(row.category));
        table(body, ['Field', 'Memory'], Object.entries(fields).filter(([key]) => row[key]).map(([key, label]) => [label, row[key]]));
        if (row.steps) {const list = element('ol'); for (const step of row.steps) list.append(element('li', '', step)); body.append(list);}
        for (const step of row.provenance?.step_support || []) {
          const item = element('div', 'memory-step-support');
          item.append(element('p', 'muted small', `${step.basis === 'observed' ? 'Observed step' : 'Proposed step'}: ${step.text}`));
          view.refs(item, step.evidence_refs || []); body.append(item);
        }
        body.append(element('p', 'muted small', `Goals: ${row.goal_ids.map(name).join('; ')}`));
        if (row.stage_ids.length) body.append(element('p', 'muted small', `Stages: ${row.stage_ids.map(name).join('; ')}`));
        body.append(element('p', 'muted small', `Actions: ${row.action_ids.join(', ')}`));
        for (const citation of row.citations) body.append(element('blockquote', 'memory-quote', citation.quote));
        view.refs(body, row.evidence_refs);
      }, false, 'memory-card setup-block');
    }
  }
  view.details(root, 'memory-scope', 'Memory scope & coverage', body => {
    if (memory.analysis_epoch) body.append(element('p', 'muted small', `Memory epoch ${memory.analysis_epoch}`));
    for (const epoch of memory.previous_epochs || []) body.append(element('p', 'muted small',
      `Archived epoch ${epoch.epoch} · ${epoch.identity.version} · ${epoch.reason}`));
    for (const text of memory.limitations) body.append(element('p', 'muted small', text));
    body.append(element('p', 'muted small', `${coverage.truncated_slices} bounded / truncated evidence slices.`));
    for (const error of coverage.errors) body.append(element('p', 'notice', error));
  }, false, 'setup-block');
  if (memory.quarantine?.length) view.details(root, 'memory-quarantine', `Quarantined candidates · ${memory.quarantine.length}`, body => {
    for (const row of memory.quarantine) view.details(body, `quarantine:${row.id}`, `${row.kind} · ${row.issue.code}`, detail => {
      detail.append(element('p', '', row.issue.reason));
      table(detail, ['Field', 'Value'], [['Window', row.unit_id], ['Candidate', row.id],
        ['Field path', (row.issue.path || []).join('/')], ['Disposition', row.issue.disposition || 'quarantine']]);
    }, false, 'setup-block');
  }, false, 'setup-block');
}
