import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {EvaluationView} from '../../src/loom/web/assets/views/evaluation.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const selectedRecord = view => view.historyList.querySelector('[aria-current="true"]');
const ids = view => [...view.historyList.querySelectorAll('[data-evaluation-id]')].map(node => node.dataset.evaluationId);
const select = (view, id) => [...view.historyList.querySelectorAll('button')].find(node => node.dataset.evaluationId === id).click();
async function fixture(t) {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const abort = new AbortController(); t.after(() => {abort.abort(); dom.window.close();});
  const settings = {max_calls: 10, max_seconds: 1800, max_tokens: 10000};
  const base = {state: 'completed', model: 'judge', settings, usage: {calls: 0, total_tokens: 0}, created_at: '2026-10-24T03:00:00Z', analysis_version: 'goal_graph_v1'};
  const jobs = [
    {...base, id: 'evaluation-one', run_id: 'run-one'}, // Newest overall is NOT in the selected run.
    {...base, id: 'evaluation-two', run_id: 'run-two'},
    {...base, id: 'evaluation-two-again', run_id: 'run-two', state: 'incomplete'},
    {...base, id: 'evaluation-legacy', analysis_version: 'v3'},
    {...base, id: 'evaluation-missing'},
    {...base, id: 'evaluation-archived', run_id: 'run-archived'},
  ];
  const starts = [];
  const api = {
    evaluations: async () => ({supports_delete: true, models: [{id: 'judge', label: 'Judge'}], default_model: 'judge',
      analysis_versions: ['goal_graph_v1', 'v3'], default_analysis_version: 'goal_graph_v1', run_ids: ['run-empty', 'run-one', 'run-two'],
      defaults: settings, bounds: Object.fromEntries(Object.keys(settings).map(key => [key, [1, 1000000]])), jobs: [...jobs]}),
    evaluation: async (_s, _a, id) => jobs.find(job => job.id === id),
    startEvaluation: async (_s, _a, options) => {
      starts.push(options);
      if (options.resume_id) return jobs.find(job => job.id === options.resume_id);
      const job = {...base, id: `evaluation-new-${starts.length}`, run_id: options.run_id};
      jobs.push(job); return job;
    },
  };
  const view = new EvaluationView(document.querySelector('main'), {api, sessionId: 's', analysisId: 'a', signal: abort.signal});
  await view.open(); await tick();
  const scope = id => {view.runSelect.value = id; view.runSelect.dispatchEvent(new dom.window.Event('change'));};
  return {view, jobs, api, starts, scope};
}

test('scope filters history before selection; clicking a record shows its detail', async t => {
  const {view, scope} = await fixture(t);
  assert.equal(view.root.querySelector('select[aria-label="Evaluation history"]'), null);
  assert.doesNotMatch(view.root.textContent, /Saved evaluations/);
  assert.deepEqual(ids(view), ['evaluation-two', 'evaluation-two-again']);
  assert.equal(view.job.id, 'evaluation-two');
  assert.equal(view.start.hidden, true); // Completed records cannot resume.
  assert.match(view.historyList.textContent, /judge.*completed/);
  select(view, 'evaluation-two-again'); await tick();
  assert.match(selectedRecord(view).textContent, /Evaluationevaluation-two-again/);
  assert.equal(view.historyList.querySelector('[aria-current="true"]').dataset.evaluationId, 'evaluation-two-again');
  assert.equal(view.start.hidden, true);
  assert.ok(view.historyList.querySelector('[aria-label="Resume evaluation evaluation-two-again"]'));
  scope('run-one');
  assert.deepEqual(ids(view), ['evaluation-one']);
  assert.equal(view.job, null);
  assert.equal(selectedRecord(view), null);
  assert.equal(view.toolbar.hidden, false);
  assert.equal(view.start.hidden, true);
  select(view, 'evaluation-one'); await tick();
  assert.match(selectedRecord(view).textContent, /run-one/);
  assert.doesNotMatch(selectedRecord(view).textContent, /run-two/);
});

