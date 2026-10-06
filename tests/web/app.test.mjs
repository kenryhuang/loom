import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { JSDOM } from "jsdom";

async function until(predicate, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {
    if (Date.now() > deadline)
      throw new Error("Timed out waiting for UI state");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

test("browser app connects to real service, answers input, creates sessions, controls tasks and loads artifacts", async () => {
  const root = fileURLToPath(new URL("../../", import.meta.url));
  const child = spawn(
    process.env.LOOM_TEST_PYTHON || `${root}.venv/bin/python`,
    ["tests/web/smoke.py"],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: "src:." },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "",
    stderr = "",
    dom;
  child.stdout.on("data", (value) => {
    output += value;
  });
  child.stderr.on("data", (value) => {
    stderr += value;
  });
  const exited = new Promise((resolve) => child.once("exit", resolve));
  const originalFetch = globalThis.fetch;
  try {
    await until(() => output.includes("Credential:"));
    const url = /http:\/\/127\.0\.0\.1:\d+\/web\//.exec(output)[0];
    const html = await readFile(
      new URL("../../src/loom/web/assets/index.html", import.meta.url),
      "utf8",
    );
    const listing = await originalFetch(
      new URL("/v1/sessions?limit=200", url),
      { headers: { Authorization: "Bearer browser-smoke-token" } },
    );
    const questionId = (await listing.json()).sessions.find(
      (session) => session.title === "Pending question",
    ).session_id;
    dom = new JSDOM(html, {
      url: `${url}#/sessions/${questionId}`,
      pretendToBeVisual: true,
    });
    for (const key of ["window", "document", "history", "location"])
      globalThis[key] = dom.window[key];
    // jsdom lacks native dialog methods; the rest of the DOM is real jsdom.
    dom.window.HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    dom.window.HTMLDialogElement.prototype.close = function () {
      this.open = false;
      this.dispatchEvent(new dom.window.Event("close"));
    };
    globalThis.fetch = (path, options) =>
      originalFetch(new URL(path, url), options);
    const $ = (id) => document.getElementById(id);
    const submit = (id) =>
      $(id).dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await import("../../src/loom/web/assets/app.mjs");
    $("credential").value = "browser-smoke-token";
    submit("auth-form");
    await until(
      () =>
        !$("auth-dialog").open &&
        $("session-title").textContent === "Pending question" &&
        !$("pending-input").hidden,
    );
    assert.match($("pending-input").textContent, /Which audience/);
    $("message").value = "General audience";
    submit("composer");
    await until(() => $("event-feed").querySelector(".result h1"));
    assert.equal(
      $("event-feed").querySelector(".result h1").textContent,
      "浏览器结果",
    );
    assert.equal($("event-feed").querySelector(".result").tagName, "ARTICLE");
    await until(() => !$("event-feed").querySelector(".process-group").open);
    assert.equal($("event-feed").querySelector(".process-group").open, false);
    assert.equal($("session-objective"), null);
    assert.ok($("event-feed").querySelector(".task-block .result"));
    $("nav-evaluation").click();
    await until(() =>
      $("trajectory-page").querySelector(".trajectory-metrics"),
    );
    assert.equal(location.hash, `#/evaluation/${questionId}`);
    assert.equal($("conversation-page").hidden, true);
    assert.match($("trajectory-page").textContent, /Use Deep evaluation/);
    history.back();
    await until(() => !$("conversation-page").hidden);
    assert.equal($("trajectory-page").hidden, true);
    history.forward();
    await until(() => !$("trajectory-page").hidden);
    assert.equal($("nav-evaluation").getAttribute("aria-pressed"), "true");
    assert.equal(
      $("trajectory-page").getAttribute("aria-label"),
      "Trajectory analysis",
    );
    assert.equal(document.querySelector(".session-tabs"), null);
    assert.equal($("evaluation-panel").hidden, false);
    $("nav-sessions").click();
    await until(() => !$("conversation-page").hidden);
    assert.equal(location.hash, `#/sessions/${questionId}`);
    assert.equal($("nav-sessions").getAttribute("aria-pressed"), "true");
    assert.equal($("evaluation-panel").hidden, true);
    assert.ok($("event-feed").querySelector(".task-block .result"));
    assert.equal($("toggle-details").textContent, "Expand all");
    $("toggle-details").click();
    assert.equal(
      $("event-feed").querySelectorAll("details:not([open])").length,
      0,
    );
    assert.equal($("toggle-details").textContent, "Fold all");
    $("toggle-details").click();
    assert.equal($("event-feed").querySelectorAll("details[open]").length, 0);
    assert.equal($("toggle-details").textContent, "Expand all");
    await until(() => document.querySelector('[data-panel="outputs"] button'));
    document.querySelector('[data-panel="outputs"] button').click();
    await until(() => document.querySelector("dialog:not([id])[open] h1"));
    assert.equal(
      document.querySelector("dialog:not([id])[open] h1").textContent,
      "浏览器结果",
    );
    document.querySelector("dialog:not([id])[open] button").click();
    [...$("session-list").querySelectorAll("button")]
      .find((button) => button.textContent.includes("Failed route"))
      .click();
    await until(
      () =>
        $("session-title").textContent === "Failed route" &&
        !$("run-failure").hidden,
    );
    assert.match(
      $("run-failure").textContent,
      /WORKFLOW_ROUTE_FAILED.*Model did not select/,
    );
    $("new-session").click();
    $("create-title").value = "UI session";
    $("create-objective").value = "Created in browser";
    submit("create-form");
    await until(
      () =>
        $("session-title").textContent === "UI session" &&
        $("event-feed").querySelector(".result"),
    );
    assert.equal($("run-failure").hidden, true);
    const sessionButton = [
      ...$("session-list").querySelectorAll("button"),
    ].find((button) => button.textContent.includes("Browser demo"));
    assert.ok(sessionButton);
    sessionButton.click();
    await until(
      () =>
        $("session-title").textContent === "Browser demo" &&
        $("event-feed").querySelector(".result"),
    );
    document.querySelector('[data-command="complete_task"]').click();
    await until(() => $("task-status").textContent === "completed");
    document.querySelector('[data-command="reopen_task"]').click();
    await until(() => $("task-status").textContent === "idle");
    document.querySelector('[aria-label="Token budget"]').value = "500k";
    document
      .querySelector(".budget-form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await until(() =>
      document
        .querySelector('[data-panel="budget"]')
        .textContent.includes("500,000"),
    );
    $("disconnect").click();
    assert.equal($("auth-dialog").open, true);
    assert.equal($("connection").textContent, "Disconnected");
    assert.equal($("credential").value, "");
    assert.equal($("toggle-details").disabled, true);
  } catch (error) {
    error.message += `\nService stderr: ${stderr}`;
    throw error;
  } finally {
    dom?.window.dispatchEvent(new dom.window.Event("pagehide"));
    dom?.window.close();
    globalThis.fetch = originalFetch;
    child.kill("SIGINT");
    await exited;
  }
});
