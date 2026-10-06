import { builtinRenderers, reportContent } from "../renderers.mjs";
import { codePoints, streamKey } from "../state.mjs";
import { element, renderResult } from "../markdown.mjs";
import { activityKey, mergeActivity } from "../activity/projection.mjs";
import { builtinPresenters } from "../activity/presenters.mjs";
import { ActivityRow } from "./activity-row.mjs";
import { ActivityInspector } from "./activity-inspector.mjs";

export class FeedView {
  constructor(
    root,
    {
      renderers = builtinRenderers(),
      loadArtifact,
      loadProcess,
      onTrajectory,
      presenters = builtinPresenters(),
      maxEvents = 1000,
      onDetailsChange = () => {},
    } = {},
  ) {
    this.root = root;
    this.renderers = renderers;
    this.loadArtifact = loadArtifact;
    this.loadProcess = loadProcess;
    this.presenters = presenters;
    this.inspector = new ActivityInspector({
      load: (record) => this.detail(record, true),
      trajectory: onTrajectory,
      loadArtifact,
    });
    this.maxEvents = maxEvents;
    this.onDetailsChange = onDetailsChange;
    this.events = [];
    this.emptyState = root.querySelector(".empty-state");
    root.addEventListener("toggle", () => this.detailsChanged(), true);
    document.addEventListener("selectionchange", () => {
      if (document.getSelection()?.isCollapsed) {
        for (const group of this.groups.values())
          for (const record of group.records.values())
            if (record.node.open) record.view.renderDetails();
      }
    });
    this.reset();
  }
  reset({ preserveDetails = false } = {}) {
    if (!preserveDetails) {
      this.inspector.close();
      this.detailMode = null;
      this.manualProcesses = new Map();
      this.processes = [];
      this.processPages = new Map();
      this.latestSummaries = new Map();
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
    const scrollTop = this.root.scrollTop;
    const sameSession = this.snapshot?.session_id === snapshot.session_id;
    const follow =
      this.root.scrollHeight - scrollTop - this.root.clientHeight < 70;
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
    this.root.scrollTop =
      sameSession && !follow ? scrollTop : this.root.scrollHeight;
  }
  detailStates() {
    this.scrollPositions = new Map(
      [...this.groups.values()].map((group) => [
        group.node.dataset.key,
        group.rows.scrollTop,
      ]),
    );
    return new Map(
      [...this.root.querySelectorAll("details")].map((node) => [
        `${node.closest(".process-group")?.dataset.key || ""}/${node.dataset.key}`,
        node.open,
      ]),
    );
  }
  reopen(keys) {
    for (const node of this.root.querySelectorAll("details"))
      if (
        keys.has(
          `${node.closest(".process-group")?.dataset.key || ""}/${node.dataset.key}`,
        )
      )
        node.open = keys.get(
          `${node.closest(".process-group")?.dataset.key || ""}/${node.dataset.key}`,
        );
    for (const group of this.groups.values()) {
      for (const record of group.records.values())
        if (record.node.open) record.view.renderDetails();
      if (this.scrollPositions?.has(group.node.dataset.key))
        group.rows.scrollTop = this.scrollPositions.get(group.node.dataset.key);
    }
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
    this.manualProcesses.clear();
    for (const node of details) node.open = this.detailMode;
    for (const group of this.groups.values())
      for (const record of group.records.values()) {
        if (record.node.open) record.view.renderDetails();
        this.detail(record);
      }
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
      node.open =
        this.manualProcesses.get(key) ??
        this.detailMode ??
        process?.state === "running";
      summary.addEventListener("click", () =>
        this.manualProcesses.set(key, !node.open),
      );
      const rows = element("div", "process-records");
      const latest = this.latestSummaries.get(`group:${key}`);
      const progress = element(
        "button",
        "quiet process-new-progress",
        "New progress · Back to latest",
      );
      progress.type = "button";
      progress.hidden = true;
      progress.addEventListener("click", () => {
        rows.scrollTop = rows.scrollHeight;
        progress.hidden = true;
      });
      rows.addEventListener("scroll", () => {
        if (rows.scrollHeight - rows.scrollTop - rows.clientHeight < 40)
          progress.hidden = true;
      });
      node.append(summary, rows, progress);
      this.groups.set(key, {
        node,
        key,
        summary,
        rows,
        process,
        latest: latest?.text,
        latestSeq: latest?.seq,
        records: new Map(),
        progress,
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
    const terminal = ["completed", "failed", "stopped"].includes(group.state);
    const label =
      group.state === "running"
        ? "Processing"
        : group.state === "failed"
          ? "Execution failed"
          : group.state === "stopped"
            ? "Stopped"
            : ["suspended", "paused"].includes(group.state)
              ? "Paused"
              : "Process";
    const count = group.records.size;
    const completeHistory =
      group.process &&
      this.processPages.get(group.process.id)?.nextBefore === null;
    const elapsed =
      group.endedAt && group.startedAt
        ? Math.max(0, Math.round((group.endedAt - group.startedAt) / 1000))
        : null;
    const duration =
      elapsed == null
        ? ""
        : ` · ${Math.floor(elapsed / 60)}m ${elapsed % 60}s elapsed`;
    const suffix = terminal
      ? `${count} ${completeHistory ? "actions" : "loaded actions"}${duration}`
      : group.latest;
    group.summary.textContent = `${label}${suffix ? ` · ${suffix}` : ""}`;
    const manual = this.manualProcesses.get(group.process?.id || group.key);
    if (manual !== undefined) group.node.open = manual;
    else if (this.detailMode !== null) group.node.open = this.detailMode;
    else if (group.state) group.node.open = !terminal;
  }
  orderRecords(group) {
    for (const current of group ? [group] : this.groups.values()) {
      const all = [...current.records.values()].sort((a, b) => a.seq - b.seq);
      const plan = all.find((record) => record.descriptor.kind === "plan");
      if (plan) plan.related = [];
      const records = all.filter((record) => {
        // Successful plan tools are represented by the current checklist.
        // Keep failed, unknown and currently inspected calls in the timeline.
        const absorbed =
          plan &&
          record.descriptor.kind === "tool" &&
          record.view.activity.kind === "plan" &&
          record.view.activity.state === "completed" &&
          !record.view.activity.pending &&
          !record.node.open;
        record.node.hidden = !!absorbed;
        if (absorbed) {
          plan.related.push(record);
          record.node.remove();
        }
        return !absorbed;
      });
      const buckets = [];
      for (const record of records) {
        const activity = record.view.activity;
        const batchable =
          activity?.state === "completed" &&
          ["read", "search"].includes(activity.kind);
        const tail = buckets.at(-1);
        if (batchable && tail?.kind === activity.kind)
          tail.records.push(record);
        else
          buckets.push({
            kind: batchable ? activity.kind : null,
            records: [record],
          });
      }
      current.batches ||= new Map();
      const retained = new Set();
      const nodes = buckets.map((bucket) => {
        if (bucket.records.length === 1) return bucket.records[0].node;
        const key = `batch:${bucket.records[0].node.dataset.key}`;
        let batch = current.batches.get(key);
        if (!batch) {
          const node = element("details", "activity-batch");
          node.dataset.key = key;
          const summary = element("summary");
          const content = element("div", "activity-batch-content");
          node.append(summary, content);
          node.open = this.detailMode === true;
          batch = { node, summary, content };
          current.batches.set(key, batch);
        }
        retained.add(key);
        batch.summary.textContent = `${bucket.kind === "read" ? "Read files" : "Search"} · ${bucket.records.length} calls`;
        bucket.records.forEach((record, index) => {
          if (batch.content.children[index] !== record.node)
            batch.content.insertBefore(
              record.node,
              batch.content.children[index] || null,
            );
        });
        return batch.node;
      });
      nodes.forEach((node, index) => {
        if (current.rows.children[index] !== node)
          current.rows.insertBefore(node, current.rows.children[index] || null);
      });
      for (const [key, batch] of current.batches)
        if (!retained.has(key)) {
          batch.node.remove();
          current.batches.delete(key);
        }
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
    const at = Date.parse(event.at || event.payload.at || "");
    if (event.type === "run.started" && Number.isFinite(at))
      group.startedAt = Math.min(group.startedAt || at, at);
    descriptor.seq = event.seq;
    descriptor.source = {
      seq: event.seq,
      type: event.type,
      run_id: event.run_id,
      timestamp:
        event.at || event.timestamp || event.created_at || event.payload.at,
      artifact: event.payload.artifact,
    };
    descriptor.key = activityKey(descriptor, event);
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
      if (
        Number.isFinite(at) &&
        [
          "run.completed",
          "run.failed",
          "run.stopped",
          "run.state.changed",
        ].includes(event.type) &&
        ["completed", "failed", "stopped"].includes(group.state)
      )
        group.endedAt = at;
      else if (group.state === "running") group.endedAt = null;
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
    else {
      descriptor.eventType = event.type;
      this.record(group, descriptor);
    }
  }
  record(group, descriptor) {
    let record = group.records.get(descriptor.key);
    const follow =
      group.rows.scrollHeight - group.rows.scrollTop - group.rows.clientHeight <
      50;
    if (!record) {
      const view = new ActivityRow(descriptor.key, {
        inspect: (trigger) => this.inspector.open(record, trigger),
        expand: () => this.detail(record),
      });
      view.node.open = this.detailMode === true;
      view.summary.addEventListener("click", () => {
        if (!view.node.open) this.manualProcesses.set(group.key, true);
      });
      record = {
        node: view.node,
        summary: view.summary,
        body: view.body,
        view,
        group,
        seq: descriptor.seq || 0,
      };
      group.records.set(descriptor.key, record);
      group.rows.append(view.node);
      this.detailsChanged();
    }
    record.seq = Math.min(record.seq, descriptor.seq || record.seq);
    const previousDigest = record.descriptor?.artifact?.sha256;
    record.descriptor = mergeActivity(record.descriptor, descriptor);
    if (previousDigest !== record.descriptor.artifact?.sha256) {
      record.loaded = false;
      record.descriptor.hydrated = false;
    }
    this.renderActivity(record);
    this.orderRecords(group);
    this.detail(record);
    if (follow) group.rows.scrollTop = group.rows.scrollHeight;
    else group.progress.hidden = false;
  }
  renderActivity(record) {
    const { descriptor, group } = record;
    const activity = this.presenters.present(descriptor, {
      workspace: this.snapshot?.task.workspace,
    });
    record.view.update(activity);
    if (!group.latestSeq || descriptor.seq >= group.latestSeq) {
      group.latestSeq = descriptor.seq;
      group.latest = [activity.title, activity.subject, activity.outcome]
        .filter(Boolean)
        .join(" · ");
      this.latestSummaries.set(group.node.dataset.key, {
        seq: descriptor.seq,
        text: group.latest,
      });
    }
    this.processSummary(group);
  }
  async detail(record, force = false) {
    const digest = record.descriptor.artifact?.sha256;
    if (
      (!force && !record.node.open) ||
      !digest ||
      record.loaded ||
      !this.loadArtifact
    )
      return;
    if (record.loading) {
      await record.loading;
      if (record.descriptor.artifact?.sha256 !== digest)
        return this.detail(record, force);
      return;
    }
    record.loading = (async () => {
      try {
        const detail = await this.loadArtifact(digest);
        if (record.descriptor.artifact?.sha256 === digest) {
          record.descriptor = {
            ...record.descriptor,
            details: { ...record.descriptor.details, ...detail },
            hydrated: true,
          };
          record.loaded = true;
          this.renderActivity(record);
        }
      } catch (error) {
        record.view.content.append(
          element(
            "p",
            "activity-hint",
            `Could not load full detail: ${error.message}. Reopen to retry.`,
          ),
        );
        if (force) throw error;
      }
    })();
    try {
      await record.loading;
    } finally {
      record.loading = null;
    }
    if (record.descriptor.artifact?.sha256 !== digest)
      return this.detail(record, force);
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
      const existing = [...group.records.entries()].find(
        ([key]) => key === `thought:${id}` || key.endsWith(`:thought:${id}`),
      );
      if (existing?.[1].descriptor.status !== "running" && existing) continue;
      const descriptor = this.renderers.describe(event, {
        streamText: text,
        streamKey: key,
      });
      if (existing) descriptor.key = existing[0];
      this.record(group, descriptor);
    }
  }
  syncMessages(messages) {
    for (const message of messages) this.message(message);
  }
  message(message) {
    if (this.messages.has(message.id)) return;
    this.emptyState?.remove();
    const result = message.role === "assistant",
      node = element("article", `message-card ${result ? "result" : "user"}`);
    node.dataset.key = `message:${message.id}`;
    if (result) {
      node.append(element("small", "", "Loom"));
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
