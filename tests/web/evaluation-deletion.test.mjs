import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {SessionApi} from "../../src/loom/web/assets/api.mjs";
import {EvaluationView} from "../../src/loom/web/assets/views/evaluation.mjs";

const removeButton = (view, id) => view.historyList.querySelector(`[aria-label="Delete evaluation ${id}"]`);

async function fixture(t, state = "completed") {
  const dom = new JSDOM('<section id="root"></section>');
  globalThis.document = dom.window.document;
  const controller = new AbortController();
  t.after(() => controller.abort());
  const settings = {max_calls: 10, max_tokens: 1000, max_seconds: 1800, max_evidence_chars: 262144};
  const job = {id: "evaluation-one", state, model: "judge", settings, usage: {calls: 1, total_tokens: 50}};
  const other = {...job, id: "evaluation-two", state: "completed"};
  const deleted = [], results = [];
  const api = {
    evaluations: async () => ({
      supports_delete: true,
      models: [{id: "judge", label: "Judge"}], default_model: "judge", defaults: settings,
      bounds: Object.fromEntries(Object.keys(settings).map(k => [k, [1, 1000000]])), jobs: [job, other],
    }),
    evaluation: async (_sid, _aid, id) => id === job.id ? job : other,
    deleteEvaluation: async (...args) => { deleted.push(args); return {id: args[2], deleted: true}; },
  };
  const view = new EvaluationView(document.querySelector("#root"), {
    api, sessionId: "session-one", analysisId: "snapshot-one", signal: controller.signal, onResult: value => results.push(value),
  });
  await view.open();
  await new Promise(resolve => setTimeout(resolve, 0));
  dom.window.confirm = () => true;
  return {dom, controller, job, other, api, view, deleted, results};
}

test("delete confirms the selected evaluation, removes history and clears displayed results", async t => {
  const {dom, controller, job, other, view, deleted, results} = await fixture(t);
  let confirmation;
  dom.window.confirm = message => { confirmation = message; return false; };
  await view.deleteEvaluation(job.id);
  assert.equal(deleted.length, 0);
  assert.equal(view.job.id, job.id);
  assert.match(confirmation, /evaluation-one/);
  assert.match(confirmation, /checkpoint/);
  dom.window.confirm = () => true;
  await view.deleteEvaluation(job.id);
  assert.deepEqual(deleted[0], ["session-one", "snapshot-one", job.id, controller.signal]);
  assert.deepEqual([...view.historyList.querySelectorAll("[data-evaluation-id]")].map(button => button.dataset.evaluationId), [other.id]);
  assert.deepEqual(view.catalog.jobs.map(row => row.id), [other.id]);
  assert.equal(view.selectedHistoryId, null);
  assert.equal(view.job, null);
  assert.equal(results.at(-1), null);
  assert.equal(view.graphView, null);
  assert.equal(view.progress.hidden, true);
  assert.equal(removeButton(view, job.id), null);
  assert.match(view.results.textContent, /No evaluation selected/);
  assert.match(view.status.textContent, /permanently deleted/);
  assert.equal(view.start.disabled, false);
  await view.follow(other.id);
  assert.equal(view.job.id, other.id);
  assert.equal(removeButton(view, other.id).disabled, false);
});

for (const state of ["queued", "running"]) {
  test(`cannot delete a ${state} evaluation until it stops`, async t => {
    const {view, job, deleted} = await fixture(t, state);
    assert.equal(removeButton(view, job.id).disabled, true);
    assert.match(removeButton(view, job.id).title, /Cancel/);
    await view.deleteEvaluation(job.id);
    assert.equal(deleted.length, 0);
    job.state = "cancelled";
    await view.follow(job.id);
    assert.equal(removeButton(view, job.id).disabled, false);
    await view.deleteEvaluation(job.id);
    assert.equal(deleted.length, 1);
  });
}

test("failed deletion retains the selected evaluation for retry and unlocks controls", async t => {
  const {api, view, job} = await fixture(t, "incomplete");
  let finish;
  api.deleteEvaluation = () => new Promise((_resolve, reject) => { finish = reject; });
  const pending = view.deleteEvaluation(job.id);
  assert.equal([...view.historyList.querySelectorAll("button")].every(button => button.disabled), true);
  assert.equal(removeButton(view, job.id).disabled, true);
  assert.equal(view.start.disabled, true);
  assert.equal(view.fresh.disabled, true);
  finish(new Error("File cleanup pending. Retry deletion."));
  await pending;
  assert.equal(view.job.id, job.id);
  assert.equal(view.selectedHistoryId, job.id);
  assert.equal([...view.historyList.querySelectorAll("button")].some(button => button.disabled), false);
  assert.equal(removeButton(view, job.id).disabled, false);
  assert.equal(view.start.disabled, false);
  assert.match(view.status.textContent, /cleanup pending/);
  api.deleteEvaluation = async () => ({deleted: true});
  await view.deleteEvaluation(job.id);
  assert.equal(view.job, null);
});

test("deleting an unselected row preserves selected details and does not select the deleted record", async t => {
  const {view, job, other, deleted} = await fixture(t);
  const identity = view.historyList.querySelector('[aria-current="true"]').textContent;
  removeButton(view, other.id).click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(deleted[0][2], other.id);
  assert.equal(view.job.id, job.id);
  assert.equal(view.selectedHistoryId, job.id);
  assert.equal(view.historyList.querySelector('[aria-current="true"]').textContent, identity);
  assert.equal(removeButton(view, other.id), null);
  assert.ok(removeButton(view, job.id));
});

