import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { KnowledgeView } from "../../src/loom/web/assets/views/knowledge.mjs";
import { SessionProjection } from "../../src/loom/web/assets/state.mjs";
const tick = () => new Promise((resolve) => setImmediate(resolve));
function setup(
  api,
  state = {
    session_id: "one",
    title: "Demo",
    task: { state: "idle", revision: 1, knowledge_base_ids: [] },
  },
  bind = async () => {},
) {
  const dom = new JSDOM("<body></body>");
  globalThis.document = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  dom.window.HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  return {
    dom,
    view: new KnowledgeView({ api: () => api, state: () => state, bind }),
  };
}
const base = {
  id: "kb",
  name: "Manual",
  engine: "sqlite_fts",
  description: "Reference",
  chunk_count: 1,
  documents: [{ id: "doc", name: "manual.md" }],
  jobs: [],
};
const catalog = {
  knowledge_bases: [base],
  embedding_profiles: [],
  engines: ["sqlite_fts", "sqlite_hybrid"],
};

test("knowledge manager renders sources, binds selected KBs and searches safely", async () => {
  const requests = [],
    bindings = [];
  const api = {
    json: async (path, options) => {
      requests.push([path, options.body]);
      if (path.endsWith("/search"))
        return {
          matches: [
            {
              document: "manual.md",
              start_line: 2,
              end_line: 4,
              source_id: "kb/doc#0",
              text: "<script>bad()</script> Evidence",
            },
          ],
        };
      return path.endsWith("/kb") ? base : catalog;
    },
  };
  const { dom, view } = setup(api, undefined, async (...args) =>
    bindings.push(args),
  );
  await view.open(true);
  const checkbox = view.dialog.querySelector('[type="checkbox"]');
  checkbox.checked = true;
  checkbox.dispatchEvent(new dom.window.Event("change"));
  view.dialog.querySelector(".knowledge-binding button").click();
  await tick();
  assert.deepEqual(bindings[0], [["kb"], "one", 1]);
  const search = view.dialog.querySelector(".knowledge-search");
  search.querySelector("input").value = "protocol";
  search.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await tick();
  assert.match(
    view.dialog.querySelector(".knowledge-hit").textContent,
    /manual.md:2–4/,
  );
  assert.match(view.dialog.textContent, /kb\/doc#0/);
  assert.equal(view.dialog.querySelector("script"), null);
  assert.deepEqual(requests.at(-1), [
    "/v1/knowledge-bases/kb/search",
    { query: "protocol", limit: 5 },
  ]);
  view.close();
  dom.window.close();
});

test("closed manager ignores late responses and busy sessions cannot change bindings", async () => {
  let resolve;
  const { dom, view } = setup({
    json: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  const pending = view.open(true);
  view.close();
  resolve(catalog);
  await pending;
  assert.equal(view.dialog.open, false);
  assert.equal(view.content.children.length, 0);
  view.client = {
    json: async (path) => (path.endsWith("/kb") ? base : catalog),
  };
  view.api = () => view.client;
  view.state = () => ({
    session_id: "one",
    task: { state: "running", knowledge_base_ids: ["kb"] },
  });
  await view.open(true);
  assert.equal(
    view.dialog.querySelector(".knowledge-binding button").disabled,
    true,
  );
  assert.equal(view.dialog.querySelector('[type="checkbox"]').disabled, true);
  view.close();
  dom.window.close();
});

test("knowledge binding events update session state without changing task progress", () => {
  const projection = new SessionProjection({
    event_cursor: 0,
    session_id: "one",
    task: { state: "idle", limits: {} },
  });
  projection.apply({
    seq: 1,
    session_id: "one",
    type: "task.knowledge.changed",
    payload: {
      knowledge_base_ids: ["kb"],
      task_spec: { tools: { collections: ["knowledge"] } },
    },
  });
  assert.deepEqual(projection.snapshot.task.knowledge_base_ids, ["kb"]);
  assert.equal(projection.snapshot.task.state, "idle");
});

test("creation is a separate page and errors retain entered fields", async () => {
  const api = {
    json: async (path, options) => {
      if (options.body) throw new Error("Unknown knowledge engine");
      return path.endsWith("/kb") ? base : catalog;
    },
  };
  const { dom, view } = setup(api);
  await view.open(false, { create: true });
  const form = view.content.querySelector(".knowledge-create-page form");
  const name = form.querySelector("input");
  name.value = "Keep my input";
  form.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await tick();
  const error = form.querySelector('[role="alert"]');
  assert.equal(error.hidden, false);
  assert.match(error.textContent, /Restart the service/);
  assert.equal(name.value, "Keep my input");
  assert.equal(form.querySelector('button[type="submit"]').disabled, false);
  assert.equal(
    form.querySelector('option[value="yakdb_local"]').disabled,
    true,
  );
  assert.equal(view.content.querySelector('.knowledge-base-panel'), null);
  assert.doesNotMatch(view.content.textContent, /Configure embedding service/);
  view.close();
});

test("LightRAG creation selects model/profile and website graph lives in main panel", async () => {
  const graphBase = {
    ...base,
    engine: "lightrag_local",
    indexing_model: "main",
    graph_stats: { entities: 2, relations: 1, chunks: 1 },
  };
  const requests = [];
  const { dom, view } = setup({
    json: async (path, options) => {
      requests.push([path, options.body]);
      if (path.endsWith("/sources"))
        return {
          sources: [
            {
              id: "web",
              url: "https://example.com/",
              path_prefix: "/",
              max_pages: 10,
              max_depth: 2,
              delay_seconds: 0.3,
              interval_hours: 0,
              enabled: true,
            },
          ],
        };
      if (path.endsWith("/graph"))
        return {
          nodes: [
            {
              id: "Loom",
              labels: ["Loom"],
              properties: {
                description: "Agent service",
                file_path: "https://example.com/",
              },
            },
          ],
          edges: [],
          is_truncated: false,
        };
      if (path.endsWith("/kb")) return graphBase;
      return {
        ...catalog,
        knowledge_bases: [graphBase],
        engines: ["sqlite_fts", "lightrag_local"],
        models: [{ id: "main", label: "Main LLM" }],
        embedding_profiles: [{ id: "emb", name: "Vectors", model: "embed" }],
      };
    },
  });
  await view.open(true);
  assert.match(view.dialog.textContent, /Website sources/);
  assert.match(view.dialog.textContent, /Sync settings/);
  const urlInput = view.dialog.querySelector(
    '.knowledge-url-form input[type="url"]',
  );
  assert.ok(urlInput);
  assert.equal(urlInput.closest("details"), null);
  assert.equal(
    view.dialog.querySelector('.knowledge-url-form button[type="submit"]')
      .textContent,
    "Import URL",
  );
  const load = [...view.dialog.querySelectorAll("button")].find(
    (b) => b.textContent === "Load graph",
  );
  load.click();
  await tick();
  assert.equal(view.dialog.querySelectorAll("svg circle").length, 1);
  view.dialog
    .querySelector("svg circle")
    .dispatchEvent(new dom.window.Event("click", { bubbles: true }));
  assert.match(view.dialog.textContent, /Agent service/);
  assert.equal(view.dialog.querySelector('.knowledge-create-page'), null);
  assert.ok(view.dialog.querySelector('#kb-pane-view .knowledge-graph'));
  await view.open(false, {create: true});
  const selects = [...view.dialog.querySelectorAll("select")];
  const engine = selects.find((s) =>
    [...s.options].some((o) => o.value === "lightrag_local"),
  );
  engine.value = "lightrag_local";
  engine.dispatchEvent(new dom.window.Event("change"));
  const model = selects.find((s) =>
    [...s.options].some((o) => o.value === "main"),
  );
  assert.equal(model.disabled, false);
  model.value = "main";
  assert.equal(model.required, true);
  view.close();
  dom.window.close();
});

test("website sync progress and failures appear beside the source", async () => {
  const job = {
    id: "job",
    source_id: "web",
    document: "https://example.com/",
    state: "running",
    stage: "crawling",
    pages: 6,
    visited: 8,
  };
  const graphBase = {
    ...base,
    engine: "lightrag_local",
    indexing_model: "main",
    jobs: [job],
  };
  const { dom, view } = setup({
    json: async (path) => {
      if (path.endsWith("/sources"))
        return {
          sources: [
            {
              id: "web",
              url: "https://example.com/",
              path_prefix: "/",
              max_pages: 10,
              max_depth: 2,
              delay_seconds: 0.3,
              interval_hours: 0,
              enabled: true,
            },
          ],
        };
      if (path.endsWith("/kb")) return graphBase;
      return { ...catalog, knowledge_bases: [graphBase] };
    },
  });
  try {
    await view.open(true);
    const panel = view.dialog.querySelector(".knowledge-job-progress");
    assert.ok(panel);
    assert.ok(
      panel.compareDocumentPosition(
        view.dialog.querySelector(".knowledge-url-import"),
      ) & dom.window.Node.DOCUMENT_POSITION_PRECEDING,
    );
    assert.equal(
      panel.querySelector(".knowledge-progress-state").textContent,
      "Running",
    );
    const row = view.dialog.querySelector(".knowledge-source");
    assert.match(
      row.textContent,
      /Crawling pages.*8 URLs visited.*6 pages collected/,
    );
    assert.ok(
      [...row.querySelectorAll("button")].find(
        (b) => b.textContent === "Syncing…",
      ).disabled,
    );
    const cancel = [...row.querySelectorAll("button")].find(
      (b) => b.textContent === "Cancel sync",
    );
    assert.equal(cancel.hidden, false);
    for (const listener of view.knowledgeJobListeners)
      listener({ ...job, state: "failed", error: "Website request failed" });
    assert.match(row.textContent, /failed.*Website request failed/);
    assert.equal(
      panel.querySelector(".knowledge-progress-state").textContent,
      "Failed",
    );
    assert.match(row.textContent, /Sync failed/);
    assert.doesNotMatch(row.textContent, /Crawling pages/);
    assert.equal(cancel.hidden, true);
    for (const listener of view.knowledgeJobListeners)
      listener({
        ...job,
        state: "failed",
        checkpointed: 4,
        total: 187,
        indexed: 4,
        snapshot_saved: true,
      });
    assert.match(row.textContent, /4 documents saved for resume/);
    assert.ok(
      [...row.querySelectorAll("button")].find(
        (b) => b.textContent === "Resume sync",
      ),
    );
    for (const listener of view.knowledgeJobListeners)
      listener({
        ...job,
        state: "completed",
        stage: "completed",
        result: {
          complete: false,
          pages: 11,
          updated: 11,
          missing_count: 8,
          pending: 20,
          incomplete_reasons: ["page_limit"],
          errors: [],
        },
      });
    assert.match(row.textContent, /Partial sync/);
    assert.match(row.textContent, /8 URLs returned 404\/410/);
    assert.match(row.textContent, /20 URLs still queued/);
    assert.doesNotMatch(
      row.querySelector(".knowledge-source-progress").textContent,
      /Sync complete/,
    );
    assert.equal(
      [...row.querySelectorAll("button")].find(
        (b) => b.textContent === "Sync now",
      ).disabled,
      false,
    );
  } finally {
    view.close();
    dom.window.close();
  }
});

test("sync polling propagates a model failure to the panel and source and stops polling", async (t) => {
  const timers = [];
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    if (!delay) return 0;
    timers.push(callback);
    return timers.length;
  });
  const job = {
    id: "job",
    kind: "website",
    source_id: "web",
    state: "running",
    stage: "embedding",
    document: "https://example.com",
    current_document: "Transformer.md",
    indexed: 3,
    total: 184,
    checkpointed: 3,
  };
  const graphBase = {
    ...base,
    engine: "lightrag_local",
    indexing_model: "main",
    jobs: [job],
  };
  const { dom, view } = setup({
    json: async (path) => {
      if (path.endsWith("/sources"))
        return { sources: [{ id: "web", url: job.document, enabled: true }] };
      if (path.endsWith("/jobs/job"))
        return {
          ...job,
          state: "failed",
          stage: "extracting_failed",
          failed_stage: "extracting",
          error:
            "Indexing model request failed (LLM_FAILED); 3 document checkpoints retained",
          finished_at: "2026-10-07T15:18:46Z",
        };
      return path.endsWith("/kb")
        ? graphBase
        : { ...catalog, knowledge_bases: [graphBase] };
    },
  });
  try {
    await view.open(true);
    assert.equal(timers.length, 1);
    await timers[0]();
    const panel = view.dialog.querySelector(".knowledge-job-progress");
    const source = view.dialog.querySelector(".knowledge-source");
    assert.equal(
      panel.querySelector(".knowledge-progress-state").textContent,
      "Failed",
    );
    assert.match(
      panel.textContent,
      /Entity extraction or merging failed.*LLM_FAILED/s,
    );
    assert.match(
      source.textContent,
      /failed.*Entity extraction or merging failed.*LLM_FAILED/s,
    );
    assert.equal(panel.querySelector("progress").value, 3);
    assert.ok(
      [...source.querySelectorAll("button")].some(
        (button) => button.textContent === "Resume sync" && !button.disabled,
      ),
    );
    assert.equal(timers.length, 1);
  } finally {
    view.close();
    dom.window.close();
  }
});

test("sync polling recovers from network errors and renders the terminal result", async (t) => {
  const timers = [];
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    if (!delay) return 0;
    timers.push({ callback, delay });
    return timers.length;
  });
  const job = {
    id: "job",
    kind: "website",
    source_id: "web",
    state: "running",
    stage: "embedding",
    document: "https://example.com",
    indexed: 3,
    total: 4,
    checkpointed: 3,
  };
  const graphBase = {
    ...base,
    engine: "lightrag_local",
    indexing_model: "main",
    jobs: [job],
  };
  let attempts = 0;
  const { dom, view } = setup({
    json: async (path) => {
      if (path.endsWith("/sources")) return { sources: [] };
      if (path.endsWith("/jobs/job")) {
        if (++attempts === 1) throw new Error("Network unavailable");
        const completed = {
          ...job,
          state: "completed",
          stage: "completed",
          indexed: 4,
          checkpointed: 4,
          result: { complete: true, chunks: 20 },
        };
        graphBase.jobs = [completed];
        return completed;
      }
      return path.endsWith("/kb")
        ? graphBase
        : { ...catalog, knowledge_bases: [graphBase] };
    },
  });
  try {
    await view.open(true);
    assert.equal(timers[0].delay, 800);
    await timers[0].callback();
    assert.match(
      view.dialog.querySelector(".knowledge-job-progress").textContent,
      /Updates interrupted.*Network unavailable.*Reconnecting automatically/s,
    );
    assert.equal(timers[1].delay, 2000);
    await timers[1].callback();
    const panel = view.dialog.querySelector(".knowledge-job-progress");
    assert.equal(
      panel.querySelector(".knowledge-progress-state").textContent,
      "Complete",
    );
    assert.equal(panel.querySelector("progress").value, 4);
    assert.equal(
      panel.querySelector(".knowledge-progress-message").hidden,
      true,
    );
    assert.equal(attempts, 2);
  } finally {
    view.close();
    dom.window.close();
  }
});

