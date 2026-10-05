import assert from "node:assert/strict";
import test from "node:test";
import {
  ApiError,
  SessionApi,
  SseParser,
} from "../../src/loom/web/assets/api.mjs";
import { SessionController } from "../../src/loom/web/assets/controller.mjs";
import {
  builtinRenderers,
  RendererRegistry,
  reportContent,
} from "../../src/loom/web/assets/renderers.mjs";
import {
  EventGap,
  parseBudget,
  SessionProjection,
} from "../../src/loom/web/assets/state.mjs";

function snapshot(id = "one", cursor = 0) {
  return {
    session_id: id,
    event_cursor: cursor,
    title: id,
    task: {
      state: "running",
      limits: { max_tokens: 100, max_window_chars: 4 },
    },
    messages: [],
  };
}
function event(seq, type, payload = {}, session_id = "one") {
  return { session_id, run_id: "run", seq, type, payload };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("SSE parser supports split CRLF frames, heartbeat and multiline JSON", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push(': heartbeat\r\nid: 1\r\ndata: {"seq":'), []);
  assert.deepEqual(parser.push('1,\r\ndata: "text":"中文"}\r\n\r'), []);
  assert.deepEqual(parser.push('\ndata: {"seq":2}\n\n'), [
    { seq: 1, text: "中文" },
    { seq: 2 },
  ]);
});

test("transport uses bearer headers and cursor, decodes split UTF-8 and releases reader", async () => {
  let request,
    cancelled = false;
  const bytes = new TextEncoder().encode('data: {"seq":2,"text":"中文😀"}\n\n');
  const api = new SessionApi({
    token: "secret",
    fetchImpl: async (url, options) => {
      request = { url, options };
      return new Response(
        new ReadableStream({
          start(controller) {
            for (const byte of bytes)
              controller.enqueue(new Uint8Array([byte]));
          },
          cancel() {
            cancelled = true;
          },
        }),
      );
    },
  });
  for await (const value of api.events("one", 1)) {
    assert.equal(value.text, "中文😀");
    break;
  }
  assert.equal(request.url, "/v1/sessions/one/events?after=1");
  assert.equal(request.options.headers.Authorization, "Bearer secret");
  assert.equal(request.options.headers["Last-Event-ID"], "1");
  assert.equal(cancelled, true);
});

test("commands expose stable retry IDs, revisions and structured errors", async () => {
  let body;
  const api = new SessionApi({
    token: "secret",
    fetchImpl: async (_url, options) => {
      body = JSON.parse(options.body);
      return Response.json({ accepted: true });
    },
  });
  await api.command("one", "pause", {}, { commandId: "retry-1", revision: 3 });
  assert.deepEqual(body, {
    command_id: "retry-1",
    type: "pause",
    payload: {},
    expected_task_revision: 3,
  });
  api.fetch = async () =>
    Response.json(
      { error: { message: "Conflict", code: "CONFLICT" } },
      { status: 409 },
    );
  await assert.rejects(
    api.snapshot("one"),
    (error) =>
      error instanceof ApiError &&
      error.status === 409 &&
      error.code === "CONFLICT",
  );
});

test("projection deduplicates sequence numbers and rejects missing events", () => {
  const projection = new SessionProjection(snapshot());
  assert.equal(
    projection.apply(
      event(1, "task.state.changed", { state: "paused", revision: 2 }),
    ),
    true,
  );
  assert.equal(
    projection.apply(event(1, "task.state.changed", { state: "running" })),
    false,
  );
  assert.equal(
    projection.apply(event(2, "task.state.changed", {}, "other")),
    false,
  );
  assert.throws(() => projection.apply(event(3, "ignored")), EventGap);
  assert.equal(projection.cursor, 1);
  assert.equal(projection.snapshot.task.state, "paused");
});

