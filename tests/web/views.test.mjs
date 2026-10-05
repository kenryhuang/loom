import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { FeedView } from "../../src/loom/web/assets/views/feed.mjs";
import {
  builtinPanels,
  PanelsView,
} from "../../src/loom/web/assets/views/panels.mjs";
import { renderMarkdown } from "../../src/loom/web/assets/markdown.mjs";

function setup() {
  const dom = new JSDOM('<div id="feed"></div><aside id="panels"></aside>');
  globalThis.document = dom.window.document;
  return dom;
}
const event = (seq, type, payload) => ({ seq, type, payload, run_id: "run" });
const state = () => ({
  session_id: "one",
  task: {
    limits: { max_tokens: 100 },
    workspace: "/workspace",
    state: "running",
  },
  messages: [],
  run: { id: "run" },
});

test("feed defaults process to collapsed and results to expanded, places snapshot messages chronologically", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root);
  feed.restore({
    snapshot: {
      ...state(),
      messages: [
        { id: "user", role: "user", content: "Task", seq: 1 },
        {
          id: "result",
          role: "assistant",
          content: '{"report":"# Result\\n\\n**Done**"}',
          seq: 4,
        },
      ],
    },
    events: [
      event(2, "tool.started", {
        tool_call_id: "call",
        tool_id: "fetch_url",
        input: { url: "https://example.test" },
      }),
      event(3, "tool.completed", {
        tool_call_id: "call",
        tool_id: "fetch_url",
        output: "facts",
      }),
    ],
  });
  assert.equal(root.children[0].dataset.key, "message:user");
  assert.equal(root.querySelector(".process-group").open, false);
  assert.equal(root.querySelectorAll(".event-row").length, 1);
  assert.equal(root.querySelector(".event-row").open, false);
  assert.match(
    root.querySelector(".event-detail").textContent,
    /https:\/\/example.test/,
  );
  assert.match(root.querySelector(".event-detail").textContent, /facts/);
  assert.equal(root.querySelector(".result").open, true);
  assert.equal(root.querySelector(".result h1").textContent, "Result");
  assert.equal(root.querySelector(".result strong").textContent, "Done");
  dom.window.close();
});

test("large tool details load on expansion, including when output arrives after opening", async () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    calls = [];
  const feed = new FeedView(root, {
    loadArtifact: async (digest) => {
      calls.push(digest);
      return { output: "full output" };
    },
  });
  feed.append(
    event(1, "tool.started", {
      tool_call_id: "call",
      tool_id: "read_file",
      input: { path: "source.txt" },
    }),
  );
  const row = root.querySelector(".event-row");
  row.open = true;
  feed.append(
    event(2, "tool.completed", {
      tool_call_id: "call",
      tool_id: "read_file",
      artifact: { sha256: "digest" },
    }),
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ["digest"]);
  assert.match(row.textContent, /full output/);
  assert.match(row.textContent, /source.txt/);
  dom.window.close();
});

test("bounded feed retains restored text and snapshot results after compaction", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root, { maxEvents: 2 });
  const snapshot = {
    ...state(),
    streams: { "call:reasoning": "全部思考" },
    messages: [{ id: "m", role: "assistant", content: "Done", seq: 1 }],
  };
  feed.restore({ snapshot, events: [] });
  feed.append(event(2, "one", {}));
  feed.append(event(3, "two", {}));
  feed.append(event(4, "three", {}));
  assert.equal(feed.events.length, 2);
  assert.equal(root.querySelectorAll(".result").length, 1);
  assert.match(root.textContent, /全部思考/);
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /3 events/,
  );
  dom.window.close();
});

test("process counts logical rows, preserving tool details during a long delta stream and after reconnect", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root, { maxEvents: 3 });
  feed.append(
    event(1, "tool.started", {
      tool_call_id: "tool",
      tool_id: "fetch_url",
      input: { url: "https://example.test" },
    }),
  );
  feed.append(
    event(2, "tool.completed", {
      tool_call_id: "tool",
      tool_id: "fetch_url",
      output: "Verified source",
    }),
  );
  const chunks = [];
  for (let index = 0; index < 150; index++) {
    const chunk = event(index + 3, "llm.reasoning.delta", {
      llm_call_id: "call",
      offset: index,
      delta: "思",
    });
    chunks.push(chunk);
    feed.append(chunk);
  }
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /2 events/,
  );
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  assert.match(root.textContent, /Verified source/);
  assert.match(root.textContent, /https:\/\/example.test/);
  assert.equal(feed.events.length, 3);
  assert.equal(
    root.querySelector('[data-key="thought:call"] pre').textContent,
    "思".repeat(150),
  );
  feed.restore({
    snapshot: { ...state(), streams: { "call:reasoning": "思".repeat(150) } },
    events: feed.events,
  });
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /2 events/,
  );
  feed.restore({ snapshot: state(), events: chunks });
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /1 events/,
  );
  assert.equal(feed.events.length, 1);
  assert.equal(
    root.querySelector('[data-key="thought:call"] pre').textContent,
    "思".repeat(150),
  );
  dom.window.close();
});

test("Markdown never interprets raw HTML or unsafe links", () => {
  const dom = setup();
  const rendered = renderMarkdown(
    "<script>alert(1)</script>\n\n[unsafe](javascript:alert)\n\n[ok](https://www.python.org/)\n\n| A | B |\n| --- | --- |\n| 1 | 2 |",
  );
  assert.equal(rendered.querySelector("script"), null);
  assert.equal(rendered.querySelectorAll("a").length, 1);
  assert.equal(rendered.querySelector("a").href, "https://www.python.org/");
  assert.match(rendered.textContent, /<script>/);
  assert.equal(rendered.querySelectorAll("table td").length, 2);
  dom.window.close();
});

test("panels show workspace second, dynamic workflow and extension widgets", () => {
  const dom = setup(),
    root = document.getElementById("panels"),
    registry = builtinPanels();
  registry.register("domain", () => ({
    element: document.createElement("section"),
    update(snapshot) {
      this.element.textContent = snapshot.session_id;
    },
  }));
  const panels = new PanelsView(root, registry, {});
  panels.update({
    ...state(),
    workflow: {
      workflow: {
        nodes: [{ id: "n", objective: "Read source", status: "running" }],
      },
    },
  });
  assert.equal(
    root.querySelectorAll(".info-row")[1].textContent,
    "Workspace/workspace",
  );
  assert.match(
    root.querySelector('[data-panel="workflow"]').textContent,
    /Read source/,
  );
  assert.equal(root.querySelector('[data-panel="domain"]').textContent, "one");
  dom.window.close();
});
