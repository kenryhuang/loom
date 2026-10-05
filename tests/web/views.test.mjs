import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { FeedView } from "../../src/loom/web/assets/views/feed.mjs";
import { SessionProjection } from "../../src/loom/web/assets/state.mjs";
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
  assert.doesNotMatch(
    group.querySelector("summary").textContent,
    /\d+ failures/,
  );
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
  assert.equal(
    first.querySelector("summary").textContent,
    "Process · Tool call (read_file) · Done",
  );
  assert.equal(first.querySelectorAll(".event-row").length, 1);
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
    ["tool:call"],
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
  assert.equal(root.querySelectorAll(".event-row").length, 3);
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
    /Thought: 思/,
  );
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  assert.match(root.textContent, /Verified source/);
  assert.match(root.textContent, /https:\/\/example.test/);
  assert.equal(feed.events.length, 3);
  assert.equal(
    JSON.parse(root.querySelector('[data-key="thought:call"] pre').textContent)
      .reasoning,
    "思".repeat(150),
  );
  feed.restore({
    snapshot: { ...state(), streams: { "call:reasoning": "思".repeat(150) } },
    events: feed.events,
  });
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  feed.restore({ snapshot: state(), events: chunks });
  assert.equal(root.querySelectorAll(".event-row").length, 1);
  assert.equal(feed.events.length, 1);
  assert.equal(
    JSON.parse(root.querySelector('[data-key="thought:call"] pre').textContent)
      .reasoning,
    "思".repeat(150),
  );
  dom.window.close();
});

test("model lifecycle and output share one row while proposed tool calls do not imply execution", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root);
  const llm = (seq, type, payload = {}) =>
    event(seq, type, { llm_call_id: "model", ...payload });
  const events = [
    llm(1, "llm.requested", {
      messages: [{ role: "user", content: "Inspect project" }],
    }),
    llm(2, "llm.stream.started"),
    llm(3, "llm.reasoning.delta", { delta: "Inspect files", offset: 0 }),
    llm(4, "llm.content.delta", { delta: "Proposed call", offset: 0 }),
    llm(5, "llm.tool_call.started", {
      tool_call_id: "proposed",
      tool_name: "unavailable_tool",
    }),
    llm(6, "llm.tool_call.arguments.delta", {
      tool_call_id: "proposed",
      delta: "{}",
    }),
    llm(7, "llm.tool_call.completed", { tool_call_id: "proposed" }),
    llm(8, "llm.stream.completed"),
    llm(9, "llm.completed", {
      response: {
        content: "Proposed call",
        tool_calls: [
          { id: "proposed", name: "unavailable_tool", arguments: "{}" },
        ],
      },
    }),
    event(10, "operation.started", {
      call: { id: "actual", name: "read_file" },
    }),
    event(11, "tool.started", {
      tool_call_id: "actual",
      tool_id: "read_file",
      input: { path: "README.md" },
    }),
    event(12, "tool.completed", {
      tool_call_id: "actual",
      tool_id: "read_file",
      output: "Project description",
    }),
    event(13, "operation.completed", { operation_id: "run:actual" }),
  ];
  for (const value of events) feed.append(value);
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  const model = root.querySelector('[data-key="thought:model"]');
  assert.match(
    model.querySelector("summary").textContent,
    /Inspect files.*Done/,
  );
  assert.equal(model.classList.contains("running"), false);
  assert.match(model.querySelector("pre").textContent, /Inspect project/);
  assert.match(model.querySelector("pre").textContent, /unavailable_tool/);
  assert.equal(root.querySelector('[data-key="tool:proposed"]'), null);
  assert.match(
    root.querySelector('[data-key="tool:actual"]').textContent,
    /README.md/,
  );
  // The historical page arrives newest first; older request/stream records
  // must enrich details without changing the finished model back to Running.
  feed.restore({ snapshot: state(), events: events.slice(8) });
  for (const value of events.slice(0, 8).reverse()) feed.present(value);
  feed.orderRecords();
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  const restored = root.querySelector('[data-key="thought:model"]');
  assert.match(restored.querySelector("summary").textContent, /Done/);
  assert.match(restored.querySelector("pre").textContent, /Inspect project/);
  assert.equal(restored.classList.contains("running"), false);
  dom.window.close();
});

