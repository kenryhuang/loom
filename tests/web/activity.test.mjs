import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { FeedView } from "../../src/loom/web/assets/views/feed.mjs";
import { builtinPresenters } from "../../src/loom/web/assets/activity/presenters.mjs";
import {
  outputValue,
  modelText,
} from "../../src/loom/web/assets/activity/content.mjs";
const tick = () => new Promise((resolve) => setTimeout(resolve, 10));
const event = (seq, type, payload = {}, run_id = "run") => ({
  seq,
  type,
  payload,
  run_id,
});
function setup(options = {}) {
  const dom = new JSDOM('<div id="feed"></div>');
  globalThis.document = dom.window.document;
  const root = document.getElementById("feed"),
    feed = new FeedView(root, options);
  return { dom, root, feed };
}
const observation = (value) => ({
  id: "obs",
  source: "tool",
  at: "2026-10-06",
  value,
});

test("real Observation envelopes decode into meaningful command, read and edit details", () => {
  const presenters = builtinPresenters();
  const show = (name, input, output) =>
    presenters.present(
      {
        kind: "tool",
        details: { tool_id: name, input, output: observation(output) },
      },
      { workspace: "/workspace" },
    );
  const read = show(
    "read_file",
    { path: "/workspace/main.py" },
    { content: "one\ntwo", truncated: true },
  );
  assert.equal(read.subject, "main.py");
  assert.match(read.outcome, /2 lines · truncated/);
  assert.equal(read.sections[1].kind, "lines");
  const command = show(
    "shell_execute",
    { command: "pytest", cwd: "/other" },
    { exit_code: 2, stderr: "bad config" },
  );
  assert.equal(command.state, "failed");
  assert.equal(command.outcome, "Failed · exit 2");
  assert.equal(
    command.sections.find((section) => section.label === "Directory").value,
    "/other",
  );
  const edit = show(
    "edit_file",
    { path: "main.py" },
    { replacements: 1, diff: "-bad\n+good" },
  );
  assert.equal(edit.sections[1].kind, "diff");
  assert.equal(edit.outcome, "1 replacements");
  assert.equal(
    outputValue({ ok: true, value: observation({ exit_code: 0 }) }).exit_code,
    0,
  );
});

test("action protocol and partial JSON do not leak into readable model text", () => {
  assert.equal(
    modelText({
      content:
        '{"reasoning": "Inspect configuration", "action": {"kind":"tool"}}',
    }),
    "Inspect configuration",
  );
  assert.equal(modelText({ content: '{"action":' }), "");
  assert.equal(modelText({ content: '```json\n{"action":' }), "");
});

test("scoped concurrent calls and newest-first replay keep the correct inputs and final states", () => {
  const { dom, root, feed } = setup();
  const records = [
    event(1, "tool.started", {
      trace_id: "a",
      tool_call_id: "same",
      tool_id: "read_file",
      input: { path: "a.py" },
    }),
    event(2, "tool.started", {
      trace_id: "b",
      tool_call_id: "same",
      tool_id: "shell_execute",
      input: { command: "false" },
    }),
    event(3, "tool.completed", {
      trace_id: "b",
      tool_call_id: "same",
      tool_id: "shell_execute",
      output: observation({ exit_code: 1 }),
    }),
    event(4, "tool.completed", {
      trace_id: "a",
      tool_call_id: "same",
      tool_id: "read_file",
      output: observation({ content: "a" }),
    }),
  ];
  for (const value of records.toReversed()) feed.present(value);
  feed.orderRecords();
  assert.equal(root.querySelectorAll(".event-row").length, 2);
  assert.match(
    root.querySelector('[data-key="a:tool:same"]').textContent,
    /a.py/,
  );
  assert.match(
    root.querySelector('[data-key="b:tool:same"]').textContent,
    /false.*Failed/,
  );
  dom.window.close();
});

