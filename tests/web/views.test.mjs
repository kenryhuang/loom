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
  assert.equal(root.children[0].className, "task-block");
  assert.deepEqual(
    [...root.children[0].children].map((node) => node.dataset.key),
    ["message:user", "group:run", "message:result"],
  );
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

test("conversation separates runs into task-process-result blocks, including live guidance and pending tasks", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root);
  const first = { id: "first", role: "user", content: "First task", seq: 2 },
    guidance = { id: "guidance", role: "user", content: "More detail", seq: 4 },
    result = { id: "result", role: "assistant", content: "Done", seq: 6 },
    second = { id: "second", role: "user", content: "Second task", seq: 8 };
  feed.restore({
    snapshot: { ...state(), messages: [second, result, guidance, first] },
    events: [
      { ...event(1, "command.accepted", {}), run_id: null },
      event(3, "run.started", {}),
      event(5, "tool.completed", { tool_call_id: "one", output: "facts" }),
    ],
  });
  assert.equal(root.querySelectorAll(".task-block").length, 2);
  assert.deepEqual(
    [...root.children[0].children].map((node) => node.dataset.key),
    ["message:first", "message:guidance", "group:run", "message:result"],
  );
  feed.append({ ...event(9, "run.started", {}), run_id: "second-run" });
  feed.append({
    ...event(10, "message.created", {
      id: "second-result",
      role: "assistant",
      content: "Second result",
    }),
    run_id: "second-run",
  });
  assert.deepEqual(
    [...root.children[1].children].map((node) => node.dataset.key),
    ["message:second", "group:second-run", "message:second-result"],
  );
  // Replaying the same messages must not duplicate or rearrange the blocks.
  feed.syncMessages([first, guidance, result, second]);
  assert.equal(root.querySelectorAll(".task-block").length, 2);
  assert.equal(root.querySelectorAll(".user").length, 3);
  dom.window.close();
});

test("new guidance after a failed execution belongs to the next run's block", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root);
  feed.restore({
    snapshot: {
      ...state(),
      messages: [
        { id: "first", role: "user", content: "First task", seq: 1 },
        { id: "retry", role: "user", content: "Try again", seq: 5 },
      ],
    },
    events: [
      event(2, "run.started", {}),
      event(3, "run.failed", { message: "Failed" }),
      { ...event(6, "run.started", {}), run_id: "retry-run" },
    ],
  });
  assert.deepEqual(
    [...root.children].map((block) =>
      [...block.children].map((node) => node.dataset.key),
    ),
    [
      ["message:first", "group:run"],
      ["message:retry", "group:retry-run"],
    ],
  );
  dom.window.close();
});

