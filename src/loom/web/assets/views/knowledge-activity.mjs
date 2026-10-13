import {element} from '../markdown.mjs';
import {KnowledgeProgress} from './knowledge-progress.mjs';

const active = job => ['queued', 'running'].includes(job.state);
const label = state => ({completed: 'Complete', failed: 'Failed', cancelled: 'Cancelled', paused: 'Paused', running: 'Running', queued: 'Queued'}[state] || state);
const timestamp = job => {
  const value = job.started_at || job.created_at;
  return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : 'Time unavailable';
};
const button = text => {const node = element('button', 'quiet', text); node.type = 'button'; return node;};

// Compatibility with service versions that only provide the recent jobs list.
export function groupJobs(jobs = []) {
  const groups = new Map();
  const sorted = [...jobs].sort((a, b) => Number(active(b) || b.state === 'paused') - Number(active(a) || a.state === 'paused')
    || String(b.started_at || b.created_at || '').localeCompare(String(a.started_at || a.created_at || '')));
  for (const job of sorted) {
    const key = `${job.kind || 'document'}:${job.source_id || job.document || job.id}`;
    if (!groups.has(key)) groups.set(key, {id: key, kind: job.kind, source: job.document, source_id: job.source_id, latest: job, history: []});
    else groups.get(key).history.push(job);
  }
  return [...groups.values()];
}

export class KnowledgeActivity {
  constructor(root, base, {act, notify, error}) {
    Object.assign(this, {root, act, notify, error}); this.cards = new Map();
    this.groups = base.import_activity || groupJobs(base.jobs);
    for (const group of this.groups) this.create(group);
    if (!this.groups.length) root.append(element('p', 'muted', 'No imports yet.'));
    else root.append(element('p', 'muted small', 'Clear removes only the selected attempt record. Indexes, source files and recovery checkpoints are retained.'));
    this.update(this.latestJobs);
  }
  get latestJobs() {return this.groups.map(group => group.latest);}
  async invoke(path, control) {
    control.disabled = true;
    try {await this.act(path);} catch (error) {this.error(error); control.disabled = false;}
  }
  create(group) {
    const root = element('article', 'kb-job-card kb-import-source'); this.root.append(root);
    const heading = element('div', 'kb-import-heading');
    heading.append(element('h3', '', {website: 'Web URL', local: 'Local import', document: 'Document import', delete: 'Index removal'}[group.kind] || 'Import'));
    const latestTime = element('span', 'muted small'); heading.append(latestTime); root.append(heading);
    const metadata = element('dl', 'kb-import-metadata'); root.append(metadata);
    const line = (parent, title, value, url = false) => {
      const row = element('div'), content = element('dd');
      if (url && /^https?:\/\//i.test(value)) {
        const link = element('a', '', value); link.href = value; link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link);
      } else content.append(element('code', '', value));
      row.append(element('dt', 'muted', title), content); parent.append(row);
    };
    line(metadata, group.kind === 'website' ? 'Source URL' : group.kind === 'local' ? 'Source path' : 'Source', group.source || 'Unknown');
    if (group.path_scope) line(metadata, 'Path scope', group.path_scope);
    line(metadata, 'Index directory · shared by this knowledge base', group.index_directory || 'Available after restarting Loom service');
    const storagePaths = [['Published index', group.published_directory], ['Index database', group.index_file],
      ['Recovery checkpoints', group.checkpoint_directory], ['Crawl snapshot', group.snapshot_file]];
    if (storagePaths.some(([, value]) => value)) {
      const storage = element('details', 'kb-import-storage'), list = element('dl', 'kb-import-metadata');
      storage.append(element('summary', '', 'Storage details'), list);
      for (const [title, value] of storagePaths) if (value) line(list, title, value);
      root.append(storage);
    }
    const controls = element('div', 'knowledge-progress-actions'); root.append(controls);
    const actions = {};
    for (const [name, title] of [['pause', 'Pause'], ['resume', 'Resume'], ['terminate', 'Terminate'], ['clear', 'Clear']]) {
      const control = button(title); actions[name] = control; controls.append(control);
      if (name === 'clear') control.title = 'Clear this attempt record only; keep indexes and checkpoints';
      control.onclick = () => {
        const job = group.latest;
        const route = name === 'resume' && group.source_id && (job.state === 'cancelled' || job.resumable === false)
          ? `/sources/${group.source_id}/sync` : `/jobs/${job.id}/${name}`;
        return this.invoke(route, control);
      };
    }
    const cancel = button(''); cancel.style.display = 'none';
    const progress = new KnowledgeProgress({cancel, resume: async () => {}, error: this.error});
    progress.retry.style.display = 'none'; progress.source.hidden = true; root.append(progress.root);
    const history = element('details', 'kb-import-history');
    history.append(element('summary', '', `History · ${group.history.length} previous attempts`));
    history.hidden = !group.history.length; root.append(history);
    for (const job of group.history) {
      const row = element('details', 'kb-import-attempt'), summary = element('summary');
      const state = job.state === 'completed' && job.stage === 'partial' ? 'Partial sync' : label(job.state);
      summary.append(element('span', '', timestamp(job)), element('span', `kb-attempt-state ${job.state}`, state));
      if (Number.isFinite(job.elapsed_seconds)) summary.append(element('span', 'muted', `${Math.round(job.elapsed_seconds)}s`));
      row.append(summary, element('code', 'muted small', job.id));
      if (job.checkpointed != null) row.append(element('p', 'small', `${job.checkpointed} document checkpoints at the end of this attempt`));
      if (job.error) row.append(element('p', 'error small', job.error));
      const clear = button('Clear'); clear.title = 'Clear this historical attempt only';
      clear.disabled = active(job) || job.state === 'paused';
      clear.onclick = () => this.invoke(`/jobs/${job.id}/clear`, clear);
      row.append(clear); history.append(row);
    }
    this.cards.set(group.id, {group, root, progress, actions, latestTime});
  }
  update(jobs, notify = true) {
    for (const job of jobs) {
      const card = [...this.cards.values()].find(card => card.group.latest.id === job.id);
      if (!card) continue;
      card.group.latest = job;
      card.latestTime.textContent = `Latest attempt · ${timestamp(job)}`;
      card.progress.update(job); card.progress.title.textContent = 'Latest status';
      card.progress.source.hidden = true;
      card.actions.pause.disabled = !active(job) || job.cancel_requested;
      const savedWebsite = card.group.source_id && ['failed', 'cancelled'].includes(job.state)
        && (job.checkpointed || job.crawl_checkpoint || job.snapshot_saved || card.group.checkpoint_directory || card.group.snapshot_file);
      card.actions.resume.disabled = !savedWebsite && (!['paused', 'failed'].includes(job.state) || job.resumable === false);
      card.actions.terminate.disabled = !active(job) && !['paused', 'failed'].includes(job.state);
      card.actions.clear.disabled = active(job) || job.state === 'paused';
      if (job.requested_action) card.latestTime.textContent += ` · ${job.requested_action} requested`;
      if (notify) this.notify(job);
    }
  }
  connectionError(error) {for (const card of this.cards.values()) if (active(card.group.latest)) card.progress.connectionError(error);}
}
