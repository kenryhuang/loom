import { SessionApi } from "./api.mjs";
import { SessionController } from "./controller.mjs";
import { element, renderResult } from "./markdown.mjs";
import { reportContent } from "./renderers.mjs";
import { FeedView } from "./views/feed.mjs";
import { builtinPanels, PanelsView } from "./views/panels.mjs";
import { SessionListView } from "./views/sessions.mjs";
import { TrajectoryView } from "./views/trajectory.mjs";

const $ = (id) => document.getElementById(id);
let controller,
  catalog,
  analysisMode = false,
  sending = false;
const trajectory = new TrajectoryView($("trajectory-page"), {
  onError: (error) => {
    if (error.status === 401) showError(error);
  },
});
const list = new SessionListView(
  $("session-list"),
  $("session-search"),
  select,
);
const feed = new FeedView($("event-feed"), {
  loadArtifact: (digest) =>
    controller.api.artifact(controller.selectedId, digest),
  loadProcess: (process, before) =>
    controller.api.history(controller.selectedId, {
      runId: process.run_id,
      after: process.start_seq - 1,
      before,
      view: "activity",
      signal: controller.abort.signal,
    }),
  onDetailsChange: ({ available, expanded }) => {
    $("toggle-details").disabled = !available;
    $("toggle-details").textContent = expanded ? "Fold all" : "Expand all";
    $("toggle-details").setAttribute("aria-expanded", String(expanded));
  },
});
const panels = new PanelsView($("session-panels"), builtinPanels(), {
  command: (kind, payload) => controller.command(kind, payload),
  error: showError,
  artifact: openArtifact,
});

function notice(message, error = false) {
  const root = $("notice");
  root.textContent = message;
  root.classList.toggle("error", error);
  root.hidden = !message;
}
function showError(error) {
  notice(error.message, true);
  if (error.status === 401) disconnect();
}
function connection(value) {
  $("connection").textContent = value;
  $("connection").classList.toggle("connected", value === "Connected");
}
function currentId() {
  try {
    return decodeURIComponent(
      /^#\/sessions\/([^/]+)(?:\/trajectory)?$/.exec(location.hash)?.[1] || "",
    );
  } catch {
    return "";
  }
}
function navigateTrajectory(analysis) {
  if (!controller?.selectedId || analysisMode === analysis) return;
  const id = controller.selectedId;
  history.pushState(
    null,
    "",
    `#/sessions/${encodeURIComponent(id)}${analysis ? "/trajectory" : ""}`,
  );
  select(id, { analysis, fromHistory: true });
}
function setSessionTab(analysis) {
  analysisMode = analysis;
  $("conversation-page").hidden = analysis;
  $("trajectory-page").hidden = !analysis;
  for (const [id, active] of [
    ["session-conversation", !analysis],
    ["session-trajectory", analysis],
  ]) {
    $(id).setAttribute("aria-selected", String(active));
    $(id).tabIndex = active ? 0 : -1;
  }
}
async function select(id, { analysis = false, fromHistory = false } = {}) {
  if (!controller || !id) return;
  if (trajectory.sessionId !== id) trajectory.close();
  setSessionTab(analysis);
  if (!fromHistory)
    history.replaceState(
      null,
      "",
      `#/sessions/${encodeURIComponent(id)}${analysis ? "/trajectory" : ""}`,
    );
  if (controller.selectedId === id && controller.projection) {
    if (analysis && trajectory.sessionId !== id)
      trajectory.open(controller.api, id, controller.projection.cursor);
    return;
  }
  $("session-conversation").disabled = true;
  $("session-trajectory").disabled = true;
  notice("");
  feed.snapshot = null;
  feed.reset();
  $("session-title").textContent = "Loading session…";
  $("task-status").textContent = "Loading";
  $("run-failure").hidden = true;
  $("session-panels").hidden = true;
  $("message").value = "";
  $("message").disabled = true;
  $("controls").replaceChildren();
  list.update(controller.sessions, id);
  try {
    await controller.select(id);
    if (controller?.selectedId === id && analysisMode)
      trajectory.open(controller.api, id, controller.projection.cursor);
  } catch (error) {
    showError(error);
  }
}

