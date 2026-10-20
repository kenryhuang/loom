import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {SessionApi} from "../../src/loom/web/assets/api.mjs";
import {EvaluationView} from "../../src/loom/web/assets/views/evaluation.mjs";

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
  await view.deleteSelected();
  assert.equal(deleted.length, 0);
  assert.equal(view.job.id, job.id);
  assert.match(confirmation, /evaluation-one/);
  assert.match(confirmation, /checkpoint/);
  dom.window.confirm = () => true;
  await view.deleteSelected();
  assert.deepEqual(deleted[0], ["session-one", "snapshot-one", job.id, controller.signal]);
  assert.deepEqual([...view.history.options].map(option => option.value), ["", other.id]);
  assert.deepEqual(view.catalog.jobs.map(row => row.id), [other.id]);
  assert.equal(view.history.value, "");
  assert.equal(view.job, null);
  assert.equal(results.at(-1), null);
  assert.equal(view.graphView, null);
  assert.equal(view.progress.hidden, true);
  assert.equal(view.deleteButton.hidden, true);
  assert.match(view.results.textContent, /No evaluation selected/);
  assert.match(view.status.textContent, /permanently deleted/);
  assert.equal(view.start.disabled, false);
  await view.follow(other.id);
  assert.equal(view.job.id, other.id);
  assert.equal(view.deleteButton.disabled, false);
});

for (const state of ["queued", "running"]) {
  test(`cannot delete a ${state} evaluation until it stops`, async t => {
    const {view, job, deleted} = await fixture(t, state);
    assert.equal(view.deleteButton.disabled, true);
    assert.match(view.deleteButton.title, /Cancel/);
    await view.deleteSelected();
    assert.equal(deleted.length, 0);
    job.state = "cancelled";
    await view.follow(job.id);
    assert.equal(view.deleteButton.disabled, false);
    await view.deleteSelected();
    assert.equal(deleted.length, 1);
  });
}

test("failed deletion retains the selected evaluation for retry and unlocks controls", async t => {
  const {api, view, job} = await fixture(t, "incomplete");
  let finish;
  api.deleteEvaluation = () => new Promise((_resolve, reject) => { finish = reject; });
  const pending = view.deleteSelected();
  assert.equal(view.history.disabled, true);
  assert.equal(view.deleteButton.disabled, true);
  assert.equal(view.start.disabled, true);
  assert.equal(view.fresh.disabled, true);
  finish(new Error("File cleanup pending. Retry deletion."));
  await pending;
  assert.equal(view.job.id, job.id);
  assert.equal(view.history.value, job.id);
  assert.equal(view.history.disabled, false);
  assert.equal(view.deleteButton.disabled, false);
  assert.equal(view.start.disabled, false);
  assert.match(view.status.textContent, /cleanup pending/);
  api.deleteEvaluation = async () => ({deleted: true});
  await view.deleteSelected();
  assert.equal(view.job, null);
});

test("cannot delete a previous selection while the new selection is loading", async t => {
  const {view, job, other, deleted} = await fixture(t);
  view.history.value = other.id;
  assert.equal(view.job.id, job.id);
  await view.deleteSelected();
  assert.equal(deleted.length, 0);
});

test("an older service does not expose a delete action it cannot execute", async t => {
  const {view, job, deleted} = await fixture(t);
  delete view.catalog.supports_delete;
  await view.follow(job.id);
  assert.equal(view.deleteButton.hidden, true);
  await view.deleteSelected();
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