test("projection handles Unicode codepoint offsets, overlap and bounded tails", () => {
  const projection = new SessionProjection(snapshot());
  const delta = (seq, offset, text) =>
    event(seq, "llm.reasoning.delta", {
      llm_call_id: "call",
      offset,
      delta: text,
    });
  projection.apply(delta(1, 0, "😀abc"));
  projection.apply(delta(2, 2, "bcde"));
  assert.equal(projection.streams["call:reasoning"], "bcde");
  assert.equal(projection.origins["call:reasoning"], 2);
  projection.apply(delta(3, 3, "cde"));
  assert.equal(projection.streams["call:reasoning"], "bcde");
  assert.throws(() => projection.apply(delta(4, 7, "gap")), EventGap);
  assert.equal(projection.cursor, 3);
  assert.equal(projection.snapshot.streams["call:reasoning"], "bcde");
});

test("projection updates budget, pending requests, outputs and workflow", () => {
  const projection = new SessionProjection(snapshot());
  projection.apply(event(1, "run.usage.changed", { total_tokens: 20 }));
  projection.apply(event(2, "task.budget.changed", { max_tokens: 50 }));
  assert.deepEqual(projection.snapshot.token_budget, {
    used: 20,
    limit: 50,
    remaining: 30,
  });
  projection.apply(
    event(3, "input.requested", { id: "question", state: "pending" }),
  );
  projection.apply(
    event(4, "task.outputs.changed", { artifacts: [{ sha256: "digest" }] }),
  );
  projection.apply(
    event(5, "workflow.updated", { workflow: { nodes: [{ id: "a" }] } }),
  );
  assert.equal(projection.snapshot.input_request.id, "question");
  assert.equal(projection.snapshot.output_artifacts[0].sha256, "digest");
  assert.equal(projection.snapshot.workflow.nodes[0].id, "a");
});

test("snapshot subscription race replays only events beyond the snapshot", async () => {
  let subscribed;
  const api = {
    snapshot: async () => snapshot("one", 1),
    history: async () => ({
      events: [event(1, "old"), event(2, "raced")],
      next_before: null,
    }),
    async *events(_id, cursor, signal) {
      subscribed = cursor;
      yield event(2, "message.created", {
        id: "m",
        role: "assistant",
        content: "Done",
      });
      await new Promise((resolve) =>
        signal.addEventListener("abort", resolve, { once: true }),
      );
    },
  };
  const controller = new SessionController(api),
    updates = [];
  controller.subscribe((update) => updates.push(update));
  try {
    await controller.select("one");
    await tick();
    assert.equal(subscribed, 1);
    assert.equal(controller.projection.cursor, 2);
    assert.equal(
      updates.find((update) => update.type === "restore").detail.events.length,
      1,
    );
    assert.equal(controller.projection.snapshot.messages[0].content, "Done");
  } finally {
    controller.disconnect();
  }
});

test("switching sessions discards slow snapshots and aborts previous subscriptions", async () => {
  const slow = deferred();
  let oldSignal,
    subscriptions = [];
  const api = {
    snapshot: (id, signal) => {
      if (id === "one") {
        oldSignal = signal;
        return slow.promise;
      }
      return snapshot(id);
    },
    history: async () => ({ events: [], next_before: null }),
    async *events(id, _cursor, signal) {
      subscriptions.push(id);
      await new Promise((resolve) =>
        signal.addEventListener("abort", resolve, { once: true }),
      );
    },
  };
  const controller = new SessionController(api);
  try {
    const first = controller.select("one");
    await controller.select("two");
    slow.resolve(snapshot("one"));
    await first;
    await tick();
    assert.equal(oldSignal.aborted, true);
    assert.equal(controller.projection.snapshot.session_id, "two");
    assert.deepEqual(subscriptions, ["two"]);
  } finally {
    controller.disconnect();
  }
});

test("authentication failure stops reconnecting and disconnects", async () => {
  const api = {
    snapshot: async () => snapshot(),
    history: async () => ({ events: [], next_before: null }),
    async *events() {
      throw new ApiError("Expired", 401);
    },
  };
  const controller = new SessionController(api);
  let unauthorized = false;
  controller.subscribe((update) => {
    if (update.type === "unauthorized") unauthorized = true;
  });
  await controller.select("one");
  await tick();
  assert.equal(unauthorized, true);
  assert.equal(controller.selectedId, null);
});

