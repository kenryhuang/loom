import assert from "node:assert/strict";
import test from "node:test";
import {JSDOM} from "jsdom";
import {renderBaseStatistics} from "../../src/loom/web/assets/views/base-evaluation.mjs";

import {statistics} from "./fixtures/base-statistics.mjs";

test("dashboard separates acceptance, visualizes measured costs and preserves evidence links", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), opened = [], refs = [];
  const s = statistics();
  assert.ok(renderBaseStatistics(root, {statistics: s}, {artifact: id => opened.push(id), evidence: ref => refs.push(ref)}));
  assert.equal(root.querySelectorAll("h3").length, 7);
  assert.match(root.textContent, /Raw events100/);
  assert.match(root.textContent, /Analyzed events10/);
  assert.equal(root.querySelector(".acceptance-ring").getAttribute("aria-label"), "1 of 1 acceptance checks passed");
  assert.match(root.textContent, /Checks passed1 \/ 1/);
  assert.match(root.textContent, /Analysis document exists/);
  assert.doesNotMatch(root.textContent, /unverified|goal coverage: unknown/);
  const activity = [...root.querySelectorAll('[role="img"]')].find(n => n.getAttribute("aria-label").startsWith("Running activity:"));
  assert.match(activity.getAttribute("aria-label"), /overlap 2 s/);
  assert.equal([...activity.children].reduce((sum, n) => sum + parseFloat(n.style.width), 0), 100);
  [...root.querySelectorAll("button")].find(b => b.textContent === "View evidence").click();
  assert.deepEqual(opened, ["proof"]); assert.deepEqual(refs, []);
  assert.doesNotMatch(root.textContent, /Failure reasons|Call details · slowest|Recorded output truncations|Repeated calls with identical|success \/ \(success|All checks passed/);
  const grouping = root.querySelector('[aria-label="Group tool statistics"]');
  grouping.value = "by_family"; grouping.dispatchEvent(new dom.window.Event("change"));
  assert.match(root.textContent, /command/);
  const model = [...root.querySelectorAll(".dashboard-panel")].find(n => n.querySelector("h3")?.textContent === "Model & token cost");
  assert.equal(model.querySelectorAll("table").length, 2);
  assert.equal(model.querySelectorAll("details, select, .dashboard-stats, figure").length, 0);
  assert.deepEqual([...model.querySelectorAll("table:first-of-type thead")][0].textContent,
    "ModelCallsTTFT · avg / P95TPS · avg / P95Duration · avg / P95");
  assert.match(model.textContent, /1 s \/ 1.5 s20 \/ 30/);
  assert.match(model.textContent, /Token in · total \/ maxToken out · total \/ maxmodel15 \/ 105 \/ 10/);
  assert.match(model.querySelector("tbody span").title, /2\/3 calls measured/);
  dom.window.close();
});

test("scope switching uses backend aggregates and unavailable measurements never become invented zero rates", () => {
  const dom = new JSDOM('<main></main>'); globalThis.document = dom.window.document;
  const root = document.querySelector("main"), s = statistics(), other = statistics();
  other.scope = "run"; other.basic.events.raw = 7; other.basic.events.analyzed = 5; other.basic.acceptance = null;
  other.tools.by_tool = [{...other.tools.by_tool[0], name: "custom <script>bad()</script>", success_rate: null, duration: {...other.tools.by_tool[0].duration, average_ms: null}}];
  s.by_run = {other};
  renderBaseStatistics(root, {statistics: s});
  const scope = root.querySelector('[aria-label="Base evaluation scope"]');
  scope.value = "other"; scope.dispatchEvent(new dom.window.Event("change"));
  assert.match(root.textContent, /Raw events7/);
  assert.equal(scope.value, "other");
  assert.match(root.textContent, /No checks recorded/);
  assert.equal(root.querySelector("script"), null);
  assert.match(root.textContent, /—/);
  dom.window.close();
});
