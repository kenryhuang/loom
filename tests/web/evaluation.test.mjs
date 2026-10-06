import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { EvaluationView } from "../../src/loom/web/assets/views/evaluation.mjs";

const settings = {
  max_calls: 10,
  max_tokens: 1000,
  max_seconds: 60,
  max_evidence_chars: 8000,
};
const ref = {
  line_number: 3,
  field_path: "response.content",
  start: 4,
  end: 20,
};
function result() {
  return {
    semantic: {
      coverage: {
        status: "incomplete",
        limitations: ["One round is unreviewed"],
        round_dimension_statuses: {
          loop_progress: { ineffective: 1, unknown: 1 },
        },
      },
      diagnoses: [
        {
          dimension: "loop_progress",
          scope: "round:1",
          epistemic_status: "inferred",
          observation: "<script>bad()</script>",
          interpretation: "Repeated planning",
          supporting_refs: [ref],
          counterevidence_refs: [],
        },
      ],
      verification: [
        {
          criterion_id: "c1",
          status: "unverified",
          rationale: "Missing final version binding",
          evidence_refs: [ref],
        },
      ],
      verification_framework: [],
      preserved_behaviors: [],
      round_analyses: [],
    },
    insights: {
      reviewed_rounds: 1,
      total_rounds: 2,
      task_completion: "unverified",
      progress_costs: {
        stalled: { rounds: 1, known_tokens: 100, unmeasured_rounds: 0 },
      },
      stalled_segments: [],
      verification_basis: "Latest definitions",
      criteria: [
        { id: "c1", description: "Original requirement", superseded: false },
      ],
    },
    proposals: [
      {
        surface: "loop_control",
        hypothesis: "Check before planning again",
        preserve: ["Keep evidence"],
        validation: "Paired comparison",
        supporting_refs: [ref],
      },
    ],
  };
}

test("deep evaluation selects budgets, renders uncertainty and evidence, and restores cached history", async () => {
  const dom = new JSDOM('<section id="root"></section>');
  globalThis.document = dom.window.document;
  const abort = new AbortController(),
    reads = [],
    starts = [];
  const job = {
    id: "judge1",
    state: "completed",
    model: "judge",
    settings,
    usage: { calls: 2, total_tokens: 60 },
    evaluation: result(),
  };
  const api = {
    evaluations: async () => ({
      models: [{ id: "judge", label: "Judge" }],
      default_model: "judge",
      defaults: settings,
      bounds: Object.fromEntries(
        Object.keys(settings).map((k) => [k, [1, 1000000]]),
      ),
      jobs: [],
    }),
    startEvaluation: async (...args) => {
      starts.push(args);
      return job;
    },
    evaluation: async () => job,
  };
  const root = document.querySelector("#root");
  const view = new EvaluationView(root, {
    api,
    sessionId: "s",
    analysisId: "a",
    signal: abort.signal,
    evidence: (r) => reads.push(r),
  });
  await view.open();
  view.fields.max_calls.value = "3";
  await view.run();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(starts[0][2].max_calls, 3);
  assert.match(root.textContent, /Review coverage · 1 \/ 2/);
  assert.match(root.textContent, /Original requirement/);
  assert.match(root.textContent, /Not tested/);
  assert.match(root.textContent, /unverified/);
  assert.equal(root.querySelector("script"), null);
  [...root.querySelectorAll("button")]
    .find((n) => n.textContent.startsWith("Evidence"))
    .click();
  assert.deepEqual(reads, [ref]);
  api.evaluations = async () => ({
    models: [{ id: "judge", label: "Judge" }],
    default_model: "judge",
    defaults: settings,
    bounds: Object.fromEntries(
      Object.keys(settings).map((k) => [k, [1, 1000000]]),
    ),
    jobs: [job],
  });
  const other = document.createElement("section");
  await new EvaluationView(other, {
    api,
    sessionId: "s",
    analysisId: "a",
    signal: abort.signal,
    evidence() {},
  }).open();
  await new Promise((r) => setTimeout(r, 0));
  assert.match(other.textContent, /Repeated planning/);
  abort.abort();
  dom.window.close();
});

