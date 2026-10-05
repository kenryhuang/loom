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
    .register("workflow", () => {
      const root = panel("Workflow"),
        list = element("ol", "plan-list");
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