test("stable details and manual process state survive deltas; raw records are lazy and complete", async () => {
  const { dom, root, feed } = setup();
  feed.append(event(1, "run.started"));
  feed.append(
    event(2, "tool.started", {
      tool_id: "shell_execute",
      tool_call_id: "cmd",
      input: { command: "echo hello" },
    }),
  );
  const process = root.querySelector(".process-group");
  assert.equal(process.open, true);
  const row = root.querySelector(".event-row");
  row.open = true;
  await tick();
  const commandNode = row.querySelector(".activity-section");
  const button = row.querySelector("button");
  button.focus();
  feed.append(
    event(3, "tool.completed", {
      tool_id: "shell_execute",
      tool_call_id: "cmd",
      output: observation({ exit_code: 0, stdout: "hello" }),
    }),
  );
  assert.equal(row.querySelector(".activity-section"), commandNode);
  assert.equal(document.activeElement, button);
  assert.equal(root.querySelector(".event-detail-tabs"), null);
  assert.equal(root.querySelector(".activity-raw-content"), null);
  button.click();
  await tick();
  const inspector = document.querySelector("dialog");
  assert.ok(inspector);
  assert.match(inspector.querySelector("pre").textContent, /echo hello/);
  assert.match(inspector.querySelector("pre").textContent, /tool.completed/);
  inspector.querySelector("header button").click();
  assert.equal(document.activeElement, button);
  process.querySelector("summary").click();
  assert.equal(process.open, false);
  feed.append(event(4, "llm.requested", { llm_call_id: "next" }));
  assert.equal(process.open, false);
  process.querySelector("summary").click();
  feed.append(event(5, "run.completed"));
  assert.equal(process.open, true);
  dom.window.close();
});

test("artifact hydration updates the same summary, handles failures and keeps evidence available", async () => {
  let loads = 0;
  const { dom, root, feed } = setup({
    loadArtifact: async () => {
      loads++;
      return {
        tool_id: "shell_execute",
        input: { command: "check" },
        output: observation({ exit_code: 2, stderr: "Missing file" }),
      };
    },
  });
  feed.append(
    event(1, "tool.completed", {
      tool_id: "shell_execute",
      tool_call_id: "cmd",
      artifact: { sha256: "full" },
    }),
  );
  assert.equal(loads, 0);
  assert.match(root.textContent, /Result pending load/);
  root.querySelector(".event-row").open = true;
  await tick();
  assert.equal(loads, 1);
  assert.match(
    root.querySelector(".event-row > summary").textContent,
    /check.*Failed · exit 2/,
  );
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /check.*Failed · exit 2/,
  );
  assert.match(root.textContent, /Missing file/);
  dom.window.close();
});

test("continuous reads fold together but failure boundaries and plan revisions are preserved", () => {
  const { dom, root, feed } = setup();
  for (const seq of [1, 2])
    feed.append(
      event(seq, "tool.completed", {
        tool_id: "read_file",
        tool_call_id: String(seq),
        input: { path: `${seq}.py` },
        output: observation({ content: "x" }),
      }),
    );
  assert.equal(root.querySelectorAll(".activity-batch").length, 1);
  feed.append(
    event(3, "tool.failed", {
      tool_id: "read_file",
      tool_call_id: "3",
      error: { message: "Missing" },
    }),
  );
  feed.append(
    event(4, "tool.completed", {
      tool_id: "read_file",
      tool_call_id: "4",
      output: "x",
    }),
  );
  assert.equal(root.querySelectorAll(".activity-batch .event-row").length, 2);
  assert.equal(root.querySelectorAll(".event-row").length, 4);
  feed.append(
    event(5, "plan.updated", {
      trace_id: "a",
      plan_id: "plan",
      plan: { items: [{ content: "Inspect", status: "pending" }] },
    }),
  );
  feed.append(
    event(6, "plan.updated", {
      trace_id: "b",
      plan_id: "plan",
      plan: { items: [{ content: "Inspect", status: "completed" }] },
    }),
  );
  feed.toggleAll();
  assert.equal(root.querySelectorAll(".activity-plan").length, 1);
  assert.match(root.querySelector(".activity-plan").textContent, /completed/);
  dom.window.close();
});

test("user reading above the bottom is not dragged by new actions", () => {
  const { dom, root, feed } = setup();
  feed.append(event(1, "run.started"));
  const rows = root.querySelector(".process-records");
  Object.defineProperties(rows, {
    scrollHeight: { value: 1000 },
    clientHeight: { value: 300 },
  });
  rows.scrollTop = 50;
  feed.append(
    event(2, "tool.started", { tool_id: "read_file", tool_call_id: "read" }),
  );
  assert.equal(rows.scrollTop, 50);
  assert.equal(root.querySelector(".process-new-progress").hidden, false);
  root.querySelector(".process-new-progress").click();
  assert.equal(rows.scrollTop, 1000);
  dom.window.close();
});

