import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { FeedView } from "../../src/loom/web/assets/views/feed.mjs";
import { builtinRenderers } from "../../src/loom/web/assets/renderers.mjs";
import { renderVerificationArtifact } from "../../src/loom/web/assets/views/activity-row.mjs";

const tick = () => new Promise(resolve => setTimeout(resolve, 10));
const event = (seq, kind, sha256 = `artifact-${seq}`, run_id = "run") => ({seq, run_id,
  type: "artifact.created", payload: {artifact: {kind, sha256}}});
const source = (overrides = {}) => ({id: "tool:read", tool: "read_file", paths: ["src/config.ts"],
  origin: "tool_receipt", ok: true, input: '{"path":"src/config.ts"}', source_ref: {sha256: "source"}, ...overrides});

test("source snapshots stay in raw history while the readable directory groups by run", () => {
  const renderers = builtinRenderers();
  assert.equal(renderers.describe(event(1, "verification_source")).hidden, true);
  const first = renderers.describe(event(2, "verification_source_directory"));
  const next = renderers.describe(event(3, "verification_source_directory"));
  assert.equal(first.aggregate, true);
  assert.equal(first.key, next.key);
  assert.notEqual(first.key, renderers.describe(event(4, "verification_source_directory", "other", "other-run")).key);
});

test("ordinary reports with empty citations keep their report viewer", () => {
  const dom = new JSDOM(); globalThis.document = dom.window.document;
  try {
    assert.equal(renderVerificationArtifact({report: "# Final report", sources: []}), null);
    assert.match(renderVerificationArtifact({version: 3, sources: []}).textContent, /Evidence indexed for acceptance review/);
  } finally {dom.window.close();}
});

test("directory loads on expansion and original receipt loads only on explicit request", async () => {
  const dom = new JSDOM('<div id="feed"></div>');
  globalThis.document = dom.window.document;
  const requests = [], root = document.getElementById("feed");
  const loadArtifact = async digest => {
    requests.push(digest);
    if (digest === "directory") return {version: 3, sources: [source()]};
    if (digest === "source") return {tool_receipt: true, row: {id: "tool:read", tool: "read_file", artifact_format: "call", artifact: {sha256: "receipt"}}};
    return {input: {path: "src/config.ts"}, output: {content: 'export const enabled = true; <script>bad()</script>', bytes_read: 64}};
  };
  const feed = new FeedView(root, {loadArtifact});
  try {
    feed.append(event(1, "verification_source"));
    assert.equal(root.querySelector(".event-row"), null);
    feed.append(event(2, "verification_source_directory", "directory"));
    assert.deepEqual(requests, []);
    assert.match(root.textContent, /Acceptance evidence indexed/);
    feed.toggleAll(true); await tick();
    assert.deepEqual(requests, ["directory"]);
    assert.match(root.textContent, /1 sources available for review/);
    assert.match(root.textContent, /read_file · src\/config.ts · Succeeded/);
    assert.match(root.textContent, /does not mean every source has been reviewed/);
    root.querySelector(".verification-source button").click(); await tick();
    assert.deepEqual(requests, ["directory", "source", "receipt"]);
    assert.match(root.querySelector(".verification-source-content").textContent, /export const enabled = true/);
    assert.equal(root.querySelector("script"), null);
    assert.equal(root.querySelector(".verification-source button"), null);
    feed.toggleAll(false); feed.toggleAll(true); await tick();
    assert.deepEqual(requests, ["directory", "source", "receipt"]);
  } finally {feed.destroy(); dom.window.close();}
});

test("source failures offer retry and mismatched references never show unrelated content", async () => {
  const dom = new JSDOM(); globalThis.document = dom.window.document;
  let calls = 0;
  const view = renderVerificationArtifact({sources: [source()]}, async () => {
    calls++;
    if (calls === 1) throw new Error("temporarily unavailable");
    if (calls === 2) return {row: {id: "other", content: "Wrong source"}};
    return {row: {id: "tool:read", content: "Original report", result: {status: "passed", reason: "Verified facts"}}};
  });
  try {
    const button = view.querySelector("button");
    button.click(); await tick();
    assert.equal(button.disabled, false);
    assert.match(view.textContent, /temporarily unavailable/);
    button.click(); await tick();
    assert.match(view.textContent, /does not match its index/);
    assert.doesNotMatch(view.textContent, /Wrong source/);
    button.click(); await tick();
    assert.match(view.textContent, /Original report/);
    assert.match(view.textContent, /Verified facts/);
    assert.equal(view.querySelector("button"), null);
  } finally {dom.window.close();}
});

test("command sources show stdout, errors and exit status without rerunning the command", async () => {
  const dom = new JSDOM(); globalThis.document = dom.window.document;
  const requests = [];
  const entry = source({tool: "shell_execute", paths: [], input: "npm test", ok: false});
  const view = renderVerificationArtifact({sources: [entry]}, async digest => {
    requests.push(digest);
    if (digest === "source") return {tool_receipt: true, row: {id: entry.id, tool: entry.tool, artifact_format: "call", artifact: {sha256: "receipt"}}};
    return {input: {command: "npm test"}, output: {stdout: "2 tests failed", stderr: "Assertion failed", exit_code: 1}};
  });
  try {
    view.querySelector("button").click(); await tick();
    assert.deepEqual(requests, ["source", "receipt"]);
    for (const text of ["npm test", "2 tests failed", "Assertion failed", "Exit code", "Failed"]) assert.ok(view.textContent.includes(text));
  } finally {dom.window.close();}
});
