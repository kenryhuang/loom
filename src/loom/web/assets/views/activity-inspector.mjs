import { element } from "../markdown.mjs";

/** A single on-demand inspector; raw JSON never lives in every activity row. */
export class ActivityInspector {
  constructor({ load, trajectory, loadArtifact }) {
    this.load = load;
    this.loadArtifact = loadArtifact;
    this.trajectory = trajectory;
  }
  close() {
    this.generation = (this.generation || 0) + 1;
    this.dialog?.remove();
    this.dialog = null;
    this.trigger?.focus();
  }
  async open(record, trigger) {
    this.close();
    this.trigger = trigger;
    const generation = this.generation;
    const dialog = element("dialog", "activity-inspector");
    this.dialog = dialog;
    const heading = element("h2", "", "Raw activity records");
    heading.id = "activity-inspector-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const close = element("button", "quiet", "Close");
    close.type = "button";
    close.addEventListener("click", () => this.close());
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      this.close();
    });
    const header = element("header");
    header.append(heading, close);
    const status = element("p", "activity-hint", "Loading…");
    const pre = element("pre", "activity-raw-content");
    const controls = element("div", "activity-inspector-actions");
    const copy = element("button", "quiet", "Copy JSON");
    copy.type = "button";
    copy.disabled = true;
    copy.addEventListener("click", async () => {
      try {
        await document.defaultView.navigator.clipboard.writeText(this.raw);
        copy.textContent = "Copied";
      } catch {
        status.textContent = "Copy unavailable. Select the text to copy it.";
      }
    });
    controls.append(copy);
    if (this.trajectory) {
      const link = element("button", "quiet", "Open session trajectory");
      link.type = "button";
      link.addEventListener("click", () => {
        this.close();
        this.trajectory(record.descriptor);
      });
      controls.append(link);
    }
    const more = element("button", "quiet", "Show more");
    more.type = "button";
    more.hidden = true;
    const render = () => {
      pre.textContent = this.raw.slice(0, this.limit);
      more.hidden = this.limit >= this.raw.length;
    };
    more.addEventListener("click", () => {
      this.limit += 24000;
      render();
    });
    dialog.append(header, controls, status, pre, more);
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    close.focus();
    try {
      await this.load(record);
      if (generation !== this.generation) return;
      const refs = [
        ...new Set(
          [record, ...(record.related || [])].flatMap((item) =>
            (item.descriptor.sources || [])
              .map((source) => source.artifact?.sha256)
              .filter(Boolean),
          ),
        ),
      ];
      const artifacts = this.loadArtifact
        ? Object.fromEntries(
            await Promise.all(
              refs.map(async (digest) => [
                digest,
                await this.loadArtifact(digest),
              ]),
            ),
          )
        : undefined;
      if (generation !== this.generation) return;
      this.raw = JSON.stringify(
        {
          events: record.descriptor.sources,
          source_artifacts: artifacts,
          detail: record.descriptor.details,
          related_plan_calls: record.related?.map((item) => ({
            events: item.descriptor.sources,
            detail: item.descriptor.details,
          })),
        },
        null,
        2,
      );
      copy.disabled = false;
      this.limit = 24000;
      render();
      status.textContent =
        "Recorded input, output and source event references. Long records are shown in sections.";
    } catch (error) {
      if (generation !== this.generation) return;
      status.textContent = `Could not load full records: ${error.message}`;
      this.raw = JSON.stringify(
        {
          events: record.descriptor.sources,
          detail: record.descriptor.details,
          related_plan_calls: record.related?.map((item) => ({
            events: item.descriptor.sources,
            detail: item.descriptor.details,
          })),
        },
        null,
        2,
      );
      copy.disabled = false;
      this.limit = 24000;
      render();
    }
  }
}