test("historical tasks retain their processes and failures outside the recent event window", async () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    requests = [];
  const older = {
    id: "old:2",
    run_id: "old",
    start_seq: 2,
    end_seq: 9,
    state: "completed",
    milestones: [
      { ...event(2, "run.started", {}), run_id: "old" },
      {
        ...event(4, "run.failed", {
          code: "WORKFLOW_ROUTE_FAILED",
          message: "Missing route",
        }),
        run_id: "old",
      },
      { ...event(5, "run.started", {}), run_id: "old" },
      {
        ...event(8, "run.state.changed", { state: "completed" }),
        run_id: "old",
      },
    ],
  };
  const feed = new FeedView(root, {
    maxEvents: 2,
    loadProcess: async (process, before) => {
      requests.push([process.run_id, before]);
      return {
        events: [
          {
            ...event(3, "tool.completed", {
              tool_call_id: "old-tool",
              tool_id: "read_file",
              output: { value: { content: "Old evidence" } },
            }),
            run_id: "old",
          },
        ],
        next_before: null,
      };
    },
  });
  const snapshot = {
    ...state(),
    messages: [
      { id: "old-task", role: "user", content: "Old task", seq: 1 },
      { id: "old-result", role: "assistant", content: "Old result", seq: 9 },
      { id: "new-task", role: "user", content: "New task", seq: 10 },
      { id: "new-result", role: "assistant", content: "New result", seq: 30 },
    ],
  };
  const processes = [
    older,
    {
      id: "run:11",
      run_id: "run",
      start_seq: 11,
      end_seq: 30,
      state: "completed",
      milestones: [
        event(11, "run.started", {}),
        event(29, "run.state.changed", { state: "completed" }),
      ],
    },
  ];
  feed.restore({
    snapshot,
    processes,
    events: [
      event(25, "llm.content.delta", {
        llm_call_id: "new",
        offset: 0,
        delta: "New answer",
      }),
    ],
  });
  assert.equal(root.querySelectorAll(".task-block").length, 2);
  for (const block of root.querySelectorAll(".task-block"))
    assert.deepEqual(
      [...block.children].map((node) => node.className),
      ["message-card user", "process-group", "message-card result"],
    );
  const group = root.querySelector('[data-key="group:old:2"]');
  assert.match(group.querySelector("summary").textContent, /1 failures/);
  assert.match(
    group.querySelector(".failed").textContent,
    /WORKFLOW_ROUTE_FAILED/,
  );
  group.open = true;
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(requests, [["old", 10]]);
  assert.match(group.textContent, /Old evidence/);
  assert.equal(group.querySelector(".process-history").disabled, true);
  feed.append(event(31, "new-event", {}));
  feed.append(event(32, "newer-event", {}));
  assert.equal(root.querySelectorAll(".process-group").length, 2);
  assert.match(
    root.querySelector('[data-key="group:old:2"]').textContent,
    /Old evidence/,
  );
  assert.match(
    root.querySelector('[data-key="group:old:2"]').textContent,
    /WORKFLOW_ROUTE_FAILED/,
  );
  dom.window.close();
});

test("rounds that reuse a run ID have distinct processes between their task and result", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root);
  feed.restore({
    snapshot: {
      ...state(),
      messages: [
        { id: "first", role: "user", content: "First", seq: 1 },
        {
          id: "first-result",
          role: "assistant",
          content: "First result",
          seq: 5,
        },
        { id: "second", role: "user", content: "Second", seq: 6 },
        {
          id: "second-result",
          role: "assistant",
          content: "Second result",
          seq: 10,
        },
      ],
    },
    events: [],
    processes: [
      {
        id: "run:2",
        run_id: "run",
        start_seq: 2,
        end_seq: 6,
        state: "completed",
        milestones: [
          event(2, "run.started", {}),
          event(4, "run.state.changed", { state: "completed" }),
        ],
      },
      {
        id: "run:7",
        run_id: "run",
        start_seq: 7,
        end_seq: 10,
        state: "completed",
        milestones: [
          event(7, "run.started", {}),
          event(9, "run.state.changed", { state: "completed" }),
        ],
      },
    ],
  });
  assert.deepEqual(
    [...root.children].map((block) =>
      [...block.children].map((node) => node.dataset.key),
    ),
    [
      ["message:first", "group:run:2", "message:first-result"],
      ["message:second", "group:run:7", "message:second-result"],
    ],
  );
  dom.window.close();
});

test("live execution rounds keep failure and retry milestones when detailed events are evicted", () => {
  const dom = setup(),
    root = document.getElementById("feed");
  const feed = new FeedView(root, {
    maxEvents: 2,
    loadProcess: async () => ({ events: [], next_before: null }),
  });
  feed.restore({ snapshot: state(), events: [] });
  feed.append(event(1, "run.started", {}));
  feed.append(
    event(2, "run.failed", {
      code: "WORKFLOW_ROUTE_FAILED",
      message: "No route",
    }),
  );
  feed.append(event(3, "run.started", {}));
  feed.append(
    event(4, "tool.completed", { tool_call_id: "call", tool_id: "read_file" }),
  );
  feed.append(event(5, "run.state.changed", { state: "completed" }));
  feed.append({ ...event(6, "run.started", {}), run_id: "next" });
  feed.append({
    ...event(7, "tool.completed", { tool_call_id: "next-call" }),
    run_id: "next",
  });
  assert.equal(feed.events.length, 2);
  assert.equal(root.querySelectorAll(".process-group").length, 2);
  const first = root.querySelector('[data-key="group:run:1"]');
  assert.match(first.textContent, /WORKFLOW_ROUTE_FAILED/);
  assert.match(first.querySelector("summary").textContent, /completed/);
  assert.equal(first.querySelectorAll(".event-row").length, 4);
  dom.window.close();
});