test("hidden bookkeeping cannot evict model failures and uncertain operations stay visible", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    feed = new FeedView(root, { maxEvents: 3 });
  feed.append(event(1, "llm.requested", { llm_call_id: "failed" }));
  feed.append(
    event(2, "llm.failed", {
      llm_call_id: "failed",
      error: { code: "LLM_FAILED", message: "Connection dropped" },
    }),
  );
  for (let seq = 3; seq < 30; seq++)
    feed.append(
      event(seq, "llm.tool_call.started", { tool_call_id: String(seq) }),
    );
  feed.append(event(30, "operation.uncertain", { operation_id: "run:write" }));
  assert.equal(feed.events.length, 3);
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  assert.equal(root.querySelectorAll(".failed").length, 2);
  assert.match(root.textContent, /Connection dropped/);
  assert.match(root.textContent, /Recovery required/);
  assert.equal(
    root.querySelector(".process-group > summary").textContent,
    "Process · Recovery required · Operation effects are uncertain",
  );
  feed.older(
    [event(0, "llm.stream.started", { llm_call_id: "failed" })],
    state(),
  );
  assert.equal(root.querySelector(".process-group > summary").className, "");
  assert.doesNotMatch(
    root.querySelector(".process-group > summary").textContent,
    /Connection dropped|failures/,
  );
  assert.match(
    root.querySelector('[data-key="thought:failed"] summary').textContent,
    /Connection dropped/,
  );
  dom.window.close();
});

test("status and usage events update progress without adding rows or consuming the detail window", () => {
  const dom = setup(),
    root = document.getElementById("feed");
  const snapshot = { ...state(), event_cursor: 0 };
  const projection = new SessionProjection(snapshot);
  const feed = new FeedView(root, {
    maxEvents: 3,
    loadProcess: async () => ({ events: [], next_before: null }),
  });
  feed.restore({ snapshot, events: [] });
  const apply = (seq, type, payload = {}) => {
    const value = { ...event(seq, type, payload), session_id: "one" };
    assert.equal(projection.apply(value), true);
    feed.append(value);
  };
  apply(1, "run.started");
  apply(2, "tool.completed", {
    tool_call_id: "read",
    tool_id: "read_file",
    output: "Evidence",
  });
  apply(3, "run.usage.changed", { total_tokens: 10 });
  apply(4, "task.state.changed", { state: "paused", revision: 2 });
  apply(5, "run.state.changed", { state: "paused" });
  assert.equal(root.querySelectorAll(".event-row").length, 1);
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /Tool call \(read_file\) · Done/,
  );
  for (let seq = 6; seq < 15; seq++)
    apply(seq, "run.usage.changed", { total_tokens: seq });
  assert.equal(feed.events.length, 3);
  assert.match(root.textContent, /Evidence/);
  apply(15, "run.state.changed", { state: "completed" });
  apply(16, "task.state.changed", { state: "idle", revision: 3 });
  assert.equal(projection.snapshot.token_budget.used, 14);
  assert.equal(projection.snapshot.run.state, "completed");
  assert.equal(projection.snapshot.task.state, "idle");
  const group = feed.groups.get("run:1");
  assert.equal(group.state, "completed");
  assert.equal(Number(group.node.dataset.endSeq), 15);
  assert.equal(
    group.summary.textContent,
    "Process · Tool call (read_file) · Done",
  );
  for (const summary of root.querySelectorAll(".event-row > summary"))
    assert.doesNotMatch(summary.textContent, /usage.changed|state.changed/);
  feed.older(
    [event(0, "run.state.changed", { state: "queued" })],
    projection.snapshot,
  );
  assert.equal(feed.groups.get("run:1").state, "completed");
  dom.window.close();
});