test('local import submits paths and job controls stay inside Import', async () => {
  const requests = [];
  const paused = {...base, jobs: [{id: 'job', kind: 'local', document: '/docs', state: 'paused', stage: 'paused'}]};
  const {dom, view} = setup({json: async (path, options) => {
    requests.push([path, options?.body]);
    if (path.endsWith('/sources')) return {sources: []};
    if (path.endsWith('/kb')) return paused;
    return catalog;
  }});
  try {
    await view.open();
    assert.equal(view.dialog.querySelector('.knowledge-create-page'), null);
    const pane = view.dialog.querySelector('#kb-pane-import');
    assert.ok(pane.querySelector('.kb-import-jobs'));
    const form = pane.querySelector('form'); form.querySelector('input').value = '/docs/guide.md';
    form.dispatchEvent(new dom.window.Event('submit', {cancelable: true})); await tick();
    assert.ok(requests.some(([path, body]) => path.endsWith('/imports') && body.path === '/docs/guide.md' && body.kind === 'file'));
    const resume = [...view.dialog.querySelectorAll('.kb-job-card > .knowledge-progress-actions button')].find(b => b.textContent === 'Resume');
    assert.equal(resume.disabled, false); resume.click(); await tick();
    assert.ok(requests.some(([path]) => path.endsWith('/jobs/job/resume')));
    const tab = view.dialog.querySelector('#kb-tab-view'); tab.click();
    assert.equal(view.dialog.querySelector('#kb-pane-import').hidden, true);
    assert.equal(view.dialog.querySelector('#kb-pane-view').hidden, false);
  } finally {view.close(); dom.window.close();}
});

