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

test('setup uses cards with collapsed optional settings and live selection summaries', () => {
  const {dom, view, $, root} = setup({});
  assert.ok(root.querySelectorAll('.setup-block').length >= 7);
  for (const id of ['setup-external', 'setup-knowledge', 'setup-plugins']) assert.equal($(id).open, false);
  assert.equal($('setup-knowledge-summary').textContent, 'None selected');
  view.renderKnowledge([{id: 'kb', name: 'Project notes', description: 'Architecture and design notes'}]);
  $('create-knowledge').querySelector('input').click();
  assert.equal($('setup-knowledge-summary').textContent, '1 selected');
  assert.equal($('setup-collections-summary').textContent, '2 selected');
  $('setup-knowledge').querySelector('summary').click();
  assert.equal($('setup-knowledge').open, true);
  view.configure(catalog);
  assert.equal($('setup-knowledge').open, false);
  dom.window.close();
});

test('knowledge checkbox cards show escaped names and descriptions and submit the current multi-selection', () => {
  const {dom, view, $} = setup({});
  view.renderKnowledge([
    {id: 'architecture', name: 'Architecture <script>name</script>', description: 'System design <img src=x onerror=alert(1)>'},
    {id: 'operations', name: 'Operations', description: 'Runbooks and incident notes'},
  ], ['architecture']);
  const root = $('create-knowledge'), checks = [...root.querySelectorAll('input[type="checkbox"]')];
  assert.equal(root.querySelectorAll('label').length, 2);
  assert.equal(root.querySelector('script, img, select'), null);
  assert.match(root.textContent, /System design <img/);
  checks[1].click();
  assert.deepEqual(view.knowledgeIds(), ['architecture', 'operations']);
  assert.equal($('setup-knowledge-summary').textContent, '2 selected');
  checks[0].click();
  const payload = {knowledge_base_ids: ['stale-selection']};
  view.apply(payload, catalog.templates[0]);
  assert.deepEqual(payload.knowledge_base_ids, ['operations']);
  assert.ok(payload.task_spec.tools.collections.includes('knowledge'));
  checks[1].click();
  assert.equal($('create-collections').querySelector('[value="knowledge"]').checked, false);
  assert.equal($('setup-knowledge-summary').textContent, 'None selected');
  view.renderKnowledge([]);
  assert.match(root.textContent, /No knowledge bases available/);
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
  assert.equal($("setup-acceptance"), null);
  assert.equal($("create-title"), null);
  assert.equal($("setup-recommend").classList.contains('primary'), true);
  assert.equal($("setup-external").open, false);
  assert.equal($("setup-knowledge").open, false);
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
      knowledge_bases: [{ id: "kb", name: "AI Infra", description: "Infrastructure design and operations" }],
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
  assert.deepEqual(view.knowledgeIds(), ['kb']);
  assert.equal($("setup-context").value, "research_context");
  assert.match($("create-knowledge").textContent, /Infrastructure design and operations/);
  assert.match($("create-knowledge").textContent, /Relevant material/);
  $("setup-context").value = "bounded_context";
  const payload = {
    knowledge_base_ids: view.knowledgeIds(),
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
  view.configure({...catalog, plugins: {context: [{id: "bounded_context"}], workflow: [{id: "legacy_planning"}]},
    tool_collections: [...catalog.tool_collections, {id: 'external_tools', label: 'External tools', description: 'Registered tools'}],
    external_tools: [{id: 'new-tool', connector_name: 'Local', name: 'Search', description: 'Search files', kind: 'cli'}]});
  view.template({task_spec: spec}); view.loadTask(task, [{id: 'new-kb', name: 'Project notes', description: 'Architecture and conventions'}]);
  assert.equal($("create-workspace").readOnly, true);
  assert.match($("workspace-hint").textContent, /fixed for this session/);
  assert.equal($("create-model").value, "removed-model");
  assert.deepEqual(view.knowledgeIds(), ['missing-kb']);
  assert.equal(root.querySelector('#create-external-tools input:checked').value, "missing-tool");
  $("create-workspace").value = "/new";
  const payload = {objective: task.objective, task_spec: structuredClone(spec), knowledge_base_ids: ["missing-kb"]};
  view.apply(payload, {task_spec: spec}); view.restartChanges(payload);
  assert.match($("restart-context-note").textContent, /7,000 characters \(limited by the context plugin\)/);
  assert.equal(payload.task_spec.context.max_window_chars, 9000);
  assert.equal(payload.workspace, '/old');
  assert.deepEqual(payload.task_spec.session_environment.resources, [{id: "source", kind: "directory", uri: "/old", access: "read-only"}]);
  assert.deepEqual(payload.limits, task.limits);
  assert.equal(payload.model, "removed-model");
  for (const id of ['missing-kb', 'new-kb']) $('create-knowledge').querySelector(`input[value="${id}"]`).click();
  for (const id of ['missing-tool', 'new-tool']) $('create-external-tools').querySelector(`input[value="${id}"]`).click();
  view.apply(payload, {task_spec: spec}); view.restartChanges(payload);
  assert.deepEqual(payload.knowledge_base_ids, ['new-kb']);
  assert.deepEqual(payload.external_tool_ids, ['new-tool']);
  assert.ok(payload.task_spec.tools.collections.includes('knowledge'));
  assert.ok(payload.task_spec.tools.collections.includes('external_tools'));
  assert.equal(payload.workspace, '/old');
  assert.match($('create-knowledge').textContent, /Project notesArchitecture and conventions/);
  $("restart-max_tokens").value = "0";
  assert.throws(() => view.restartChanges(payload), /positive integers/);
  dom.window.close();
});

test('task editing keeps the workspace across template changes and custom specifications, and creation unlocks it', () => {
  const {dom, view, $} = setup({});
  const original = {session_environment: {plugin: 'session', resources: [{id: 'source', kind: 'directory', uri: '/fixed'}]},
    execution_runtime: {plugin: 'native_os', workspace_resource: 'source'}, tools: {collections: ['task_control']}};
  const task = {objective: 'Continue', workspace: '/fixed', plan_mode: 'off', task_spec: original, limits: {
    max_steps: 10, max_llm_calls: 20, max_tokens: 30000, max_duration_seconds: 60, max_window_chars: 9000}};
  view.loadTask(task, []);
  const replacement = {task_spec: {session_environment: {plugin: 'session'}, tools: {collections: ['task_control']}}};
  view.template(replacement);
  $('create-workspace').value = '/attempted-change';
  const payload = {objective: task.objective, knowledge_base_ids: []};
  view.apply(payload, replacement); view.restartChanges(payload);
  assert.equal(payload.workspace, '/fixed');
  assert.equal(payload.task_spec.session_environment.resources[0].uri, '/fixed');
  payload.task_spec.session_environment.resources[0].uri = '/custom-change';
  assert.throws(() => view.restartChanges(payload), /Workspace is fixed/);
  view.setEditing(false);
  assert.equal($('create-workspace').readOnly, false);
  assert.doesNotMatch($('workspace-hint').textContent, /fixed for this session/);
  dom.window.close();
});

test('task editing cannot add a workspace to a session created without one', () => {
  const {dom, view, $} = setup({});
  view.loadTask({objective: 'Continue', workspace: null, task_spec: catalog.templates[0].task_spec, limits: {
    max_steps: 10, max_llm_calls: 20, max_tokens: 30000, max_duration_seconds: 60, max_window_chars: 9000}}, []);
  assert.equal($('create-workspace').readOnly, true);
  assert.equal($('create-workspace').placeholder, 'No workspace bound');
  const payload = {objective: 'Continue', task_spec: {execution_runtime: {workspace_resource: 'new'},
    session_environment: {resources: [{id: 'new', kind: 'directory', uri: '/other'}]}}};
  assert.throws(() => view.restartChanges(payload), /Workspace is fixed/);
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
