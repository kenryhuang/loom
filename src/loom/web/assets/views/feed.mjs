import { builtinRenderers, reportContent } from "../renderers.mjs";
import { codePoints, streamKey } from "../state.mjs";
import { element, renderResult } from "../markdown.mjs";

export class FeedView {
  constructor(
    root,
    { renderers = builtinRenderers(), loadArtifact, maxEvents = 1000 } = {},
  ) {
    this.root = root;
    this.renderers = renderers;
    this.loadArtifact = loadArtifact;
    this.maxEvents = maxEvents;
    this.events = [];
    this.emptyState = root.querySelector(".empty-state");
    this.reset();
  }
  reset() {
    this.groups = new Map();
    this.messages = new Map();
    this.streams = {};
    this.origins = {};
    this.root.replaceChildren(...(this.emptyState ? [this.emptyState] : []));
  }
  restore({ snapshot, events }) {
    const opened = this.opened();
    this.snapshot = snapshot;
    this.events = [];
    for (const event of events) this.retain(event);
    this.events = this.events.slice(-this.maxEvents);
    this.reset();
    for (const event of this.events) this.present(event);
    this.seedStreams(snapshot);
    this.syncMessages(snapshot.messages || []);
    this.reopen(opened);
    this.root.scrollTop = this.root.scrollHeight;
  }
  opened() {
    return new Set(
      [...this.root.querySelectorAll("details[open]")].map(
        (node) => node.dataset.key,
      ),
    );
  }
  reopen(keys) {
    for (const node of this.root.querySelectorAll("details"))
      if (keys.has(node.dataset.key)) node.open = true;
  }
  older(events, snapshot) {
    const opened = this.opened(),
      height = this.root.scrollHeight,
      top = this.root.scrollTop;
    const unique = new Map(
      [...events, ...this.events].map((event) => [event.seq, event]),
    );
    this.events = [];
    for (const event of [...unique.values()].sort((a, b) => a.seq - b.seq))
      this.retain(event);
    this.events = this.events.slice(0, this.maxEvents);
    this.reset();
    for (const event of this.events) this.present(event);
    this.seedStreams(snapshot);
    this.syncMessages(snapshot.messages || []);
    this.reopen(opened);
    this.root.scrollTop = top + this.root.scrollHeight - height;
  }
  append(event) {
    const follow =
      this.root.scrollHeight - this.root.scrollTop - this.root.clientHeight <
      70;
    this.retain(event);
    this.present(event);
    if (this.events.length > this.maxEvents) {
      const opened = this.opened();
      this.events = this.events.slice(-this.maxEvents);
      this.reset();
      for (const retained of this.events) this.present(retained);
      if (this.snapshot) {
        this.seedStreams(this.snapshot);
        this.syncMessages(this.snapshot.messages || []);
      }
      this.reopen(opened);
    }
    if (follow) this.root.scrollTop = this.root.scrollHeight;
  }
  retain(event) {
    const previous = this.events.at(-1);
    const delta =
      event.type.startsWith("llm.") &&
      event.type.endsWith(".delta") &&
      typeof event.payload.delta === "string";
    if (
      !delta ||
      previous?.type !== event.type ||
      previous.run_id !== event.run_id ||
      streamKey(previous) !== streamKey(event)
    ) {
      this.events.push(event);
      return;
    }
    // Keep consecutive chunks as one retained stream, without consuming the
    // history window on each token. The live projection still sees every seq.
    const old = codePoints(previous.payload.delta),
      origin = previous.payload.offset || 0;
    const end = origin + old.length,
      offset = event.payload.offset ?? end;
    if (offset > end) {
      this.events.push(event);
      return;
    }
    const text = [
      ...old,
      ...codePoints(event.payload.delta).slice(Math.max(0, end - offset)),
    ];
    const limit = this.snapshot?.task.limits?.max_window_chars || 240000;
    const dropped = Math.max(0, text.length - limit);
    this.events[this.events.length - 1] = {
      ...previous,
      payload: {
        ...event.payload,
        offset: origin + dropped,
        delta: text.slice(dropped).join(""),
      },
    };
  }
  group(event) {
    this.emptyState?.remove();
    const key = event.run_id || "session";
    if (!this.groups.has(key)) {
      const node = element("details", "process-group"),
        summary = element("summary", "", "Process");
      node.dataset.key = `group:${key}`;
      node.dataset.seq = event.seq || this.snapshot?.event_cursor || 0;
      const rows = element("div", "process-records");
      node.append(summary, rows);
      this.root.append(node);
      this.groups.set(key, {
        node,
        summary,
        rows,
        records: new Map(),
      });
    }
    return this.groups.get(key);
  }
  present(event) {
    if (event.type === "message.created") {
      this.message({ ...event.payload, seq: event.seq });
      return;
    }
    if (event.type === "command.applied") return;
    let context = {};
    if (
      event.type.startsWith("llm.") &&
      event.type.endsWith(".delta") &&
      typeof event.payload.delta === "string"
    ) {
      const key = streamKey(event),
        old = codePoints(this.streams[key]),
        origin = this.origins[key] || 0;
      const offset = event.payload.offset ?? origin + old.length;
      this.streams[key] = [
        ...old,
        ...codePoints(event.payload.delta).slice(
          Math.max(0, origin + old.length - offset),
        ),
      ].join("");
      context = { streamKey: key, streamText: this.streams[key] };
    }
    const descriptor = this.renderers.describe(event, context);
    this.record(this.group(event), descriptor);
  }
  record(group, descriptor) {
    let record = group.records.get(descriptor.key);
    if (!record) {
      const node = element("details", "event-row"),
        summary = element("summary"),
        body = element("div", "event-detail"),
        pre = element("pre");
      node.dataset.key = descriptor.key;
      body.append(pre);
      node.append(summary, body);
      group.rows.append(node);
      record = { node, summary, body, pre, descriptor };
      group.records.set(descriptor.key, record);
      node.addEventListener("toggle", () => this.detail(record));
    }
    const previous = record.descriptor;
    if (descriptor.kind === "tool")
      descriptor = {
        ...descriptor,
        details: { ...previous.details, ...descriptor.details },
      };
    if (previous.artifact?.sha256 !== descriptor.artifact?.sha256)
      record.loaded = false;
    record.descriptor = descriptor;
    record.node.className = `event-row ${descriptor.status || ""}`;
    record.summary.textContent = descriptor.summary;
    if (!record.loaded)
      record.pre.textContent =
        typeof descriptor.details === "string"
          ? descriptor.details
          : JSON.stringify(descriptor.details, null, 2);
    group.summary.textContent = `Process · ${group.records.size} events · ${descriptor.summary}`;
    this.detail(record);
  }
  async detail(record) {
    const digest = record.descriptor.artifact?.sha256;
    if (
      !record.node.open ||
      !digest ||
      record.loaded ||
      record.loading ||
      !this.loadArtifact
    )
      return;
    record.loading = true;
    try {
      const detail = await this.loadArtifact(digest);
      if (record.descriptor.artifact?.sha256 === digest) {
        const value =
          record.descriptor.kind === "tool"
            ? { ...record.descriptor.details, ...detail }
            : detail;
        record.pre.textContent = JSON.stringify(value, null, 2);
        record.loaded = true;
      }
    } catch (error) {
      record.pre.textContent = `Could not load full detail: ${error.message}`;
    } finally {
      record.loading = false;
    }
  }
  seedStreams(snapshot) {
    Object.assign(this.streams, snapshot.streams || {});
    Object.assign(this.origins, snapshot.stream_origins || {});
    for (const [key, text] of Object.entries(snapshot.streams || {})) {
      if (!key.endsWith(":reasoning")) continue;
      const id = key.slice(0, -10),
        event = {
          seq: 0,
          run_id: snapshot.run?.id,
          type: "llm.reasoning.delta",
          payload: { llm_call_id: id },
        };
      const group = this.group(event);
      this.record(
        group,
        this.renderers.describe(event, { streamText: text, streamKey: key }),
      );
    }
  }
  syncMessages(messages) {
    for (const message of messages) this.message(message);
  }
  message(message) {
    if (this.messages.has(message.id)) return;
    this.emptyState?.remove();
    const result = message.role === "assistant",
      node = element(
        result ? "details" : "article",
        `message-card ${result ? "result" : "user"}`,
      );
    node.dataset.key = `message:${message.id}`;
    if (result) {
      node.open = true;
      node.append(element("summary", "", "Result"));
      node.append(renderResult(reportContent(message.content)));
    } else {
      node.append(element("small", "", "You"));
      node.append(element("p", "", message.content));
    }
    node.dataset.seq = message.seq || 0;
    this.messages.set(message.id, node);
    const following = [...this.root.children].find(
      (child) => Number(child.dataset.seq) > message.seq,
    );
    this.root.insertBefore(node, following || null);
  }
}