test("late evaluation responses do not replace newer selection or an aborted session", async () => {
  const dom = new JSDOM("<section></section>");
  globalThis.document = dom.window.document;
  const abort = new AbortController();
  let resolveOld;
  const api = {
    evaluation: async (s, a, id) =>
      id === "old"
        ? new Promise((r) => {
            resolveOld = r;
          })
        : {
            id,
            model: "judge",
            settings,
            state: "completed",
            usage: { calls: 1 },
            evaluation: result(),
          },
  };
  const root = document.querySelector("section");
  const view = new EvaluationView(root, {
    api,
    sessionId: "s",
    analysisId: "a",
    signal: abort.signal,
    evidence() {},
  });
  view.status = document.createElement("p");
  view.start = document.createElement("button");
  view.cancel = document.createElement("button");
  view.model = document.createElement("select");
  view.fields = {};
  view.results = root;
  const old = view.follow("old");
  await view.follow("new");
  resolveOld({ id: "old" });
  await old;
  assert.equal(view.job.id, "new");
  const late = view.follow("old");
  abort.abort();
  resolveOld({ id: "old" });
  await late;
  assert.equal(view.job.id, "new");
  dom.window.close();
});

test("raising a stopped evaluation's time budget sends its checkpoint identity", async () => {
  const dom = new JSDOM("<section></section>");
  globalThis.document = dom.window.document;
  const abort = new AbortController();
  const starts = [];
  const job = {
    id: "saved",
    model: "judge",
    state: "budget_exhausted",
    settings: { ...settings, batch_rounds: 1 },
    usage: { calls: 8, total_tokens: 224707 },
    elapsed_seconds: 1985,
    error: "Evaluation time budget reached",
  };
  const api = {
    evaluations: async () => ({
      models: [{ id: "judge", label: "Judge" }],
      default_model: "judge",
      defaults: settings,
      bounds: Object.fromEntries(
        Object.keys(settings).map((k) => [k, [1, 1000000]]),
      ),
      jobs: [],
    }),
    evaluation: async () => job,
    startEvaluation: async (...args) => {
      starts.push(args);
      return job;
    },
  };
  const root = document.querySelector("section");
  const view = new EvaluationView(root, {
    api,
    sessionId: "s",
    analysisId: "a",
    signal: abort.signal,
  });
  await view.open();
  await view.follow(job.id);
  assert.match(root.textContent, /Increase the exhausted total budget/);
  view.fields.max_seconds.value = "7200";
  await view.run();
  assert.equal(starts[0][2].resume_id, "saved");
  assert.equal(starts[0][2].max_seconds, 7200);
  assert.equal(starts[0][2].batch_rounds, 1);
  abort.abort();
  dom.window.close();
});

test("progress distinguishes saved coverage from an in-flight evaluator call", () => {
  const dom = new JSDOM('<section id="root"></section>');
  globalThis.document = dom.window.document;
  const abort = new AbortController();
  const root = document.querySelector("section");
  const view = new EvaluationView(root, { signal: abort.signal });
  view.progress = root;
  view.cancel = document.createElement("button");
  view.renderProgress({
    state: "running",
    stage: "batch-2",
    selected_rounds: 24,
    reviewed_rounds: 8,
    completed_batches: 1,
    elapsed_seconds: 45,
    usage: { calls: 2, total_tokens: 1000 },
    current_call: {
      stage: "batch-2",
      state: "waiting",
      started_elapsed_seconds: 30,
    },
    progress: [
      { kind: "batch.saved", message: "Saved batch 1", elapsed_seconds: 29 },
    ],
  });
  assert.equal(root.querySelector("progress").value, 8);
  assert.equal(root.querySelector("progress").max, 24);
  assert.match(root.textContent, /Waiting for evaluator response · 15s/);
  assert.match(root.textContent, /Saved batch 1/);
  assert.equal(
    root.querySelector("ol").getAttribute("aria-label"),
    "Evaluation activity",
  );
  abort.abort();
  dom.window.close();
});
