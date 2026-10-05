/** Session lifecycle orchestration. Views subscribe rather than own network state. */
import { ApiError, delay } from "./api.mjs";
import { EventGap, SessionProjection } from "./state.mjs";

export class SessionController {
  constructor(api) {
    this.api = api;
    this.listeners = new Set();
    this.sessions = [];
    this.catalog = null;
    this.selectedId = null;
    this.projection = null;
    this.generation = 0;
    this.abort = null;
    this.nextBefore = null;
  }
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  emit(type, detail) {
    for (const listener of this.listeners) listener({ type, detail });
  }
  async connect() {
    [this.catalog, this.sessions] = await Promise.all([
      this.api.catalog(),
      this.api.sessions(),
    ]);
    this.emit("catalog", this.catalog);
    this.emit("sessions", this.sessions);
  }
  async refresh() {
    this.sessions = await this.api.sessions();
    this.emit("sessions", this.sessions);
  }
  async select(id) {
    this.abort?.abort();
    this.abort = new AbortController();
    const generation = ++this.generation,
      signal = this.abort.signal;
    this.selectedId = id;
    this.projection = null;
    this.emit("connection", "Loading");
    try {
      await this.load(id, generation, signal);
      if (!this.current(id, generation)) return;
      this.live(id, generation, signal).catch((error) => {
        if (error.name !== "AbortError") this.emit("error", error);
      });
    } catch (error) {
      if (error.name !== "AbortError" && this.current(id, generation))
        throw error;
    }
  }
  current(id, generation) {
    return this.selectedId === id && this.generation === generation;
  }
  async load(id, generation, signal) {
    const [snapshot, history, index] = await Promise.all([
      this.api.snapshot(id, signal),
      this.api.history(id, { signal }),
      this.api.processes ? this.api.processes(id, signal) : { processes: [] },
    ]);
    if (!this.current(id, generation)) return;
    this.projection = new SessionProjection(snapshot);
    this.nextBefore = history.next_before;
    this.emit("restore", {
      snapshot,
      processes: index.processes
        .filter((process) => process.start_seq <= snapshot.event_cursor)
        .map((process) => ({
          ...process,
          end_seq: Math.min(process.end_seq, snapshot.event_cursor),
          milestones: process.milestones.filter(
            (event) => event.seq <= snapshot.event_cursor,
          ),
        })),
      events: history.events.filter(
        (event) => event.seq <= snapshot.event_cursor,
      ),
    });
    this.emit("state", snapshot);
    this.updateList(snapshot);
  }
  updateList(state) {
    const summary = {
      session_id: state.session_id,
      title: state.title,
      task: state.task,
      event_cursor: state.event_cursor,
    };
    const index = this.sessions.findIndex(
      (item) => item.session_id === state.session_id,
    );
    if (index < 0) this.sessions.unshift(summary);
    else this.sessions[index] = summary;
    this.emit("sessions", this.sessions);
  }
  async live(id, generation, signal) {
    let backoff = 400,
      reload = false;
    while (!signal.aborted && this.current(id, generation)) {
      try {
        if (reload) {
          await this.load(id, generation, signal);
          reload = false;
        }
        if (signal.aborted || !this.current(id, generation)) return;
        this.emit("connection", "Connected");
        for await (const event of this.api.events(
          id,
          this.projection.cursor,
          signal,
        )) {
          if (!this.current(id, generation) || signal.aborted) return;
          if (this.projection.apply(event)) {
            this.emit("event", event);
            this.emit("state", this.projection.snapshot);
            if (
              ["task.state.changed", "task.goal.revised"].includes(event.type)
            )
              this.updateList(this.projection.snapshot);
          }
          backoff = 400;
        }
      } catch (error) {
        if (signal.aborted || !this.current(id, generation)) return;
        if (error instanceof ApiError && error.status === 401) {
          this.emit("unauthorized", error);
          this.disconnect();
          return;
        }
        if (error instanceof EventGap || error.status === 409) reload = true;
        this.emit("notice", `Reconnecting: ${error.message}`);
      }
      this.emit("connection", "Reconnecting");
      await delay(backoff, signal);
      backoff = Math.min(backoff * 2, 5000);
    }
  }
  async older() {
    if (!this.selectedId || !this.nextBefore) return;
    const id = this.selectedId,
      generation = this.generation;
    const page = await this.api.history(id, {
      before: this.nextBefore,
      signal: this.abort.signal,
    });
    if (!this.current(id, generation)) return;
    this.nextBefore = page.next_before;
    this.emit("history", page.events);
  }
  async command(kind, payload = {}) {
    const id = this.selectedId,
      generation = this.generation;
    if (!id) throw new Error("Choose a session first");
    const receipt = await this.api.command(id, kind, payload);
    if (this.current(id, generation))
      this.emit("notice", "Accepted; applies at the next execution boundary.");
    return receipt;
  }
  async send(content, redirect = false) {
    if (!content.trim()) throw new Error("Enter a message");
    const request = this.projection?.snapshot.input_request;
    if (request?.state === "pending") {
      if (redirect && request.kind === "recovery")
        throw new Error("Answer the recovery question before redirecting");
      return this.command(redirect ? "supersede_input" : "answer_input", {
        request_id: request.id,
        [redirect ? "content" : "answer"]: content,
      });
    }
    return this.command("submit_message", { content });
  }
  disconnect() {
    this.abort?.abort();
    ++this.generation;
    this.selectedId = null;
    this.projection = null;
    this.emit("connection", "Disconnected");
  }
}
