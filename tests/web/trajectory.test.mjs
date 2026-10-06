import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { TrajectoryView } from "../../src/loom/web/assets/views/trajectory.mjs";

const ref = (line_number) => ({ line_number, field_path: null });
function fixture(id = "one") {
  return {
    id: "analysis-" + id,
    session_id: id,
    source_cursor: 10,
    state: "completed",
    analysis: {
      rounds: [
        {
          id: "round-" + id,
          run_id: "run",
          llm_call_id: "call",
          model: id,
          seq: 2,
          at: "2026-10-05T00:00:00Z",
          summary: "<script>alert(1)</script>",
          status: "complete",
          duration_ms: 100,
          request_ref: ref(1),
          response_ref: ref(2),
          evidence_refs: [],
          usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
          context: { message_count: 1 },
          context_changes: { added: 1, retained: 0, removed: 0 },
        },
      ],
      tools: [
        {
          id: "tool",
          round_id: "round-" + id,
          run_id: "run",
          tool_id: "read_file",
          status: "failed",
          exit_code: 2,
          input_ref: ref(3),
          raw_output_ref: ref(4),
          output_excerpt: "File missing",
          injection_count: 0,
        },
      ],
      tokens: { known_total_tokens: 15 },
      coverage: { missing_artifacts: [], unscoped_model_events: 0 },
      run_ids: ["run"],
      task_contracts: [],
      failures: [],
      semantic_status: "not_evaluated",
      task_completion: "unverified",
    },
  };
}
function setup() {
  const dom = new JSDOM('<section id="root"></section>');
  globalThis.document = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  dom.window.HTMLDialogElement.prototype.close = function () {
    this.dispatchEvent(new dom.window.Event("close"));
  };
  const root = document.getElementById("root");
  return { dom, root, view: new TrajectoryView(root, { onBack: () => {} }) };
}

test("trajectory renders measured facts, filters calls and reads paginated evidence on demand", async () => {
  const { dom, root, view } = setup(),
    reads = [];
  const api = {
    startTrajectory: async (id) => ({ ...fixture(id), state: "queued" }),
    trajectory: async (id) => fixture(id),
    trajectoryRound: async () => ({
      context_deltas: [{ added: [] }],
      verification_evidence: [],
    }),
    trajectoryEvidence: async (id, analysisId, pointer, start) => {
      reads.push([id, analysisId, pointer.line_number, start]);
      return {
        seq: 2,
        event_type: "llm.requested",
        content: start ? "second" : "first",
        returned_ref: { end: start ? 11 : 5 },
        truncated: !start,
      };
    },
  };
  await view.open(api, "one", 10);
  assert.match(root.textContent, /Use Deep evaluation/);
  assert.equal(root.querySelectorAll('[role="tab"]').length, 2);
  assert.equal(view.deepPanel.hidden, true);
  const signal = view.abort.signal;
  view.selectTab(1);
  assert.equal(view.body.hidden, true);
  assert.equal(view.deepPanel.hidden, false);
  assert.equal(signal.aborted, false);
  view.selectTab(0);
  assert.equal(view.body.hidden, false);
  assert.match(root.textContent, /15/);
  assert.equal(root.querySelectorAll(".trajectory-round").length, 1);
  assert.equal(root.querySelector("script"), null);
  view.updateState({ session_id: "one", event_cursor: 11 });
  assert.match(view.status.textContent, /New events available/);
  view.statusFilter.value = "failed";
  view.statusFilter.dispatchEvent(new dom.window.Event("input"));
  assert.equal(root.querySelectorAll(".trajectory-round").length, 1);
  view.search.value = "missing-query";
  view.search.dispatchEvent(new dom.window.Event("input"));
  assert.match(view.timeline.textContent, /No calls match/);
  view.search.value = "read_file";
  view.search.dispatchEvent(new dom.window.Event("input"));
  const call = root.querySelector(".trajectory-round");
  call.open = true;
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(call.textContent, /Recorded verification evidence/);
  await view.showEvidence(ref(1));
  assert.equal(view.dialog.querySelector("pre").textContent, "first");
  [...view.dialog.querySelectorAll("button")]
    .find((button) => button.textContent === "Load more")
    .click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(view.dialog.querySelector("pre").textContent, "firstsecond");
  assert.deepEqual(reads, [
    ["one", "analysis-one", 1, 0],
    ["one", "analysis-one", 1, 5],
  ]);
  view.close();
  assert.equal(document.querySelector("dialog"), null);
  dom.window.close();
});

test("late analysis responses cannot replace another session and failed jobs remain retryable", async () => {
  const { dom, root, view } = setup();
  let resolveOld;
  const oldApi = {
    startTrajectory: () =>
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
  };
  const old = view.open(oldApi, "old");
  const api = {
    startTrajectory: async (id) => fixture(id),
    trajectory: async (id) => fixture(id),
  };
  await view.open(api, "new");
  resolveOld(fixture("old"));
  await old;
  assert.equal(view.job.id, "analysis-new");
  assert.equal(view.sessionId, "new");
  assert.match(root.textContent, /new/);
  await view.open(
    {
      startTrajectory: async () => ({ id: "bad" }),
      trajectory: async () => ({
        state: "failed",
        error: "Evidence unavailable",
      }),
    },
    "bad",
  );
  assert.match(root.textContent, /Evidence unavailable/);
  assert.equal(view.refresh.disabled, false);
  view.close();
  dom.window.close();
});

test("sessions without model events have an explicit empty analysis", async () => {
  const { dom, root, view } = setup(),
    value = fixture();
  value.analysis.rounds = [];
  value.analysis.tools = [];
  await view.open(
    { startTrajectory: async () => value, trajectory: async () => value },
    "one",
  );
  assert.match(root.textContent, /No model calls have been recorded/);
  view.close();
  dom.window.close();
});