test('file and folder choosers fill editable paths without importing on selection or cancel', async () => {
  const requests = [];
  let cancel = false;
  const {dom, view} = setup({json: async (path, options) => {
    requests.push([path, options?.body]);
    if (path.endsWith('/pick-path')) return {path: cancel ? null : options.body.kind === 'folder' ? '/docs' : '/docs/指南.md'};
    return path.endsWith('/kb') ? base : catalog;
  }});
  const click = (root, label) => [...root.querySelectorAll('button')].find(b => b.textContent === label).click();
  try {
    await view.open();
    let form = view.dialog.querySelector('[aria-label="Single file import settings"] form');
    form.querySelector('input').value = '/docs';
    click(form, 'Choose file…'); await tick();
    assert.equal(document.querySelector('.local-path-picker'), null);
    assert.equal(form.querySelector('input').value, '/docs/指南.md');
    assert.ok(requests.some(([path, body]) => path.endsWith('/pick-path') && body.kind === 'file' && body.path === '/docs'));
    assert.equal(requests.some(([path]) => path.endsWith('/imports')), false);
    form.querySelector('input').value = '/docs/edited.md';
    form.dispatchEvent(new dom.window.Event('submit', {cancelable: true})); await tick();
    assert.ok(requests.some(([path, body]) => path.endsWith('/imports') && body.path === '/docs/edited.md' && body.kind === 'file'));
    click(view.dialog.querySelector('.kb-import-modes'), 'Folder');
    form = view.dialog.querySelector('[aria-label="Folder import settings"] form');
    click(form, 'Choose folder…'); await tick();
    assert.equal(form.querySelector('input').value, '/docs');
    cancel = true;
    click(form, 'Choose folder…'); await tick();
    assert.equal(form.querySelector('input').value, '/docs');
    assert.equal(document.querySelector('.local-path-picker'), null);
    form.dispatchEvent(new dom.window.Event('submit', {cancelable: true})); await tick();
    assert.ok(requests.some(([path, body]) => path.endsWith('/imports') && body.path === '/docs' && body.kind === 'folder'));
  } finally {view.close(); dom.window.close();}
});