test("event gaps reload the snapshot before resubscribing", async () => {
  let loads = 0,
    cursors = [];
  const api = {
    snapshot: async () => snapshot("one", loads++ ? 3 : 0),
    history: async () => ({ events: [], next_before: null }),
    async *events(_id, cursor, signal) {
      cursors.push(cursor);
      if (!cursor) yield event(3, "missing-predecessors");
      else
        await new Promise((resolve) =>
          signal.addEventListener("abort", resolve, { once: true }),
        );
    },
  };
  const controller = new SessionController(api);
  try {
    await controller.select("one");
    await new Promise((resolve) => setTimeout(resolve, 450));
    assert.deepEqual(cursors, [0, 3]);
    assert.equal(loads, 2);
  } finally {
    controller.disconnect();
  }
});

test("pending questions route answers and prevent recovery redirection", async () => {
  const controller = new SessionController({
    command: async (...args) => args,
  });
  controller.selectedId = "one";
  controller.projection = new SessionProjection(snapshot());
  controller.projection.snapshot.input_request = {
    id: "question",
    state: "pending",
    kind: "clarification",
  };
  assert.deepEqual(await controller.send("Answer"), [
    "one",
    "answer_input",
    { request_id: "question", answer: "Answer" },
  ]);
  assert.deepEqual(await controller.send("New goal", true), [
    "one",
    "supersede_input",
    { request_id: "question", content: "New goal" },
  ]);
  controller.projection.snapshot.input_request.kind = "recovery";
  await assert.rejects(controller.send("New goal", true), /recovery question/);
});

test("reports normalize JSON envelopes and preserve arbitrary JSON", () => {
  assert.equal(
    reportContent('```json\n{"report":"# Result"}\n```'),
    "# Result",
  );
  assert.equal(
    reportContent({ action: { kind: "none", input: { report: "Done" } } }),
    "Done",
  );
  assert.equal(
    reportContent({ result: { markdown: "# Done" }, sources: [] }),
    "# Done",
  );
  assert.deepEqual(reportContent('{"rows":[1,2]}'), { rows: [1, 2] });
  assert.equal(
    reportContent("<script>alert(1)</script>"),
    "<script>alert(1)</script>",
  );
});

test("event renderer plugins override defaults without feed changes", () => {
  const registry = builtinRenderers().register(
    "domain",
    (value) => value.type === "domain.checked",
    (value) => ({ key: "domain", summary: value.payload.label }),
    10,
  );
  assert.equal(
    registry.describe(event(1, "domain.checked", { label: "Verified" }))
      .summary,
    "Verified",
  );
  assert.equal(
    registry.describe(
      event(2, "tool.started", { tool_call_id: "t", tool_id: "fetch_url" }),
    ).key,
    "tool:t",
  );
  assert.equal(
    new RendererRegistry().describe(event(3, "unknown")).summary,
    "unknown",
  );
});

test("token budget parsing accepts units and rejects invalid values", () => {
  assert.equal(parseBudget("1.5M"), 1500000);
  assert.equal(parseBudget("500k"), 500000);
  for (const invalid of ["0", "-1", "NaN", "1.25", "9007199254740992", "1x"])
    assert.throws(() => parseBudget(invalid));
});

test("live failure preserves its cause through recovery and clears on the next attempt", () => {
  const projection = new SessionProjection(snapshot());
  projection.apply(
    event(1, "run.failed", {
      code: "WORKFLOW_ROUTE_FAILED",
      message: "Required tool missing",
    }),
  );
  projection.apply(
    event(2, "run.recovery.required", {
      reason: "Worker exited",
      operations: [],
    }),
  );
  projection.apply(
    event(3, "task.state.changed", { state: "failed", revision: 57 }),
  );
  assert.equal(
    projection.snapshot.run.failure.message,
    "Required tool missing",
  );
  assert.equal(projection.snapshot.run.state, "suspended");
  projection.apply(event(4, "run.started", {}));
  assert.equal(projection.snapshot.run.failure, undefined);
});
