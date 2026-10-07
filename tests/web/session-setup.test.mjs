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