test('native chooser failures preserve manual paths and late selections do not overwrite edits', async () => {
  let resolve, fail = false;
  const {dom, view} = setup({json: async path => {
    if (path.endsWith('/pick-path')) {
      if (fail) throw new Error('Native chooser unavailable');
      return new Promise(done => {resolve = done;});
    }
    return path.endsWith('/kb') ? base : catalog;
  }});
  try {
    await view.open();
    const form = view.dialog.querySelector('[aria-label="Single file import settings"] form');
    const path = form.querySelector('input'), choose = [...form.querySelectorAll('button')].find(b => b.textContent === 'Choose file…');
    path.value = '/original.md'; choose.click();
    assert.equal(choose.disabled, true);
    path.value = '/manual.md'; resolve({path: '/late.md'}); await tick();
    assert.equal(path.value, '/manual.md'); assert.equal(choose.disabled, false);
    fail = true; choose.click(); await tick();
    assert.match(view.notice.textContent, /Native chooser unavailable/);
    assert.equal(path.value, '/manual.md'); assert.equal(choose.disabled, false);
    fail = false; choose.click(); view.close(); resolve({path: '/closed.md'}); await tick();
    assert.equal(path.value, '/manual.md');
  } finally {view.close(); dom.window.close();}
});

test('import activity groups attempts by source, keeps old statuses in History and clears the selected attempt', async () => {
  const requests = [];
  const latest = {id: 'latest', kind: 'website', source_id: 'web', document: 'https://example.com/docs', state: 'failed', stage: 'failed',
    error: 'Latest failure', checkpointed: 38, created_at: '2026-10-13T12:00:00Z'};
  const previous = {id: 'previous', kind: 'website', state: 'completed', checkpointed: 3, created_at: '2026-10-12T12:00:00Z'};
  const grouped = {...base, jobs: [latest, {...previous, source_id: 'web'}], import_activity: [{
    id: 'website:web', kind: 'website', source_id: 'web', source: latest.document, index_directory: '/data/knowledge/lightrag/kb',
    published_directory: '/data/knowledge/lightrag/kb/generation_current', checkpoint_directory: '/data/knowledge/lightrag/kb/resume_web',
    latest, history: [previous],
  }]};
  const {dom, view} = setup({json: async (path, options) => {
    requests.push([path, options?.body]);
    if (path.endsWith('/sources')) return {sources: [{id: 'web', url: latest.document}]};
    if (path.endsWith('/kb')) return grouped;
    return catalog;
  }});
  try {
    await view.open();
    assert.equal(view.dialog.querySelectorAll('.kb-import-source').length, 1);
    assert.equal(view.dialog.querySelectorAll('.knowledge-job-progress').length, 1);
    assert.equal(view.dialog.querySelector('.knowledge-progress-state').textContent, 'Failed');
    assert.match(view.dialog.querySelector('.knowledge-source-progress').textContent, /failed/);
    const card = view.dialog.querySelector('.kb-import-source');
    assert.match(card.textContent, /https:\/\/example.com\/docs/);
    assert.match(card.textContent, /\/data\/knowledge\/lightrag\/kb/);
    const history = card.querySelector('.kb-import-history'); assert.equal(history.open, false);
    assert.match(history.querySelector('summary').textContent, /1 previous attempts/);
    history.open = true; history.querySelector('.kb-import-attempt').open = true;
    history.querySelector('button').click(); await tick();
    assert.ok(requests.some(([path]) => path.endsWith('/jobs/previous/clear')));
    assert.ok(!requests.some(([path]) => path.endsWith('/delete')));
  } finally {view.close(); dom.window.close();}
});