function renderState(state) {
  $("session-panels").hidden = false;
  $("session-title").textContent = state.title || "New session";
  $("task-status").textContent = state.task.state;
  $("task-status").className = `status ${state.task.state}`;
  const failure = state.run?.failure;
  $("run-failure").hidden = state.task.state !== "failed";
  $("run-failure").textContent = failure
    ? `${failure.code || failure.type || "Execution failed"}: ${failure.message}`
    : state.run?.reason ||
      "Task execution failed. Resume or send new guidance to retry.";
  panels.update(state);
  $("session-conversation").disabled = false;
  $("session-trajectory").disabled = false;
  trajectory.updateState(state);
  feed.snapshot = state;
  feed.syncMessages(state.messages || []);
  $("history").disabled = !controller.nextBefore;
  const request = state.input_request,
    pending = request?.state === "pending";
  $("pending-input").hidden = !pending;
  if (pending)
    $("pending-input").textContent =
      `${request.kind === "recovery" ? "Verify the previous operation" : "Loom needs your input"}\n\n${request.question}`;
  $("composer-label").textContent = pending ? "Your answer" : "Message";
  $("send").textContent = pending ? "Send answer" : "Send message";
  $("redirect").hidden = !pending || request.kind === "recovery";
  $("composer-hint").textContent =
    pending && request.kind === "recovery"
      ? "Answer with the requested resolution JSON."
      : "Ctrl / ⌘ + Enter to send";
  $("message").disabled = state.task.state === "completed";
  $("send").disabled = sending || $("message").disabled;
  if (
    state.run?.reason &&
    ["paused", "recovering", "failed"].includes(state.task.state)
  )
    notice(state.run.reason);
}

function renderControls() {
  $("controls").replaceChildren();
  for (const command of catalog.commands) {
    const button = element(
      "button",
      command.id === "stop_run" ? "danger" : "",
      command.label,
    );
    button.dataset.command = command.id;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await controller.command(command.id);
      } catch (error) {
        showError(error);
      } finally {
        button.disabled = false;
      }
    });
    $("controls").append(button);
  }
}

async function login(token) {
  controller?.disconnect();
  const next = new SessionController(new SessionApi({ token }));
  await next.connect();
  controller = next;
  catalog = next.catalog;
  next.subscribe(({ type, detail }) => {
    if (controller !== next) return;
    if (type === "connection") connection(detail);
    else if (type === "sessions") list.update(detail, next.selectedId);
    else if (type === "restore") {
      feed.restore(detail);
      renderControls();
    } else if (type === "state") renderState(detail);
    else if (type === "event") feed.append(detail);
    else if (type === "history") {
      feed.older(detail, next.projection.snapshot);
      $("history").disabled = !next.nextBefore;
    } else if (type === "notice") notice(detail);
    else if (type === "error") showError(detail);
    else if (type === "unauthorized") disconnect();
  });
  list.update(next.sessions, null);
  connection("Connected");
  $("new-session").disabled = false;
  populateCreate();
  $("auth-dialog").close();
  $("credential").value = "";
  $("credential-file").value = "";
  const requested = currentId(),
    chosen =
      next.sessions.find((session) => session.session_id === requested) ||
      next.sessions[0];
  if (chosen)
    await select(chosen.session_id, {
      analysis: location.hash.endsWith("/trajectory"),
    });
}

function disconnect() {
  trajectory.close();
  setSessionTab(false);
  $("session-conversation").disabled = true;
  $("session-trajectory").disabled = true;
  if (controller) {
    controller.disconnect();
    controller.api.token = "";
  }
  controller = null;
  catalog = null;
  connection("Disconnected");
  list.update([], null);
  $("new-session").disabled = true;
  $("message").disabled = true;
  $("send").disabled = true;
  $("controls").replaceChildren();
  feed.snapshot = null;
  feed.reset();
  $("pending-input").hidden = true;
  $("run-failure").hidden = true;
  $("session-title").textContent = "Connect to Loom";
  $("session-panels").hidden = true;
  if (!$("auth-dialog").open) $("auth-dialog").showModal();
}

function populateCreate() {
  $("session-panels").hidden = false;
  $("create-template").replaceChildren();
  for (const template of [
    ...catalog.templates,
    { id: "custom", label: "Custom specification" },
  ]) {
    const option = element("option", "", template.label);
    option.value = template.id;
    $("create-template").append(option);
  }
  $("create-model").replaceChildren(element("option", "", "Service default"));
  $("create-model").firstChild.value = "";
  for (const model of catalog.models) {
    const option = element("option", "", model.label);
    option.value = model.id;
    $("create-model").append(option);
  }
  templateChanged();
}
function templateChanged() {
  const id = $("create-template").value,
    template = catalog?.templates.find((item) => item.id === id);
  $("template-description").textContent =
    template?.description ||
    "Provide a task specification for the installed plugins.";
  $("workspace-field").hidden = !template?.requires_workspace;
  $("create-workspace").required = !!template?.requires_workspace;
  $("spec-field").hidden = id !== "custom";
  if (id === "custom" && !$("create-spec").value)
    $("create-spec").value = JSON.stringify(
      catalog.templates[0].task_spec,
      null,
      2,
    );
}

