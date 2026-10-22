import { element, renderMarkdown } from "../markdown.mjs";
import { verificationArtifactSections } from "../activity/event-details.mjs";

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
function renderSection(item, loadArtifact) {
  const node = element("section", "activity-section");
  if (item.label) node.append(element("small", "activity-label", item.label));
  const value = item.value;
  if (item.kind === "visual-evidence") {
    const view = renderVerificationArtifact(value, loadArtifact);
    if (view) node.append(view);
  } else if (item.kind === "acceptance-criterion") {
    node.append(element("p", "", value.description || "No criterion description recorded"));
    if (Array.isArray(value.scope) && value.scope.length)
      node.append(element("p", "muted", `Scope: ${value.scope.join(", ")}`));
    const methods = {semantic: "Review the result and supporting evidence", command: "Run a verification command",
      artifact: "Check the deliverable", source: "Check source evidence", visual: "Inspect actual browser screenshots and assertions",
      external: "External confirmation"};
    if (value.verifier) node.append(element("p", "muted", `Verification: ${methods[value.verifier] || value.verifier}`));
    const input = value.check?.input;
    if (input?.command || Array.isArray(input?.argv))
      node.append(element("pre", "", input.command || input.argv.join(" ")));
    if (value.check?.path) node.append(element("p", "", `Deliverable: ${value.check.path}`));
  } else if (item.kind === "acceptance-check") {
    const row = element("details", "acceptance-check");
    row.dataset.key = `criterion:${value.id}`;
    row.open = ["failed", "blocked"].includes(value.status);
    row.append(element("summary", "", `${value.deferred ? 'pending review' : value.status || "pending"} · ${value.description || value.id}`));
    if (value.reason) row.append(element("p", "", value.reason));
    if (value.evidence_ids?.length)
      row.append(element("p", "muted", `Evidence: ${value.evidence_ids.join(", ")}`));
    if (value.artifact?.sha256 && loadArtifact) {
      const button = element("button", "quiet", "View verification evidence");
      button.type = "button";
      const evidence = element("div", "verification-evidence");
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const content = await loadArtifact(value.artifact.sha256);
          evidence.replaceChildren(renderVerificationArtifact(content, loadArtifact) || element("pre", "activity-raw-content", JSON.stringify(content, null, 2)));
        } catch (error) {
          evidence.textContent = `Could not load evidence: ${error.message}`;
          button.disabled = false;
        }
      });
      row.append(button, evidence);
    }
    node.append(row);
  } else if (item.kind === "plan") {
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
        const label = `${hit.path || hit.file || hit.document || hit.title || hit.url || "Match"}${(hit.line ?? hit.start_line) != null ? `:${hit.line ?? hit.start_line}` : ""}`;
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
        if (hit.page_number)
          row.append(
            element("small", "activity-hint", `Page ${hit.page_number}`),
          );
        if (hit.source_id)
          row.append(element("small", "activity-hint", hit.source_id));
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
export function renderVerificationArtifact(value, loadArtifact) {
  if (value?.media_type === 'image/png' && /^data:image\/png;base64,/.test(value.data_url || '')) {
    const image = element('img', 'visual-original'); image.src = value.data_url; image.alt = 'Original browser screenshot'; return image;
  }
  const frames = value?.frames || value?.detail?.frames;
  if (Array.isArray(frames) && frames.length && loadArtifact) {
    const root = element('div', 'visual-evidence');
    root.append(element('p', 'muted small', 'Actual browser captures · image collection is separate from acceptance judgment.'));
    const findings = value.judgment?.findings || value.detail?.judgment?.findings || value.result?.findings || [];
    for (const frame of frames) {
      const figure = element('figure', 'visual-frame'), caption = element('figcaption', '', `${frame.id} · ${frame.width} × ${frame.height}`);
      const button = element('button', 'quiet', 'View screenshot'); button.type = 'button';
      figure.append(caption, button); root.append(figure);
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const content = await loadArtifact(frame.artifact.sha256);
          const image = renderVerificationArtifact(content);
          if (!image) throw new Error('Image artifact unavailable');
          const display = element('div', 'visual-image'); display.append(image);
          for (const finding of findings.filter(f => f.evidence_id === frame.id && f.region?.length === 4)) {
            if (!finding.region.every(v => Number.isFinite(v) && v >= 0 && v <= 1)) continue;
            const box = element('span', 'visual-region'); box.title = finding.observation;
            const [x,y,w,h] = finding.region; Object.assign(box.style, {left:`${x*100}%`,top:`${y*100}%`,width:`${w*100}%`,height:`${h*100}%`}); display.append(box);
          }
          if (display.querySelector('.visual-region')) {
            const toggle = element('button', 'quiet', 'Hide findings'); toggle.type = 'button';
            toggle.addEventListener('click', () => {
              const hide = toggle.textContent === 'Hide findings';
              display.querySelectorAll('.visual-region').forEach(box => box.hidden = hide);
              toggle.textContent = hide ? 'Show findings' : 'Hide findings';
            });
            figure.append(toggle);
          }
          figure.append(display); button.remove();
        } catch (error) {button.disabled = false; button.textContent = `Could not load screenshot: ${error.message}`;}
      });
    }
    if (value.url) root.append(element('p', 'muted small', `Target: ${value.url}`));
    if (value.result?.reason || value.judgment?.reason) root.append(element('p', '', value.result?.reason || value.judgment.reason));
    const raw = element('details'); raw.append(element('summary', '', 'Capture metadata and raw evidence'), element('pre', 'activity-raw-content', JSON.stringify(value, null, 2))); root.append(raw);
    return root;
  }
  const sections = verificationArtifactSections(value);
  if (!sections) return null;
  const root = element("div", "verification-evidence");
  root.append(...sections.map(item => renderSection(item, loadArtifact)));
  const raw = element("details");
  raw.append(element("summary", "", "Raw verification evidence"));
  raw.addEventListener("toggle", () => {
    if (raw.open && !raw.querySelector("pre")) raw.append(element("pre", "activity-raw-content", JSON.stringify(value, null, 2)));
  });
  root.append(raw);
  return root;
}

