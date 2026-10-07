import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { KnowledgeProgress } from "../../src/loom/web/assets/views/knowledge-progress.mjs";

function setup() {
  const dom = new JSDOM("<body></body>");
  globalThis.document = dom.window.document;
  const calls = [];
  const panel = new KnowledgeProgress({
    cancel: document.createElement("button"),
    resume: async (job) => calls.push(job.id),
    error: (error) => {
      throw error;
    },
  });
  document.body.append(panel.root);
  return { dom, panel, calls };
}
const now = Date.parse("2026-10-07T12:02:00Z");
const job = {
  id: "job",
  kind: "website",
  source_id: "source",
  state: "running",
  document: "https://example.com/docs/",
  started_at: "2026-10-07T12:00:00Z",
  updated_at: "2026-10-07T12:01:25Z",
  elapsed_seconds: 85,
};

test("crawl shows current URL and real counts without inventing a percentage", () => {
  const { dom, panel } = setup();
  try {
    assert.match(panel.root.textContent, /No sync is running/);
    panel.update(
      {
        ...job,
        stage: "crawling",
        pages: 6,
        visited: 8,
        current_url: "https://example.com/docs/page",
      },
      now,
    );
    assert.equal(panel.state.textContent, "Running");
    assert.match(
      panel.item.textContent,
      /Working on: https:\/\/example.com\/docs\/page/,
    );
    assert.equal(
      panel.steps.querySelector('[aria-current="step"]').textContent,
      "Collect pages",
    );
    assert.match(
      panel.coverageLabel.textContent,
      /6 pages collected.*8 URLs visited/,
    );
    assert.equal(panel.bar.hasAttribute("value"), false);
    assert.equal(panel.percentage.textContent, "");
    assert.doesNotMatch(panel.root.textContent, /undefined|NaN/);
  } finally {
    dom.window.close();
  }
});

test("long-document work shows saved coverage, request stage and activity age", () => {
  const { dom, panel } = setup();
  try {
    panel.update(
      {
        ...job,
        stage: "embedding",
        current_document: "web_abcd_1234_Transformer.md",
        current_url: "https://example.com/old-page",
        indexed: 3,
        total: 184,
        checkpointed: 3,
        llm_calls: 234,
        reported_tokens: 755301,
        embedding_inputs: 3758,
      },
      now,
    );
    assert.equal(panel.stage.textContent, "Generating embeddings");
    assert.equal(panel.item.textContent, "Working on: Transformer.md");
    assert.equal(
      panel.steps.querySelector('[aria-current="step"]').textContent,
      "Build index",
    );
    assert.equal(panel.bar.value, 3);
    assert.equal(panel.bar.max, 184);
    assert.equal(panel.percentage.textContent, "1%");
    assert.match(
      panel.coverageLabel.textContent,
      /3 \/ 184 documents completed/,
    );
    assert.match(
      panel.coverageNote.textContent,
      /after a full document finishes/,
    );
    assert.match(
      panel.freshness.textContent,
      /35s ago.*Still checking for updates/,
    );
    assert.match(panel.metrics.textContent, /Elapsed2m 0s/);
    assert.equal(panel.cancel.hidden, false);
  } finally {
    dom.window.close();
  }
});

test("retry notices clear when work advances and activity text renders safely", () => {
  const { dom, panel } = setup();
  try {
    const progress = [
      { at: job.started_at, stage: "embedding", embedding_inputs: 10 },
      { at: job.updated_at, stage: "embedding", embedding_inputs: 20 },
      {
        at: job.updated_at,
        stage: "embedding_retry",
        detail: "<img src=x onerror=bad()> Timeout",
        retry: 1,
      },
    ];
    panel.update(
      {
        ...job,
        stage: "embedding_retry",
        detail: "Retrying after timeout",
        retry: 1,
        progress,
      },
      now,
    );
    assert.match(panel.message.textContent, /Retrying after timeout.*Retry 1/);
    assert.equal(panel.log.children.length, 2);
    assert.equal(panel.history.hidden, false);
    assert.equal(panel.root.querySelector("img"), null);
    panel.update(
      {
        ...job,
        stage: "extracting",
        detail: "Old retry message",
        retry: 1,
        progress,
      },
      now,
    );
    assert.equal(panel.message.hidden, true);
    assert.equal(
      panel.stage.textContent,
      "Extracting entities and relationships",
    );
  } finally {
    dom.window.close();
  }
});