async function send(redirect = false) {
  if (!controller || sending) return;
  const active = controller,
    id = active.selectedId,
    generation = active.generation;
  sending = true;
  $("send").disabled = true;
  try {
    await active.send($("message").value, redirect);
    if (active.current(id, generation)) $("message").value = "";
  } catch (error) {
    if (active.current(id, generation)) showError(error);
  } finally {
    sending = false;
    if (controller?.projection) renderState(controller.projection.snapshot);
  }
}

async function openArtifact(digest) {
  const active = controller,
    id = active.selectedId;
  try {
    const value = await active.api.artifact(id, digest);
    if (controller !== active || controller.selectedId !== id) return;
    const dialog = element("dialog"),
      heading = element("div", "dialog-heading"),
      close = element("button", "quiet", "×");
    close.setAttribute("aria-label", "Close artifact");
    close.addEventListener("click", () => dialog.close());
    heading.append(element("h2", "", "Artifact"), close);
    dialog.append(heading, renderResult(reportContent(value)));
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
  } catch (error) {
    showError(error);
  }
}

$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("connect").disabled = true;
  $("auth-error").textContent = "";
  try {
    await login($("credential").value.trim());
  } catch (error) {
    $("auth-error").textContent = error.message;
  } finally {
    $("connect").disabled = false;
  }
});
$("credential-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (file) $("credential").value = (await file.text()).trim();
});
$("auth-dialog").addEventListener("cancel", (event) => event.preventDefault());
$("disconnect").addEventListener("click", disconnect);
$("refresh-sessions").addEventListener("click", () =>
  controller?.refresh().catch(showError),
);
$("new-session").addEventListener("click", () => {
  $("create-error").textContent = "";
  $("create-dialog").showModal();
});
$("create-template").addEventListener("change", templateChanged);
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("create-submit").disabled = true;
  $("create-error").textContent = "";
  try {
    const template = catalog.templates.find(
        (item) => item.id === $("create-template").value,
      ),
      payload = {};
    if ($("create-objective").value.trim())
      payload.objective = $("create-objective").value.trim();
    if ($("create-title").value.trim())
      payload.title = $("create-title").value.trim();
    if ($("create-model").value) payload.model = $("create-model").value;
    if ($("create-template").value === "custom")
      payload.task_spec = JSON.parse($("create-spec").value);
    else if (template.task_spec)
      payload.task_spec = structuredClone(template.task_spec);
    if (template?.requires_workspace)
      payload.workspace = $("create-workspace").value.trim();
    const active = controller,
      result = await active.api.create(payload);
    if (controller !== active) return;
    $("create-dialog").close();
    $("create-objective").value = "";
    $("create-title").value = "";
    await active.refresh();
    if (controller === active) await select(result.session_id);
  } catch (error) {
    $("create-error").textContent = error.message;
  } finally {
    $("create-submit").disabled = false;
  }
});
for (const button of document.querySelectorAll("[data-close]"))
  button.addEventListener("click", () => $(button.dataset.close).close());
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    send();
  }
});
$("redirect").addEventListener("click", () => send(true));
$("toggle-details").addEventListener("click", () => feed.toggleAll());
$("history").addEventListener("click", async () => {
  $("history").disabled = true;
  try {
    await controller.older();
  } catch (error) {
    showError(error);
  } finally {
    $("history").disabled = !controller?.nextBefore;
  }
});
const sessionTabs = [$("session-conversation"), $("session-trajectory")];
for (const [index, tab] of sessionTabs.entries()) {
  tab.addEventListener("click", () => navigateTrajectory(index === 1));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
    if (sessionTabs[next].disabled) return;
    sessionTabs[next].focus();
    navigateTrajectory(next === 1);
  });
}
window.addEventListener("hashchange", () =>
  select(currentId(), {
    analysis: location.hash.endsWith("/trajectory"),
    fromHistory: true,
  }),
);
window.addEventListener("pagehide", () => {
  trajectory.close();
  controller?.disconnect();
});
$("auth-dialog").showModal();
