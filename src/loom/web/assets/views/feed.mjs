import {
  builtinRenderers,
  modelSummary,
  reportContent,
} from "../renderers.mjs";
import { codePoints, streamKey } from "../state.mjs";
import { element, renderResult } from "../markdown.mjs";

export class FeedView {
  constructor(
    root,
    {
      renderers = builtinRenderers(),
      loadArtifact,
      loadProcess,
      maxEvents = 1000,
      onDetailsChange = () => {},
    } = {},
  ) {
    this.root = root;
    this.renderers = renderers;
    this.loadArtifact = loadArtifact;
    this.loadProcess = loadProcess;
    this.maxEvents = maxEvents;
    this.onDetailsChange = onDetailsChange;
    this.events = [];
    this.emptyState = root.querySelector(".empty-state");
    root.addEventListener("toggle", () => this.detailsChanged(), true);
    this.reset();
  }
  reset({ preserveDetails = false } = {}) {
    if (!preserveDetails) {
      this.detailMode = null;
      this.processes = [];
      this.processPages = new Map();
    }
    this.groups = new Map();
    this.messages = new Map();
    this.blocks = new Map();
    this.streams = {};
    this.origins = {};
    this.root.replaceChildren(...(this.emptyState ? [this.emptyState] : []));
    this.detailsChanged();
  }
  restore({ snapshot, events, processes = [] }) {
    const opened = this.detailStates();
    this.snapshot = snapshot;
    this.events = [];
    for (const event of events) this.retain(event);
    this.events = this.events.slice(-this.maxEvents);
    this.reset({ preserveDetails: true });
    this.processes = processes;
    this.seedProcesses();
    for (const event of this.events) this.present(event);
    this.seedStreams(snapshot);
    this.orderRecords();
    this.syncMessages(snapshot.messages || []);
    this.reopen(opened);
    this.root.scrollTop = this.root.scrollHeight;
  }
  detailStates() {
    return new Map(
      [...this.root.querySelectorAll("details")].map((node) => [
        node.dataset.key,
        node.open,
      ]),
    );
  }
  reopen(keys) {
    for (const node of this.root.querySelectorAll("details"))
      if (keys.has(node.dataset.key)) node.open = keys.get(node.dataset.key);
    this.detailsChanged();
  }
  detailsChanged() {
    const details = [...this.root.querySelectorAll("details")];
    this.onDetailsChange({
      available: details.length > 0,
      expanded: details.length > 0 && details.every((node) => node.open),
    });
  }
  toggleAll() {
    const details = [...this.root.querySelectorAll("details")];
    this.detailMode = !details.every((node) => node.open);
    for (const node of details) node.open = this.detailMode;
    for (const group of this.groups.values())
      for (const record of group.records.values()) this.detail(record);
    this.detailsChanged();
  }
  older(events, snapshot) {
    const opened = this.detailStates(),
      height = this.root.scrollHeight,
      top = this.root.scrollTop;
    const unique = new Map(
      [...events, ...this.events].map((event) => [event.seq, event]),
    );
    this.events = [];
    for (const event of [...unique.values()].sort((a, b) => a.seq - b.seq))
      this.retain(event);
    this.events = this.events.slice(0, this.maxEvents);
    this.reset({ preserveDetails: true });
    this.seedProcesses();
    for (const event of this.events) this.present(event);
    this.seedStreams(snapshot);
    this.orderRecords();
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
      const opened = this.detailStates();
      this.events = this.events.slice(-this.maxEvents);
      this.reset({ preserveDetails: true });
      this.seedProcesses();
      for (const retained of this.events) this.present(retained);
      if (this.snapshot) {
        this.seedStreams(this.snapshot);
        this.orderRecords();
        this.syncMessages(this.snapshot.messages || []);
      }
      this.reopen(opened);
    }
    if (follow) this.root.scrollTop = this.root.scrollHeight;
  }
  retain(event) {
    const descriptor = this.renderers.describe(event);
    if (descriptor.hidden && !descriptor.affectsProcess) return;
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
    let process = this.processes.find(
      (item) =>
        item.run_id === event.run_id &&
        event.seq >= item.start_seq &&
        event.seq <= item.end_seq,
    );
    if (!process) {
      const latest = this.processes
        .filter((item) => item.run_id === event.run_id)
        .at(-1);
      if (latest && (!event.seq || event.seq > latest.end_seq)) {
        if (event.type === "run.started" && latest.state === "completed") {
          process = {
            id: `${event.run_id}:${event.seq}`,
            run_id: event.run_id,
            start_seq: event.seq,
            end_seq: event.seq,
            state: "running",
            milestones: [],
          };
          this.processes.push(process);
        } else process = latest;
      }
    }
    if (!process && this.loadProcess && event.type === "run.started") {
      process = {
        id: `${event.run_id}:${event.seq}`,
        run_id: event.run_id,
        start_seq: event.seq,
        end_seq: event.seq,
        state: "running",
        milestones: [],
      };
      this.processes.push(process);
    }
    const key = process?.id || event.run_id || "session";
    if (event.run_id && !this.groups.has(key) && this.groups.has("session")) {
      const pending = this.groups.get("session");
      this.groups.delete("session");
      pending.node.dataset.key = `group:${key}`;
      this.groups.set(key, pending);
    }
    if (!this.groups.has(key)) {
      const node = element("details", "process-group"),
        summary = element("summary", "", "Process");
      node.dataset.key = `group:${key}`;
      node.dataset.seq =
        process?.start_seq || event.seq || this.snapshot?.event_cursor || 0;
      node.open = this.detailMode === true;
      const rows = element("div", "process-records");
      node.append(summary, rows);
      this.groups.set(key, {
        node,
        summary,
        rows,
        process,
        failures: new Set(),
        records: new Map(),
      });
      this.layout();
    }
    const group = this.groups.get(key);
    if (process) {
      group.process = process;
      group.node.dataset.seq = process.start_seq;
      this.processHistoryButton(group);
    }
    return group;
  }
  seedProcesses() {
    for (const process of this.processes) {
      const group = this.group({
        run_id: process.run_id,
        seq: process.start_seq,
      });
      group.node.dataset.endSeq = process.end_seq;
      group.state = process.state;
      for (const event of process.milestones) this.present(event);
      const cached = this.processPages.get(process.id);
      for (const event of cached?.events || []) this.present(event);
      this.processSummary(group);
    }
  }
  processHistoryButton(group) {
    if (!this.loadProcess || group.historyButton) return;
    const process = group.process,
      cached = this.processPages.get(process.id);
    const button = element(
      "button",
      "quiet process-history",
      cached?.nextBefore === null
        ? "Process history loaded"
        : "Load process history",
    );
    button.disabled = cached?.nextBefore === null;
    group.node.append(button);
    group.historyButton = button;
    const load = () => this.history(group);
    button.addEventListener("click", load);
    group.node.addEventListener("toggle", () => {
      if (group.node.open && !this.processPages.has(process.id)) load();
    });
  }
  async history(group) {
    if (!this.loadProcess || group.loading) return;
    const process = group.process,
      cached = this.processPages.get(process.id);
    if (cached?.nextBefore === null) return;
    group.loading = true;
    group.historyButton.disabled = true;
    group.historyButton.textContent = "Loading process history…";
    try {
      const page = await this.loadProcess(
        process,
        cached?.nextBefore || process.end_seq + 1,
      );
      if (this.groups.get(process.id) !== group) return;
      const events = [
        ...new Map(
          [...(cached?.events || []), ...page.events].map((event) => [
            event.seq,
            event,
          ]),
        ).values(),
      ].sort((a, b) => a.seq - b.seq);
      this.processPages.set(process.id, {
        events,
        nextBefore: page.next_before,
      });
      for (const event of page.events) this.present(event);
      // Insert older rows chronologically without changing expansion state.
      this.orderRecords(group);
      group.historyButton.textContent = page.next_before
        ? "Load earlier process events"
        : "Process history loaded";
      group.historyButton.disabled = !page.next_before;
      this.processSummary(group);
    } catch (error) {
      group.historyButton.textContent = `Retry loading process history: ${error.message}`;
      group.historyButton.disabled = false;
    } finally {
      group.loading = false;
    }
  }
  processSummary(group) {
    const errors = group.failures.size;
    group.summary.textContent = `Process · ${group.records.size} events${errors ? ` · ${errors} failures` : ""}${group.state ? ` · ${group.state}` : ""}${group.latest ? ` · ${group.latest}` : ""}`;
    group.summary.classList.toggle("has-failures", errors > 0);
  }
  orderRecords(group) {
    for (const current of group ? [group] : this.groups.values()) {
      const records = [...current.records.values()].sort(
        (a, b) => a.seq - b.seq,
      );
      records.forEach((record, index) => {
        if (current.rows.children[index] !== record.node)
          current.rows.insertBefore(
            record.node,
            current.rows.children[index] || null,
          );
      });
    }
  }
  present(event) {
    if (event.type === "message.created") {
      this.message({ ...event.payload, seq: event.seq });
      return;
    }
    if (event.type === "command.applied") return;
    const initial = this.renderers.describe(event);
    if (initial.hidden && !initial.affectsProcess) return;
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
    const group = this.group(event);
    descriptor.seq = event.seq;
    if (event.seq >= (group.stateSeq || 0)) {
      if (event.type === "run.started") group.state = "running";
      else if (event.type === "run.completed") group.state = "completed";
      else if (event.type === "run.failed") group.state = "failed";
      else if (event.type === "run.stopped") group.state = "stopped";
      else if (event.type === "run.recovery.required")
        group.state = "suspended";
      else if (event.type === "run.state.changed")
        group.state = event.payload.state;
      if (
        [
          "run.started",
          "run.completed",
          "run.failed",
          "run.state.changed",
          "run.stopped",
          "run.recovery.required",
        ].includes(event.type)
      )
        group.stateSeq = event.seq;
      if (group.process) group.process.state = group.state;
    }
    if (group.process)
      group.process.end_seq = Math.max(group.process.end_seq, event.seq);
    if (
      group.process &&
      ([
        "run.started",
        "run.completed",
        "run.failed",
        "run.stopped",
        "run.recovery.required",
      ].includes(event.type) ||
        (event.type === "run.state.changed" &&
          event.payload.state !== "running")) &&
      !group.process.milestones.some((item) => item.seq === event.seq)
    )
      group.process.milestones.push(event);
    if (
      ["run.failed", "run.completed", "run.stopped"].includes(event.type) ||
      (event.type === "run.state.changed" &&
        ["completed", "failed", "stopped"].includes(event.payload.state))
    )
      group.node.dataset.endSeq = event.seq;
    if (descriptor.hidden) this.processSummary(group);
    else this.record(group, descriptor);
  }
  record(group, descriptor) {
    let record = group.records.get(descriptor.key);
    if (!record) {
      const node = element("details", "event-row"),
        summary = element("summary"),
        body = element("div", "event-detail"),
        pre = element("pre");
      node.dataset.key = descriptor.key;
      node.open = this.detailMode === true;
      body.append(pre);
      node.append(summary, body);
      group.rows.append(node);
      record = {
        node,
        summary,
        body,
        pre,
        descriptor,
        seq: descriptor.seq || 0,
      };
      group.records.set(descriptor.key, record);
      node.addEventListener("toggle", () => this.detail(record));
      this.detailsChanged();
    }
    const previous = record.descriptor;
    record.seq = Math.min(record.seq, descriptor.seq || record.seq);
    if (descriptor.kind === "tool" || descriptor.kind === "model") {
      descriptor =
        previous.seq > (descriptor.seq || 0)
          ? {
              ...previous,
              details: { ...descriptor.details, ...previous.details },
            }
          : {
              ...descriptor,
              details: { ...previous.details, ...descriptor.details },
            };
      if (descriptor.kind === "model")
        descriptor.summary = modelSummary(
          descriptor.details,
          descriptor.status,
          descriptor.stage,
        );
    }
    if (previous.artifact?.sha256 !== descriptor.artifact?.sha256)
      record.loaded = false;
    record.descriptor = descriptor;
    record.node.className = `event-row ${descriptor.status || ""}`;
    record.summary.textContent = descriptor.summary;
    if (descriptor.status === "failed") group.failures.add(descriptor.key);
    if (!record.loaded)
      record.pre.textContent =
        typeof descriptor.details === "string"
          ? descriptor.details
          : JSON.stringify(descriptor.details, null, 2);
    if (!group.latestSeq || descriptor.seq >= group.latestSeq) {
      group.latestSeq = descriptor.seq;
      group.latest = descriptor.summary;
    }
    this.processSummary(group);
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
      node.open = this.detailMode !== false;
      node.append(element("summary", "", "Result"));
      node.append(renderResult(reportContent(message.content)));
    } else {
      node.append(element("small", "", "Task · You"));
      node.append(element("p", "", message.content));
    }
    node.dataset.seq = message.seq || 0;
    this.messages.set(message.id, node);
    this.layout();
  }
  layout() {
    // Snapshot messages and retained process groups arrive independently.
    // Reconcile them by sequence rather than assuming replay order or run_id
    // on user messages (a new task's message still carries the previous run).
    const timeline = [
      ...this.messages.values(),
      ...[...this.groups.values()].map((group) => group.node),
    ].sort((a, b) => Number(a.dataset.seq) - Number(b.dataset.seq));
    const blocks = [];
    let block;
    for (const node of timeline) {
      const process = node.classList.contains("process-group");
      if (!block || block.finished || (process && block.hasProcess)) {
        const pending =
          process && block?.endSeq
            ? block.nodes.filter(
                (item) =>
                  item.classList.contains("user") &&
                  Number(item.dataset.seq) > block.endSeq,
              )
            : [];
        if (pending.length)
          block.nodes = block.nodes.filter((item) => !pending.includes(item));
        block = {
          key: pending[0]?.dataset.key || node.dataset.key,
          nodes: pending,
          hasProcess: false,
          finished: false,
        };
        blocks.push(block);
      }
      block.nodes.push(node);
      block.hasProcess ||= process;
      if (process) block.endSeq = Number(node.dataset.endSeq) || 0;
      block.finished = node.classList.contains("result");
    }
    const next = new Map();
    for (const block of blocks) {
      const section =
        this.blocks.get(block.key) || element("section", "task-block");
      section.dataset.key = `block:${block.key}`;
      section.setAttribute("aria-label", "Task execution and result");
      // Move only nodes whose position changed, keeping expanded details and
      // focused controls intact during streaming updates.
      const ordered = [
        ...block.nodes.filter((node) => node.classList.contains("user")),
        ...block.nodes.filter((node) => !node.classList.contains("user")),
      ];
      ordered.forEach((node, index) => {
        if (section.children[index] !== node)
          section.insertBefore(node, section.children[index] || null);
      });
      next.set(block.key, section);
    }
    for (const section of this.blocks.values())
      if (![...next.values()].includes(section)) section.remove();
    [...next.values()].forEach((section, index) => {
      if (this.root.children[index] !== section)
        this.root.insertBefore(section, this.root.children[index] || null);
    });
    this.blocks = next;
    this.detailsChanged();
  }
}