test('resume keeps selected identity; new evaluations create separate records in the same run', async t => {
  const {view, starts} = await fixture(t);
  select(view, 'evaluation-two-again'); await tick();
  view.fields.max_seconds.value = '3600';
  view.historyList.querySelector('[aria-label="Resume evaluation evaluation-two-again"]').click();
  await tick();
  assert.equal(starts[0].resume_id, 'evaluation-two-again');
  assert.equal(starts[0].new_evaluation, undefined);
  assert.equal(starts[0].max_seconds, 3600);
  assert.equal(view.model.disabled, true);
  view.fresh.click();
  assert.equal(view.runSelect.value, 'run-two');
  assert.equal(view.selectedHistoryId, null);
  assert.equal(selectedRecord(view), null);
  assert.equal(view.start.textContent, 'Create evaluation');
  await view.run();
  assert.equal(starts[1].new_evaluation, true);
  assert.equal(starts[1].resume_id, undefined);
  assert.equal(starts[1].run_id, 'run-two');
  assert.equal(view.job.id, 'evaluation-new-2');
  assert.deepEqual(ids(view), ['evaluation-new-2', 'evaluation-two', 'evaluation-two-again']);
  view.fresh.click(); await view.run();
  assert.equal(view.job.id, 'evaluation-new-3');
  assert.equal(ids(view).length, 4);
});

test('empty, legacy, missing and archived scopes never mix records from another run', async t => {
  const {view, scope} = await fixture(t);
  scope('run-empty');
  assert.deepEqual(ids(view), []);
  assert.match(view.historyList.textContent, /No evaluations in this scope/);
  view.fresh.click();
  assert.equal(view.runSelect.value, 'run-empty');
  scope('');
  assert.deepEqual(ids(view), ['evaluation-legacy']);
  select(view, 'evaluation-legacy'); await tick();
  assert.match(selectedRecord(view).textContent, /Snapshot scope/);
  assert.equal(view.runLabel.hidden, false);
  scope('__missing_run__');
  assert.deepEqual(ids(view), ['evaluation-missing']);
  assert.equal(view.fresh.disabled, true);
  select(view, 'evaluation-missing'); await tick();
  assert.match(selectedRecord(view).textContent, /Run ID was not recorded/);
  scope('run-archived');
  assert.deepEqual(ids(view), ['evaluation-archived']);
  assert.equal(view.fresh.disabled, false);
  select(view, 'evaluation-archived'); await tick();
  assert.match(selectedRecord(view).textContent, /run-archived/);
});

test('a late history response cannot restore the old run after scope changes', async t => {
  const {view, api, jobs, scope} = await fixture(t);
  let finish;
  api.evaluation = () => new Promise(resolve => {finish = resolve;});
  const pending = view.follow('evaluation-two-again');
  scope('run-one');
  finish(jobs.find(job => job.id === 'evaluation-two-again'));
  await pending;
  assert.equal(view.runSelect.value, 'run-one');
  assert.equal(view.job, null);
  assert.equal(selectedRecord(view), null);
  assert.deepEqual(ids(view), ['evaluation-one']);
});

test('a failed detail load cannot turn Resume into a new evaluation', async t => {
  const {view, api, starts} = await fixture(t);
  api.evaluation = async () => {throw new Error('Detail unavailable');};
  await view.follow('evaluation-two-again');
  await view.run();
  assert.equal(starts.length, 0);
  assert.match(view.status.textContent, /Detail unavailable/);
});

test('pending creation prevents duplicate submissions and history changes', async t => {
  const {view, api} = await fixture(t);
  let finish, calls = 0;
  api.startEvaluation = () => {calls++; return new Promise((_resolve, reject) => {finish = reject;});};
  view.fresh.click();
  const pending = view.run();
  await view.run();
  assert.equal(calls, 1);
  assert.equal(view.runSelect.disabled, true);
  assert.equal(view.fresh.disabled, true);
  assert.ok([...view.historyList.querySelectorAll('button')].every(node => node.disabled));
  finish(new Error('Service unavailable'));
  await pending;
  assert.equal(view.runSelect.disabled, false);
  assert.equal(view.fresh.disabled, false);
  assert.equal(view.start.disabled, false);
  assert.equal(view.selectedHistoryId, null);
  assert.match(view.status.textContent, /Service unavailable/);
});