test("plan calls remain visible alongside the current checklist with their own raw evidence", async () => {
  const { dom, root, feed } = setup();
  feed.append(
    event(1, "plan.updated", {
      plan_id: "p",
      plan: { items: [{ content: "Inspect", status: "completed" }] },
    }),
  );
  feed.append(
    event(2, "tool.completed", {
      tool_id: "update_plan",
      tool_call_id: "good",
      input: { items: [] },
      output: observation({ accepted: true }),
    }),
  );
  feed.append(
    event(3, "tool.completed", {
      tool_id: "update_plan",
      tool_call_id: "blocked",
      output: observation({ accepted: false, message: "Invalid transition" }),
    }),
  );
  assert.equal(root.querySelectorAll(".event-row").length, 3);
  assert.ok(root.querySelector('[data-key="tool:blocked"].failed'));
  const plan = root.querySelector('[data-key="tool:good"]');
  plan.open = true;
  await tick();
  plan.querySelector("button").click();
  await tick();
  assert.match(
    plan.querySelector(".activity-tool").textContent,
    /Tool · update_plan/,
  );
  assert.match(document.querySelector("dialog pre").textContent, /good/);
  feed.inspector.close();
  dom.window.close();
});

test("a stale artifact response cannot replace the newest tool result", async () => {
  const callbacks = {};
  const { dom, root, feed } = setup({
    loadArtifact: (digest) =>
      new Promise((resolve) => {
        callbacks[digest] = resolve;
      }),
  });
  feed.append(
    event(1, "tool.started", {
      tool_call_id: "call",
      tool_id: "read_file",
      artifact: { sha256: "old" },
    }),
  );
  root.querySelector(".event-row").open = true;
  await tick();
  feed.append(
    event(2, "tool.completed", {
      tool_call_id: "call",
      tool_id: "read_file",
      artifact: { sha256: "new" },
    }),
  );
  callbacks.old({ output: observation({ content: "stale" }) });
  await tick();
  callbacks.new({ output: observation({ content: "latest" }) });
  await tick();
  assert.match(root.textContent, /latest/);
  assert.doesNotMatch(root.textContent, /stale/);
  dom.window.close();
});

test("new model text preserves an active text selection until the reader releases it", async () => {
  const { dom, root, feed } = setup();
  feed.append(
    event(1, "llm.reasoning.delta", {
      llm_call_id: "model",
      delta: "Inspect config",
    }),
  );
  const row = root.querySelector(".event-row");
  row.open = true;
  await tick();
  const paragraph = row.querySelector(".markdown p");
  const range = document.createRange();
  range.selectNodeContents(paragraph);
  document.getSelection().addRange(range);
  feed.append(
    event(2, "llm.reasoning.delta", {
      llm_call_id: "model",
      offset: 14,
      delta: " carefully",
    }),
  );
  assert.equal(row.querySelector(".markdown p"), paragraph);
  assert.equal(document.getSelection().toString(), "Inspect config");
  document.getSelection().removeAllRanges();
  document.dispatchEvent(new dom.window.Event("selectionchange"));
  assert.match(row.textContent, /Inspect config carefully/);
  dom.window.close();
});

test("completed process elapsed time uses lifecycle timestamps, not later detail updates", () => {
  const { dom, root, feed } = setup();
  feed.append({ ...event(1, "run.started"), at: "2026-10-06T00:00:00Z" });
  feed.append({ ...event(2, "run.completed"), at: "2026-10-06T00:00:05Z" });
  feed.append({
    ...event(3, "tool.completed", {
      tool_id: "read_file",
      tool_call_id: "late",
    }),
    at: "2026-10-06T00:00:10Z",
  });
  assert.match(
    root.querySelector(".process-group > summary").textContent,
    /0m 5s elapsed/,
  );
  dom.window.close();
});

test("model summaries use explicit summary or action intent rather than truncating the detail", () => {
  const presenters = builtinPresenters();
  const show = (content) =>
    presenters.present({
      kind: "model",
      details: { response: { content: JSON.stringify(content) } },
    });
  const prose = "This is a long explanation with multiple details. ".repeat(30);
  const explicit = show({
    summary: "Check configuration",
    reasoning: prose,
    action: { kind: "tool", description: "Read the config" },
  });
  assert.equal(explicit.subject, "Check configuration");
  assert.equal(explicit.sections[0].value, prose);
  assert.equal(
    show({
      reasoning: prose,
      action: { kind: "tool", description: "Read the config" },
    }).subject,
    "Read the config",
  );
  assert.equal(show({ reasoning: prose }).subject, "Response received");
  assert.equal(explicit.toolName, "");
});