export class ActivityRow {
  constructor(key, { inspect, expand, loadArtifact }) {
    this.loadArtifact = loadArtifact;
    this.node = element("details", "event-row");
    this.node.dataset.key = key;
    this.summary = element("summary");
    this.status = element("span", "activity-status");
    this.title = element("span", "activity-title");
    this.tool = element("span", "activity-tool");
    this.subject = element("span", "activity-subject");
    this.outcome = element("span", "activity-outcome");
    this.timestamp = element("time", "activity-timestamp");
    this.summary.append(
      this.status,
      this.title,
      this.tool,
      this.subject,
      this.outcome,
      this.timestamp,
    );
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
    set(this.tool, activity.toolName ? `Tool · ${activity.toolName}` : "");
    this.tool.hidden = !activity.toolName;
    set(this.subject, activity.subject);
    set(this.outcome, activity.outcome);
    const at = activity.timestamp ? new Date(activity.timestamp) : null;
    if (at && Number.isFinite(at.getTime())) {
      const time = at.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"});
      const full = at.toLocaleString(undefined, {timeZoneName: "short"});
      set(this.timestamp, time);
      this.timestamp.dateTime = at.toISOString();
      this.timestamp.title = full;
      this.timestamp.setAttribute("aria-label", `Action time: ${full}`);
    } else {
      set(this.timestamp, "—");
      this.timestamp.removeAttribute("datetime");
      this.timestamp.title = "Timestamp unavailable";
      this.timestamp.setAttribute("aria-label", "Timestamp unavailable");
    }
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
      const node = renderSection(item, this.loadArtifact);
      if (previous) previous.node.replaceWith(node);
      else this.content.append(node);
      this.sections[index] = { signature, node };
    });
    for (const section of this.sections.slice(items.length))
      section.node.remove();
    this.sections.length = items.length;
  }
}