test("failure retains coverage and provides resume for the saved source", async () => {
  const { dom, panel, calls } = setup();
  try {
    panel.update(
      {
        ...job,
        state: "failed",
        stage: "embedding_failed",
        indexed: 3,
        total: 184,
        checkpointed: 3,
        error: "Embedding request failed",
        finished_at: "2026-10-07T12:02:00Z",
      },
      now,
    );
    assert.equal(panel.state.textContent, "Failed");
    assert.equal(panel.percentage.textContent, "1%");
    assert.equal(panel.cancel.hidden, true);
    assert.equal(panel.retry.hidden, false);
    assert.equal(panel.retry.textContent, "Resume sync");
    assert.match(
      panel.message.textContent,
      /3 completed documents saved.*unfinished document/,
    );
    panel.retry.click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(calls, ["job"]);
    panel.update({ ...job, id: "next", stage: "queued", state: "queued" }, now);
    panel.cancellationRequested();
    assert.equal(panel.cancel.disabled, true);
    assert.match(panel.message.textContent, /Cancellation requested/);
    panel.update({ ...job, id: "another", stage: "crawling" }, now);
    assert.equal(panel.cancel.disabled, false);
    assert.equal(panel.message.hidden, true);
  } finally {
    dom.window.close();
  }
});

test("partial completion keeps crawl limitations visible even for legacy jobs", () => {
  const { dom, panel } = setup();
  try {
    panel.update(
      {
        ...job,
        state: "completed",
        stage: "completed",
        indexed: 11,
        total: 11,
        result: {
          complete: false,
          pages: 11,
          chunks: 55,
          pending: 20,
          missing_count: 8,
        },
      },
      now,
    );
    assert.equal(panel.state.textContent, "Partial sync");
    assert.match(panel.stage.textContent, /website crawl incomplete/);
    assert.match(
      panel.message.textContent,
      /20 URLs still queued.*8 URLs returned 404\/410/,
    );
    assert.equal(panel.percentage.textContent, "100%");
    assert.equal(panel.steps.querySelector('[aria-current="step"]'), null);
    assert.equal(panel.cancel.hidden, true);
    assert.equal(panel.retry.hidden, true);
  } finally {
    dom.window.close();
  }
});

test("connection interruption is explicit and clears after a successful refresh", () => {
  const { dom, panel } = setup();
  try {
    panel.update({ ...job, stage: "extracting" }, now);
    panel.connectionError(new Error("Network unavailable"));
    assert.equal(panel.state.textContent, "Updates interrupted");
    assert.match(panel.message.textContent, /Reconnecting automatically/);
    panel.update({ ...job, stage: "embedding" }, now);
    assert.equal(panel.state.textContent, "Running");
    assert.equal(panel.message.hidden, true);
  } finally {
    dom.window.close();
  }
});

test("terminal failures keep the last document and never display an active stage", () => {
  const { dom, panel } = setup();
  try {
    for (const [stage, expected] of [
      ["embedding", "Sync failed"],
      ["extracting_failed", "Entity extraction or merging failed"],
      ["cleanup_failed", "Index cleanup failed"],
    ]) {
      panel.update(
        {
          ...job,
          state: "failed",
          stage,
          failed_stage: "extracting",
          indexed: 3,
          total: 184,
          checkpointed: 3,
          current_document: "Transformer.md",
          error: "Indexing model request failed (LLM_FAILED)",
        },
        now,
      );
      assert.equal(panel.stage.textContent, expected);
      assert.equal(panel.state.textContent, "Failed");
      assert.equal(panel.item.textContent, "Last working on: Transformer.md");
      assert.equal(panel.cancel.hidden, true);
      assert.equal(panel.retry.hidden, false);
      assert.match(
        panel.message.textContent,
        /LLM_FAILED.*3 completed documents saved/,
      );
    }
    panel.update({ ...job, state: "cancelled", stage: "embedding" }, now);
    assert.equal(panel.stage.textContent, "Sync cancelled");
    assert.equal(panel.state.textContent, "Cancelled");
  } finally {
    dom.window.close();
  }
});