test("loading older process pages preserves completed state and the latest tool outcome", async () => {
  const dom = setup(),
    root = document.getElementById("feed");
  const process = {
    id: "run:2",
    run_id: "run",
    start_seq: 2,
    end_seq: 5,
    state: "completed",
    milestones: [
      event(2, "run.started", {}),
      event(5, "run.state.changed", { state: "completed" }),
    ],
  };
  const feed = new FeedView(root, {
    loadProcess: async (_process, before) =>
      before === 6
        ? {
            events: [
              event(4, "tool.completed", {
                tool_id: "shell_execute",
                tool_call_id: "call",
                output: {
                  value: { ok: false, exit_code: 2, stderr: "Command failed" },
                },
              }),
            ],
            next_before: 4,
          }
        : {
            events: [
              event(3, "tool.started", {
                tool_id: "shell_execute",
                tool_call_id: "call",
                input: { command: "failed-command" },
              }),
            ],
            next_before: null,
          },
  });
  feed.restore({ snapshot: state(), events: [], processes: [process] });
  const group = feed.groups.get(process.id);
  await feed.history(group);
  await feed.history(group);
  assert.equal(group.state, "completed");
  assert.equal(
    group.records.get("tool:call").node.classList.contains("failed"),
    true,
  );
  assert.match(
    group.records.get("tool:call").summary.textContent,
    /Failed \(exit 2\)/,
  );
  assert.match(
    group.records.get("tool:call").pre.textContent,
    /failed-command/,
  );
  assert.deepEqual(
    [...group.rows.children].map((node) => node.dataset.key),
    ["event:2", "tool:call", "event:5"],
  );
  dom.window.close();
});

test("expand/fold all includes nested details, new events and results and survives replay", async () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    changes = [],
    calls = [];
  const feed = new FeedView(root, {
    onDetailsChange: (state) => changes.push(state),
    loadArtifact: async (digest) => {
      calls.push(digest);
      return { output: "Full detail" };
    },
  });
  const snapshot = {
    ...state(),
    messages: [{ id: "result", role: "assistant", content: "Done", seq: 3 }],
  };
  feed.restore({
    snapshot,
    events: [
      event(2, "tool.completed", {
        tool_call_id: "call",
        artifact: { sha256: "digest" },
      }),
    ],
  });
  assert.deepEqual(changes.at(-1), { available: true, expanded: false });
  feed.toggleAll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ["digest"]);
  assert.equal(root.querySelectorAll("details:not([open])").length, 0);
  assert.deepEqual(changes.at(-1), { available: true, expanded: true });
  feed.append({
    ...event(4, "tool.started", { tool_call_id: "next" }),
    run_id: "next-run",
  });
  assert.equal(root.querySelectorAll("details:not([open])").length, 0);
  feed.toggleAll();
  assert.equal(root.querySelectorAll("details[open]").length, 0);
  feed.restore({ snapshot, events: feed.events });
  assert.equal(root.querySelectorAll("details[open]").length, 0);
  feed.older([event(1, "task.state.changed", { state: "running" })], snapshot);
  assert.equal(root.querySelectorAll("details[open]").length, 0);
  feed.append({
    ...event(5, "message.created", {
      id: "next-result",
      role: "assistant",
      content: "Later",
    }),
    run_id: "next-run",
  });
  assert.equal(root.querySelectorAll("details[open]").length, 0);
  feed.reset();
  assert.deepEqual(changes.at(-1), { available: false, expanded: false });
  feed.message({
    id: "fresh",
    role: "assistant",
    content: "Fresh session",
    seq: 1,
  });
  assert.equal(root.querySelector(".result").open, true);
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