test('legacy cancelled website attempts can resume shared checkpoints from their source card', async () => {
  const requests = [];
  const latest = {id: 'legacy', kind: 'website', source_id: 'web', document: 'https://example.com', state: 'cancelled', checkpointed: 38, resumable: false};
  const grouped = {...base, jobs: [latest], import_activity: [{id: 'website:web', kind: 'website', source_id: 'web', source: latest.document,
    index_directory: '/data/index', checkpoint_directory: '/data/index/resume_web', latest, history: []}]};
  const {dom, view} = setup({json: async (path, options) => {
    requests.push([path, options?.body]);
    if (path.endsWith('/sources')) return {sources: []};
    if (path.endsWith('/kb')) return grouped;
    return catalog;
  }});
  try {
    await view.open();
    const resume = [...view.dialog.querySelectorAll('.kb-import-source > .knowledge-progress-actions button')].find(b => b.textContent === 'Resume');
    assert.equal(resume.disabled, false); resume.click(); await tick();
    assert.ok(requests.some(([path]) => path.endsWith('/sources/web/sync')));
    assert.ok(!requests.some(([path]) => path.endsWith('/jobs/legacy/resume')));
    assert.equal(view.dialog.querySelector('.knowledge-progress-source').hidden, true);
  } finally {view.close(); dom.window.close();}
});