test('toolbar actions are buttons and each horizontal history row contains its complete identity and aligned actions', async t => {
  const {view, scope} = await fixture(t);
  for (const control of [view.runSelect, view.model, view.fresh, view.start]) {
    assert.equal(control.closest('.deep-toolbar'), view.toolbar);
  }
  assert.equal(view.form.firstElementChild, view.toolbar);
  assert.equal(view.root.querySelectorAll('.deep-toolbar').length, 1);
  assert.equal(view.historyList.closest('.dashboard-panel'), view.historyPanel);
  assert.equal(view.root.querySelector('.evaluation-identity'), null);
  assert.doesNotMatch(view.root.textContent, /Viewing saved evaluation/);
  assert.equal(view.fresh.classList.contains('quiet'), false);
  for (const item of view.historyList.children) {
    const record = item.querySelector('.evaluation-history-record');
    const fields = [...record.querySelectorAll('.evaluation-history-field')];
    assert.deepEqual(fields.map(field => field.firstChild.textContent), ['Run 3', 'Evaluation', 'Created', 'Judge model']);
    assert.equal(fields[0].lastChild.textContent, 'run-two');
    assert.equal(fields[1].lastChild.textContent, record.dataset.evaluationId);
    assert.ok(fields[2].lastChild.textContent.length > 0);
    assert.equal(fields[3].lastChild.textContent, 'judge');
    assert.equal(item.children[1].className, 'evaluation-history-status');
    const actions = item.querySelector('.evaluation-history-actions');
    const remove = actions.querySelector('.evaluation-history-delete');
    assert.equal(remove.tagName, 'BUTTON');
    assert.equal(remove.textContent, 'Delete');
    assert.equal(remove.classList.contains('quiet'), false);
  }
  assert.equal(view.historyPanel.querySelectorAll('.dashboard-panel').length, 0);
  select(view, 'evaluation-two-again'); await tick();
  assert.equal(view.start.hidden, true);
  assert.ok(view.historyList.querySelector('[aria-label="Resume evaluation evaluation-two-again"]'));
  assert.equal(view.toolbar.hidden, false);
  scope('run-one');
  assert.equal(view.toolbar.hidden, false);
  assert.equal(view.fresh.disabled, false);
  view.fresh.click();
  assert.equal(view.start.textContent, 'Create evaluation');
  assert.equal(view.start.hidden, false);
});


test('row Resume selects and resumes that record, with Delete as its sibling', async t => {
  const {view, starts} = await fixture(t);
  assert.equal(view.job.id, 'evaluation-two');
  const resume = view.historyList.querySelector('[aria-label="Resume evaluation evaluation-two-again"]');
  assert.equal(resume.nextElementSibling.getAttribute('aria-label'), 'Delete evaluation evaluation-two-again');
  assert.equal(view.historyList.querySelector('[aria-label="Resume evaluation evaluation-two"]'), null);
  resume.click(); await tick();
  assert.equal(starts.length, 1);
  assert.equal(starts[0].resume_id, 'evaluation-two-again');
  assert.equal(starts[0].new_evaluation, undefined);
  assert.equal(view.selectedHistoryId, 'evaluation-two-again');
  assert.equal(view.start.hidden, true);
});

test('changing scope during row Resume loading cancels the pending resume', async t => {
  const {view, api, jobs, starts, scope} = await fixture(t);
  let finish;
  api.evaluation = () => new Promise(resolve => {finish = resolve;});
  const pending = view.resumeEvaluation('evaluation-two-again');
  scope('run-one');
  finish(jobs.find(job => job.id === 'evaluation-two-again'));
  await pending;
  assert.equal(starts.length, 0);
  assert.equal(view.runSelect.value, 'run-one');
});

test('a failed row detail load does not resume another record or create a new evaluation', async t => {
  const {view, api, starts} = await fixture(t);
  api.evaluation = async () => {throw new Error('Detail unavailable');};
  await view.resumeEvaluation('evaluation-two-again');
  assert.equal(starts.length, 0);
  assert.match(view.status.textContent, /Detail unavailable/);
  assert.equal(view.resumingId, null);
});
