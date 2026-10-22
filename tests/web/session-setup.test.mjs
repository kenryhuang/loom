import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";
import { SessionSetupView } from "../../src/loom/web/assets/views/session-setup.mjs";

const html = await readFile(
  new URL("../../src/loom/web/assets/index.html", import.meta.url),
  "utf8",
);
const catalog = {
  setup_recommendation: true,
  acceptance: {required: true, defaults: {max_repairs: 2, max_model_calls: 6, max_seconds: 180}},
  models: [{ id: "main" }],
  templates: [
    {
      id: "general",
      task_spec: {
        session_environment: { plugin: "session" },
        tools: { collections: ["task_control"] },
      },
    },
    { id: "coding", requires_workspace: true },
  ],
  tool_collections: [
    {
      id: "filesystem",
      label: "Files",
      description: "Read and edit files",
      requires_workspace: true,
    },
    {
      id: "shell",
      label: "Commands",
      description: "Run commands",
      requires_workspace: true,
    },
    { id: "task_control", label: "Completion", description: "Finish" },
    {
      id: "knowledge",
      label: "Knowledge",
      description: "Search selected knowledge bases",
    },
  ],
};
const result = {
  task_type: "coding",
  tool_collections: ["filesystem", "task_control"],
  collection_reasons: { filesystem: "Read the source" },
  rationale: "Inspect source only",
  model: "main",
  usage: { total_tokens: 50 },
};
function setup(api) {
  const dom = new JSDOM(html);
  globalThis.document = dom.window.document;
  const root = document.querySelector("#create-page");
  root.hidden = false;
  const $ = (id) => root.querySelector(`#${id}`);
  for (const template of catalog.templates) {
    const option = document.createElement("option");
    option.value = template.id;
    $("create-template").append(option);
  }
  let view;
  const changed = () =>
    view.template(
      catalog.templates.find((t) => t.id === $("create-template").value),
    );
  view = new SessionSetupView(root, {
    api: () => api,
    templateChanged: changed,
  });
  view.configure(catalog);
  changed();
  return { dom, view, $, root };
}

test('adaptive DAG is selectable without replacing the default workflow', () => {
  const {dom, view, $} = setup({});
  const configured = {...catalog, plugins: {context: [{id: 'bounded_context'}],
    workflow: [{id: 'adaptive_dag'}, {id: 'dynamic'}, {id: 'legacy_planning'}]}};
  view.configure(configured);
  view.template(catalog.templates[0]);
  assert.equal($('setup-workflow').value, 'legacy_planning');
  $('setup-workflow').value = 'adaptive_dag';
  $('setup-workflow').dispatchEvent(new dom.window.Event('change'));
  assert.match($('setup-workflow-reason').textContent, /stable inputs.*read-only/);
  const payload = {knowledge_base_ids: []};
  view.apply(payload, catalog.templates[0]);
  assert.equal(payload.task_spec.workflow.plugin, 'adaptive_dag');
  dom.window.close();
});

test("recommendation is reviewable, tools can be changed, and workspace is explicitly bound", async () => {
  const requests = [];
  const { dom, view, $ } = setup({
    json: async (path, options) => {
      requests.push([path, options]);
      return result;
    },
  });
  $("create-objective").value = "Explain source indexing";
  assert.equal($("create-submit").textContent, "Create session");
  await view.recommend();
  assert.equal($("setup-auto"), null);
  assert.equal($("create-submit").textContent, "Create session");
  assert.equal($("create-template").value, "coding");
  assert.deepEqual(view.collections(), ["filesystem", "task_control"]);
  assert.match($("setup-status").textContent, /Inspect source only/);
  assert.equal($("setup-acceptance").hidden, false);
  assert.match($("setup-acceptance-status").textContent, /recommended setup includes automatic task acceptance/);
  assert.match($("setup-acceptance-status").textContent, /No workspace has been inspected/);
  assert.match($("setup-acceptance-budget").textContent, /2 repair rounds.*6 planning\/review calls.*180s/);
  const payload = { knowledge_base_ids: [] };
  assert.throws(
    () => view.apply(payload, catalog.templates[1]),
    /workspace directory/,
  );
  $("create-workspace").value = "/project";
  $("create-collections").querySelector('[value="shell"]').checked = true;
  view.apply(payload, catalog.templates[1]);
  assert.deepEqual(payload.task_spec.tools.collections, [
    "filesystem",
    "shell",
    "task_control",
  ]);
  assert.equal(
    payload.task_spec.session_environment.resources[0].uri,
    "/project",
  );
  assert.equal(requests.length, 1);
  assert.match(requests[0][0], /recommend$/);
  $("create-objective").value = "A different task";
  $("create-objective").dispatchEvent(new dom.window.Event("input"));
  assert.equal($("create-submit").textContent, "Create session");
  dom.window.close();
});

test("late recommendation cannot override a changed task or a hidden creation panel", async () => {
  let resolve;
  const { dom, view, $, root } = setup({
    json: () =>
      new Promise((r) => {
        resolve = r;
      }),
  });
  $("create-objective").value = "First";
  const pending = view.recommend();
  $("create-objective").value = "Second";
  $("create-objective").dispatchEvent(new dom.window.Event("input"));
  resolve(result);
  await pending;
  assert.equal($("create-template").value, "general");
  assert.equal(view.recommendation, null);
  const another = view.recommend();
  root.hidden = true;
  resolve(result);
  await another;
  assert.equal(view.recommendation, null);
  dom.window.close();
});

