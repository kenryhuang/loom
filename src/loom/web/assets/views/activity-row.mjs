import { element, renderMarkdown } from "../markdown.mjs";

const previewLimit = 6000;
function preview(value) {
  return String(value ?? "").slice(0, previewLimit);
}
function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}
function renderSection(item) {
  const node = element("section", "activity-section");
  if (item.label) node.append(element("small", "activity-label", item.label));
  const value = item.value;
  if (item.kind === "plan") {
    const list = element("ul", "activity-plan");
    for (const entry of Array.isArray(value) ? value : []) {
      const row = element("li");
      row.append(
        element("span", "activity-plan-state", entry.status || "pending"),
        element(
          "span",
          "",
          entry.content || entry.title || entry.description || String(entry),
        ),
      );
      list.append(row);
    }
    node.append(list);
  } else if (item.kind === "hits") {
    const list = element("ul", "activity-hits");
    for (const hit of value.slice(0, 20)) {
      const row = element("li");
      if (typeof hit === "string") row.textContent = preview(hit);
      else {
        const label = `${hit.path || hit.file || hit.title || hit.url || "Match"}${hit.line != null ? `:${hit.line}` : ""}`;
        const href = safeLink(hit.url);
        const title = element(href ? "a" : "code", "", label);
        if (href) {
          title.href = href;
          title.target = "_blank";
          title.rel = "noopener noreferrer";
        }
        row.append(
          title,
          element("p", "", preview(hit.text || hit.snippet || hit.content)),
        );
      }
      list.append(row);
    }
    node.append(list);
    if (value.length > 20)
      node.append(
        element(
          "p",
          "activity-hint",
          "Showing 20 matches. Open raw records for the full result.",
        ),
      );
  } else if (item.kind === "link" && safeLink(value)) {
    const link = element("a", "", value);
    link.href = safeLink(value);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    node.append(link);
  } else if (item.kind === "markdown")
    node.append(renderMarkdown(preview(value)));
  else if (["code", "lines", "diff"].includes(item.kind)) {
    const pre = element("pre", `activity-code ${item.kind}`);
    let content = preview(value);
    if (item.kind === "lines")
      content = content
        .split("\n")
        .slice(0, 80)
        .map((line, index) => `${Number(item.start || 1) + index}  ${line}`)
        .join("\n");
    pre.append(element("code", "", content));
    node.append(pre);
  } else node.append(element("p", "", preview(value)));
  if (
    String(value ?? "").length > previewLimit ||
    (item.kind === "lines" && String(value).split("\n").length > 80)
  )
    node.append(
      element(
        "p",
        "activity-hint",
        "Preview shortened. Open raw records for the full content.",
      ),
    );
  return node;
}

/** Stable summary, controls, and section nodes; closed rows do no detail work. */
export class ActivityRow {
  constructor(key, { inspect, expand }) {
    this.node = element("details", "event-row");
    this.node.dataset.key = key;
    this.summary = element("summary");
    this.status = element("span", "activity-status");
    this.title = element("span", "activity-title");
    this.subject = element("span", "activity-subject");
    this.outcome = element("span", "activity-outcome");
    this.summary.append(this.status, this.title, this.subject, this.outcome);
    this.body = element("div", "event-detail");
    this.content = element("div", "activity-content");
    this.raw = element("button", "quiet activity-raw", "View raw records");
    this.raw.type = "button";
    this.raw.addEventListener("click", () => inspect(this.raw));
    this.body.append(this.content, this.raw);
    this.node.append(this.summary, this.body);
    this.sections = [];
    this.node.addEventListener("toggle", () => {
      if (this.node.open) {
        this.renderDetails();
        expand();
      }
    });
  }
  update(activity) {
    this.activity = activity;
    this.node.className =
      `event-row ${activity.state === "completed" ? "" : activity.state}`.trim();
    const set = (node, value) => {
      if (node.textContent !== value) node.textContent = value;
    };
    set(
      this.status,
      { running: "●", completed: "✓", failed: "!" }[activity.state] || "·",
    );
    this.status.setAttribute("aria-label", activity.state);
    set(this.title, activity.title);
    set(this.subject, activity.subject);
    set(this.outcome, activity.outcome);
    this.subject.title = activity.subject;
    if (this.node.open) this.renderDetails();
  }
  renderDetails() {
    if (!this.activity) return;
    const items = this.activity.sections.filter(
      (item) => item.value != null && item.value !== "",
    );
    if (this.activity.pending)
      items.push({ label: "", value: "Full result loads when expanded…" });
    // Compare bounded presentation values, never the entire request/artifact.
    items.forEach((item, index) => {
      const signature = JSON.stringify(item);
      const previous = this.sections[index];
      if (previous?.signature === signature) return;
      const selection = document.getSelection();
      if (
        previous &&
        selection &&
        !selection.isCollapsed &&
        previous.node.contains(selection.anchorNode)
      )
        return;
      const node = renderSection(item);
      if (previous) previous.node.replaceWith(node);
      else this.content.append(node);
      this.sections[index] = { signature, node };
    });
    for (const section of this.sections.slice(items.length))
      section.node.remove();
    this.sections.length = items.length;
  }
}
