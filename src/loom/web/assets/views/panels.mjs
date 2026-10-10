import { element } from "../markdown.mjs";
import { parseBudget } from "../state.mjs";

function panel(title) {
  const root = element("section", "panel");
  root.append(element("h2", "", title));
  return root;
}

export class PanelRegistry {
  constructor() {
    this.factories = new Map();
  }
  register(id, factory) {
    this.factories.set(id, factory);
    return this;
  }
}

export class PanelsView {
  constructor(root, registry, actions) {
    this.widgets = [...registry.factories].map(([id, factory]) => {
      const widget = factory(actions);
      widget.element.dataset.panel = id;
      root.append(widget.element);
      return widget;
    });
  }
  update(state) {
    for (const widget of this.widgets) widget.update(state);
  }
}

export function builtinPanels() {
  return new PanelRegistry()
    .register("info", () => {
      const root = panel("Details"),
        body = element("div");
      root.append(body);
      let signature;
      return {
        element: root,
        update(state) {
          const rows = [
            ["Session", state.session_id],
            ["Workspace", state.task.workspace || "—"],
            ["State", state.task.state],
            ["Run state", state.run?.state || "—"],
            ...(state.run?.reason ? [["Reason", state.run.reason]] : []),
            ...(state.run?.failure
              ? [
                  [
                    "Error",
                    state.run.failure.message ||
                      state.run.failure.code ||
                      "Execution failed",
                  ],
                ]
              : []),
            ...(state.task.objective
              ? [["Objective", state.task.objective]]
              : []),
            [
              "Time budget",
              state.task.limits.max_duration_seconds == null
                ? "—"
                : `${state.task.limits.max_duration_seconds.toLocaleString()} s`,
            ],
            ["Model", state.task.model || "Service default"],
            [
              "Runtime",
              state.task.task_spec?.execution_runtime?.plugin || "native_os",
            ],
            [
              "Tools",
              (state.task.task_spec?.tools?.collections || []).join(", ") ||
                "—",
            ],
          ];
          const next = JSON.stringify(rows);
          if (signature === next) return;
          signature = next;
          body.replaceChildren();
          for (const [label, value] of rows) {
            const row = element("div", "info-row");
            row.append(element("small", "", label), element("span", "", value));
            body.append(row);
          }
        },
      };
    })
    .register("acceptance", (actions) => {
      const root = panel("Task acceptance"), body = element("div");
      root.append(body);
      let signature;
      return {element: root, update(state) {
        const current = state.acceptance_run_id === state.run?.id ? state.acceptance : null;
        const next = JSON.stringify([current, state.acceptance_current_check]);
        if (signature === next) return;
        signature = next;
        body.replaceChildren();
        if (!current) {body.append(element("p", "muted", "Acceptance will be prepared from the task and workspace.")); return;}
        body.append(element("strong", "", (current.state === "not_ready" && !current.plan ? "Preparing plan" : ({not_ready: "Ready to work", verifying: "Verifying", passed: "Passed", needs_repair: "Needs repair", blocked: "Blocked"})[current.state]) || current.state),
          element("p", "muted", current.reason));
        if (state.acceptance_current_check) body.append(element("p", "", state.acceptance_current_check));
        const results = new Map((current.results || []).map(r => [r.criterion_id, r]));
        for (const c of current.plan?.criteria || []) {
          const row = element("details"), result = results.get(c.id);
          row.open = ["failed", "blocked"].includes(result?.status);
          row.append(element("summary", "", `${result?.status || "pending"} · ${c.description}`));
          if (result) row.append(element("p", "", result.reason));
          row.append(element("small", "muted", `${c.verifier} · ${result?.assurance || "recorded check"}`));
          if (result?.artifact?.sha256) {
            const link = element("button", "output-link", "View verification evidence");
            link.addEventListener("click", () => actions.artifact(result.artifact.sha256));
            row.append(link);
          }
          body.append(row);
        }
        for (const missing of current.plan?.unresolved_requirements || []) body.append(element("p", "notice", missing));
        const profile = state.workspace_profile;
        if (profile) body.append(element("p", "muted small", `Workspace: ${profile.workspace} · ${profile.files?.length || 0} discovered entries · ${profile.limitations?.length || 0} probe limitations`));
      }};
    })
    .register("budget", (actions) => {
      const root = panel("Token budget"),
        text = element("div", "muted small"),
        form = element("form", "budget-form"),
        input = element("input"),
        button = element("button", "", "Set");
      input.setAttribute("aria-label", "Token budget");
      input.placeholder = "10M";
      input.inputMode = "decimal";
      form.append(input, button);
      root.append(text, form);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        button.disabled = true;
        try {
          await actions.command("set_token_budget", {
            max_tokens: parseBudget(input.value),
          });
          input.value = "";
        } catch (error) {
          actions.error(error);
        } finally {
          button.disabled = false;
        }
      });
      return {
        element: root,
        update(state) {
          const budget = state.token_budget || {};
          text.textContent = `${(budget.used || 0).toLocaleString()} used / ${(budget.limit || state.task.limits.max_tokens).toLocaleString()} total`;
        },
      };
    })
    .register("knowledge", (actions) => {
      const root = panel("Knowledge bases"),
        text = element("p", "muted"),
        configure = element("button", "quiet", "Configure knowledge bases");
      configure.type = "button";
      configure.addEventListener("click", () => actions.knowledge?.());
      root.append(text, configure);
      return {
        element: root,
        update(state) {
          const ids = state.task.knowledge_base_ids || [];
          text.textContent = ids.length
            ? `${ids.length} attached · Model searches when needed`
            : "No knowledge bases attached";
        },
      };
    })
    .register("workflow", () => {
      const root = panel("Workflow"),
        list = element("ul", "plan-list");
      root.append(list);
      let signature;
      return {
        element: root,
        update(state) {
          const nodes =
            state.workflow?.workflow?.nodes || state.workflow?.nodes;
          const items = nodes
            ? nodes.map((node) => ({
                content: node.objective,
                status: node.status,
                note: node.id,
              }))
            : state.plan?.items || [];
          const next = JSON.stringify(items);
          if (next === signature) return;
          signature = next;
          list.replaceChildren();
          for (const item of items) {
            const node = element("li", item.status, item.content);
            node.title = item.note || "";
            list.append(node);
          }
          if (!items.length)
            list.append(
              element("li", "muted", "The task will choose its next steps."),
            );
        },
      };
    })
    .register("outputs", (actions) => {
      const root = panel("Outputs"),
        body = element("div");
      root.append(body);
      let signature;
      return {
        element: root,
        update(state) {
          const refs = state.output_artifacts || [];
          const next = JSON.stringify(refs);
          if (next === signature) return;
          signature = next;
          body.replaceChildren();
          for (const ref of refs) {
            const button = element(
              "button",
              "output-link",
              `${ref.kind} · ${ref.sha256.slice(0, 10)}`,
            );
            button.addEventListener("click", () =>
              actions.artifact(ref.sha256),
            );
            body.append(button);
            for (const path of ref.workspace_paths || [])
              body.append(
                element(
                  "p",
                  "output-path small",
                  `Saved in workspace: ${path}`,
                ),
              );
          }
          if (!refs.length)
            body.append(
              element(
                "p",
                "muted small",
                "Reports and other outputs appear here.",
              ),
            );
        },
      };
    });
}