test("session synchronization updates sidebar fields while live and restored streams retain only errors and recovery", () => {
  const dom = setup(),
    root = document.getElementById("feed"),
    sidebar = document.getElementById("panels");
  const snapshot = { ...state(), event_cursor: 0 };
  const projection = new SessionProjection(snapshot);
  const panels = new PanelsView(sidebar, builtinPanels(), {
    artifact: () => {},
  });
  const feed = new FeedView(root, {
    loadProcess: async () => ({ events: [], next_before: null }),
  });
  feed.restore({ snapshot, events: [] });
  let seq = 0;
  const apply = (type, payload = {}) => {
    const value = { ...event(++seq, type, payload), session_id: "one" };
    projection.apply(value);
    feed.append(value);
    feed.snapshot = projection.snapshot;
    panels.update(projection.snapshot);
  };
  const info = (label) =>
    [...sidebar.querySelectorAll(".info-row")]
      .find((row) => row.querySelector("small").textContent === label)
      ?.querySelector("span").textContent;
  apply("command.accepted", { type: "resume" });
  apply("task.goal.revised", {
    objective: "Inspect current source",
    goal_revision: 2,
  });
  apply("task.budget.changed", { max_tokens: 200 });
  apply("run.started");
  apply("step.started");
  apply("run.usage.changed", { total_tokens: 25 });
  apply("run.state.changed", {
    state: "suspended",
    reason: "Time budget exhausted",
  });
  apply("task.state.changed", { state: "paused", revision: 2 });
  assert.equal(info("State"), "paused");
  assert.equal(info("Run state"), "suspended");
  assert.equal(info("Reason"), "Time budget exhausted");
  assert.equal(info("Objective"), "Inspect current source");
  assert.match(
    sidebar.querySelector('[data-panel="budget"]').textContent,
    /25 used \/ 200 total/,
  );
  apply("run.time_budget.renewed", {
    active_seconds: 120,
    max_duration_seconds: 90,
  });
  assert.equal(info("Time budget"), "90 s");
  apply("run.started");
  assert.equal(projection.snapshot.run.time_budget_start_seconds, 120);
  assert.equal(info("Reason"), undefined);
  assert.equal(projection.snapshot.token_budget.used, 25);
  apply("run.completed");
  apply("task.state.changed", { state: "idle", revision: 3 });
  apply("task.outputs.changed", {
    artifacts: [{ kind: "report", sha256: "digest" }],
  });
  apply("step.completed");
  apply("command.applied", { command_id: "resume" });
  assert.equal(info("Run state"), "completed");
  assert.equal(info("State"), "idle");
  assert.match(
    sidebar.querySelector('[data-panel="outputs"]').textContent,
    /report/,
  );
  assert.equal(root.querySelectorAll(".event-row").length, 0);
  apply("run.failed", { code: "LLM_FAILED", message: "Connection dropped" });
  assert.equal(info("State"), "failed");
  assert.equal(info("Error"), "Connection dropped");
  apply("run.recovery.required", { reason: "Verify operation effects" });
  apply("task.state.changed", { state: "recovering" });
  assert.equal(info("Run state"), "suspended");
  assert.equal(info("State"), "recovering");
  apply("run.started");
  assert.equal(info("Error"), undefined);
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  feed.restore({
    snapshot: projection.snapshot,
    processes: feed.processes,
    events: feed.events,
  });
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  assert.match(root.textContent, /Connection dropped/);
  assert.match(root.textContent, /Verify operation effects/);
  assert.doesNotMatch(
    root.querySelector(".process-records").textContent,
    /task\.goal\.revised|run\.started|run\.completed|step\.started|command\.accepted|run\.time_budget\.renewed/,
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