test("recommendation failure keeps manual creation available and missing knowledge binding is explicit", async () => {
  const { dom, view, $ } = setup({
    json: async () => {
      throw new Error("Model unavailable");
    },
  });
  $("create-objective").value = "Read a manual";
  await view.recommend();
  assert.match($("create-error").textContent, /configure manually/);
  assert.equal($("setup-auto"), null);
  $("create-collections").querySelector('[value="knowledge"]').checked = true;
  assert.throws(
    () => view.apply({ knowledge_base_ids: [] }, catalog.templates[0]),
    /Select a knowledge base/,
  );
  dom.window.close();
});

test("recommended knowledge and plugins remain editable and reach the final specification", async () => {
  const { dom, view, $ } = setup({
    json: async () => ({
      ...result,
      task_type: "general",
      tool_collections: ["task_control", "knowledge"],
      plugins: { context: "research_context", workflow: "dynamic" },
      plugin_reasons: { context: "Read sources" },
      knowledge_bases: [{ id: "kb", name: "AI Infra" }],
      knowledge_base_ids: ["kb"],
      knowledge_reasons: { kb: "Relevant material" },
    }),
  });
  view.catalog = {
    ...catalog,
    plugins: {
      context: [{ id: "bounded_context" }, { id: "research_context" }],
      workflow: [{ id: "dynamic" }, { id: "legacy_planning" }],
    },
  };
  $("create-objective").value = "Explain AI infrastructure";
  await view.recommend();
  assert.equal($("create-knowledge").selectedOptions[0].value, "kb");
  assert.equal($("setup-context").value, "research_context");
  assert.match($("setup-knowledge-reasons").textContent, /Relevant material/);
  $("setup-context").value = "bounded_context";
  const payload = {
    knowledge_base_ids: [...$("create-knowledge").selectedOptions].map(
      (o) => o.value,
    ),
  };
  view.apply(payload, catalog.templates[0]);
  assert.equal(payload.task_spec.context.plugin, "bounded_context");
  assert.equal(payload.task_spec.workflow.plugin, "dynamic");
  assert.deepEqual(payload.knowledge_base_ids, ["kb"]);
  assert.ok(payload.task_spec.tools.collections.includes("knowledge"));
  dom.window.close();
});


test("task editing preserves existing plugin options, unavailable bindings and validates restart limits", () => {
  const {dom, view, $, root} = setup({});
  const spec = {session_environment: {plugin: "session", resources: [{id: "source", kind: "directory", uri: "/old", access: "read-only"}]},
    execution_runtime: {plugin: "native_os", workspace_resource: "source"}, context: {plugin: "bounded_context", max_window_chars: 7000},
    workflow: {plugin: "legacy_planning", mode: "off"}, tools: {collections: ["filesystem", "task_control"]}};
  const task = {objective: "Inspect again", title: "Existing", workspace: "/old", model: "removed-model", plan_mode: "off", task_spec: spec,
    knowledge_base_ids: ["missing-kb"], external_tool_ids: ["missing-tool"], limits: {
      max_steps: 10, max_llm_calls: 20, max_tokens: 30000, max_duration_seconds: 60, max_window_chars: 9000}};
  view.configure({...catalog, plugins: {context: [{id: "bounded_context"}], workflow: [{id: "legacy_planning"}]}});
  view.template({task_spec: spec}); view.loadTask(task, []);
  assert.equal($("create-model").value, "removed-model");
  assert.equal($("create-knowledge").selectedOptions[0].value, "missing-kb");
  assert.equal(root.querySelector('#create-external-tools input:checked').value, "missing-tool");
  $("create-workspace").value = "/new";
  const payload = {objective: task.objective, task_spec: structuredClone(spec), knowledge_base_ids: ["missing-kb"]};
  view.apply(payload, {task_spec: spec}); view.restartChanges(payload);
  assert.match($("restart-context-note").textContent, /7,000 characters \(limited by the context plugin\)/);
  assert.equal(payload.task_spec.context.max_window_chars, 9000);
  assert.deepEqual(payload.task_spec.session_environment.resources, [{id: "source", kind: "directory", uri: "/new", access: "read-only"}]);
  assert.deepEqual(payload.limits, task.limits);
  assert.equal(payload.model, "removed-model");
  $("restart-max_tokens").value = "0";
  assert.throws(() => view.restartChanges(payload), /positive integers/);
  dom.window.close();
});

test('editing an old session resolves its model alias to the provider/model option', () => {
  const {dom, view, $} = setup({});
  view.configure({...catalog, model_aliases: {main: 'cloud/glm-5.2'}});
  const option = document.createElement('option'); option.value = 'cloud/glm-5.2'; option.textContent = option.value;
  $('create-model').append(option);
  view.loadTask({model: 'main', objective: 'Continue', limits: {max_window_chars: 9000}, task_spec: catalog.templates[0].task_spec}, []);
  assert.equal($('create-model').value, 'cloud/glm-5.2');
  assert.doesNotMatch($('create-model').textContent, /unavailable/);
  dom.window.close();
});