test("deleting another row retains polling of the selected running evaluation", async t => {
  const {view, job, other} = await fixture(t, "running");
  await view.deleteEvaluation(other.id);
  assert.equal(view.job.id, job.id);
  assert.equal(view.selectedHistoryId, job.id);
  assert.ok(view.timer);
  assert.ok(view.historyList.querySelector(`[aria-label="Cancel evaluation ${job.id}"]`));
});

test("deleting the loading record cannot restore it from a late detail response", async t => {
  const {view, api, job, other} = await fixture(t);
  let finish;
  api.evaluation = () => new Promise(resolve => {finish = resolve;});
  const loading = view.follow(other.id);
  await view.deleteEvaluation(other.id);
  finish(other); await loading;
  assert.equal(view.job, null);
  assert.equal(view.selectedHistoryId, null);
  assert.equal(view.historyList.querySelector('[aria-current="true"]'), null);
  assert.deepEqual(view.catalog.jobs.map(row => row.id), [job.id]);
});

test("an older service does not expose a delete action it cannot execute", async t => {
  const {view, job, deleted} = await fixture(t);
  delete view.catalog.supports_delete;
  await view.follow(job.id);
  assert.equal(removeButton(view, job.id), null);
  await view.deleteEvaluation(job.id);
  assert.equal(deleted.length, 0);
});

test("delete transport is authenticated, scoped and encodes identifiers", async () => {
  let request;
  const api = new SessionApi({token: "test-secret", fetchImpl: async (url, options) => {
    request = {url, options};
    return new Response(JSON.stringify({id: "evaluation/id", deleted: true}), {headers: {"Content-Type": "application/json"}});
  }});
  const result = await api.deleteEvaluation("session/id", "snapshot/id", "evaluation/id");
  assert.equal(result.deleted, true);
  assert.equal(request.url, "/v1/sessions/session%2Fid/trajectory/snapshot%2Fid/evaluation/evaluation%2Fid/delete");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers.Authorization, "Bearer test-secret");
  assert.equal(request.options.body, "{}");
});

for (const state of ['queued', 'running']) {
  test(`${state} rows show Cancel beside disabled Delete, and update after cancellation`, async t => {
    const {view, api, job, other, controller} = await fixture(t, state);
    await view.follow(other.id); // Cancelling a different row must not change the selection.
    const calls = [];
    let finish;
    api.cancelEvaluation = (...args) => {calls.push(args); return new Promise(resolve => {finish = resolve;});};
    const cancel = () => view.historyList.querySelector(`[aria-label="Cancel evaluation ${job.id}"]`);
    assert.equal(cancel().nextElementSibling, removeButton(view, job.id));
    assert.equal(removeButton(view, job.id).disabled, true);
    assert.equal(view.historyList.querySelector(`[aria-label="Resume evaluation ${job.id}"]`), null);
    cancel().click();
    assert.equal(cancel().disabled, true);
    await view.cancelEvaluation(job.id);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], ['session-one', 'snapshot-one', job.id, controller.signal]);
    job.state = 'cancelled'; finish(job);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(view.selectedHistoryId, other.id);
    assert.equal(cancel(), null);
    assert.equal(removeButton(view, job.id).disabled, false);
    assert.ok(view.historyList.querySelector(`[aria-label="Resume evaluation ${job.id}"]`));
  });
}

test('incomplete rows show Resume and enabled Delete; completed rows only show Delete', async t => {
  const {view, job, other} = await fixture(t, 'incomplete');
  assert.ok(view.historyList.querySelector(`[aria-label="Resume evaluation ${job.id}"]`));
  assert.equal(view.historyList.querySelector(`[aria-label="Cancel evaluation ${job.id}"]`), null);
  assert.equal(removeButton(view, job.id).disabled, false);
  assert.equal(view.historyList.querySelector(`[aria-label="Resume evaluation ${other.id}"]`), null);
  assert.equal(removeButton(view, other.id).disabled, false);
});

test('failed cancellation can be retried and never enables deletion of a running record', async t => {
  const {view, api, job} = await fixture(t, 'running');
  api.cancelEvaluation = async () => {throw new Error('Cancel unavailable');};
  await view.cancelEvaluation(job.id);
  assert.equal(view.historyList.querySelector(`[aria-label="Cancel evaluation ${job.id}"]`).disabled, false);
  assert.equal(removeButton(view, job.id).disabled, true);
  assert.match(view.status.textContent, /Cancel unavailable/);
});

test('accepted cancellation keeps Delete disabled until the server reports the record stopped', async t => {
  const {view, api, job} = await fixture(t, 'running');
  api.cancelEvaluation = async () => job;
  await view.cancelEvaluation(job.id);
  assert.equal(removeButton(view, job.id).disabled, true);
  assert.equal(view.historyList.querySelector(`[aria-label="Cancel evaluation ${job.id}"]`).disabled, true);
  assert.ok(view.cancelTimers.has(job.id));
  clearTimeout(view.cancelTimers.get(job.id));
  job.state = 'cancelled';
  await view.refreshCancellation(job.id);
  assert.equal(removeButton(view, job.id).disabled, false);
  assert.equal(view.cancelTimers.has(job.id), false);
  assert.equal(view.job.state, 'cancelled');
});
