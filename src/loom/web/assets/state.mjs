/** Protocol projection, independent of transport and DOM. */
export class EventGap extends Error {}
export const codePoints = (value) => Array.from(value || "");
export function streamKey(event) {
  const data = event.payload;
  const channel = event.type.slice(4, -6);
  return `${data.llm_call_id}:${channel}${data.tool_call_id ? `:${data.tool_call_id}` : ""}`;
}

export class SessionProjection {
  constructor(snapshot) {
    this.snapshot = structuredClone(snapshot);
    this.cursor = snapshot.event_cursor;
    this.streams = structuredClone(snapshot.streams || {});
    this.origins = structuredClone(snapshot.stream_origins || {});
  }
  budget(used = this.snapshot.token_budget?.used || 0) {
    const limit = this.snapshot.task.limits?.max_tokens || 10000000;
    this.snapshot.token_budget = {
      used,
      limit,
      remaining: Math.max(0, limit - used),
    };
  }
  apply(event) {
    if (
      event.session_id !== this.snapshot.session_id ||
      event.seq <= this.cursor
    )
      return false;
    if (event.seq !== this.cursor + 1)
      throw new EventGap("Event gap; reload snapshot");
    const data = event.payload,
      kind = event.type,
      state = this.snapshot;
    if (data.acceptance) {
      state.acceptance = structuredClone(data.acceptance);
      state.acceptance_run_id = event.run_id;
    }
    if (kind === "workspace.probed") state.workspace_profile = structuredClone(data.profile);
    if (kind === "verification.started") state.acceptance_current_check = data.description;
    if (["verification.completed", "acceptance.gate.passed", "acceptance.gate.blocked"].includes(kind)) state.acceptance_current_check = null;
    if (kind === "message.created")
      state.messages = [
        ...(state.messages || []),
        { ...data, seq: event.seq },
      ].slice(-200);
    else if (kind === "command.applied") {
      const id = event.command_id || data.command_id;
      for (const message of state.messages || [])
        if (message.command_id === id) message.state = "applied";
    } else if (kind === "task.state.changed") {
      state.task.state = data.state;
      if (data.revision != null) state.task.revision = data.revision;
    } else if (kind === "task.goal.revised") {
      Object.assign(state.task, {
        objective: data.objective,
        goal_revision: data.goal_revision,
      });
      if (data.title) state.title = state.task.title = data.title;
    } else if (kind === "task.knowledge.changed") {
      Object.assign(state.task, structuredClone(data));
    } else if (kind === "task.budget.changed") {
      state.task.limits.max_tokens = data.max_tokens;
      this.budget();
    } else if (kind === "task.outputs.changed")
      state.output_artifacts = structuredClone(data.artifacts);
    else if (kind === "run.usage.changed") this.budget(data.total_tokens);
    else if (kind === "run.wrapping_up")
      state.run = { ...state.run, reason: `Wrapping up: ${data.reason}` };
    else if (kind === "run.started") {
      if (state.run?.id !== event.run_id) {
        this.budget(0);
        this.streams = {};
        this.origins = {};
      }
      state.run = {
        ...(state.run?.id === event.run_id ? state.run : {}),
        id: event.run_id,
        state: "running",
      };
      delete state.run.failure;
      delete state.run.reason;
    } else if (kind === "run.time_budget.renewed") {
      state.run = {
        ...state.run,
        active_seconds: data.active_seconds,
        time_budget_start_seconds: data.active_seconds,
      };
      if (data.max_duration_seconds != null)
        state.task.limits.max_duration_seconds = data.max_duration_seconds;
    } else if (kind === "run.completed" || kind === "run.stopped") {
      state.run = {
        ...state.run,
        state: kind === "run.completed" ? "completed" : "stopped",
        reason: data.reason,
      };
    } else if (kind === "run.state.changed")
      state.run = { ...state.run, state: data.state, reason: data.reason };
    else if (kind === "run.failed") {
      state.run = { ...state.run, failure: structuredClone(data) };
      state.task.state = "failed";
    } else if (kind === "run.recovery.required")
      state.run = { ...state.run, state: "suspended", reason: data.reason };
    else if (kind.startsWith("input."))
      state.input_request = structuredClone(data);
    else if (kind.startsWith("plan.") || kind.startsWith("workflow.")) {
      state.plan_event = structuredClone(data);
      if (data.plan) state.plan = structuredClone(data.plan);
      if (data.workflow) state.workflow = structuredClone(data.workflow);
    } else if (
      kind.startsWith("llm.") &&
      kind.endsWith(".delta") &&
      typeof data.delta === "string"
    ) {
      const key = streamKey(event),
        old = codePoints(this.streams[key]),
        origin = this.origins[key] || 0;
      const end = origin + old.length,
        offset = data.offset ?? end;
      if (offset > end) throw new EventGap("Text offset gap; reload snapshot");
      const next = [
        ...old,
        ...codePoints(data.delta).slice(Math.max(0, end - offset)),
      ];
      const dropped = Math.max(
        0,
        next.length - (state.task.limits?.max_window_chars || 240000),
      );
      this.streams[key] = next.slice(dropped).join("");
      this.origins[key] = origin + dropped;
    } else if (kind === "llm.completed" && data.response?.content != null) {
      this.streams[`${data.llm_call_id}:content`] = data.response.content;
      this.origins[`${data.llm_call_id}:content`] = 0;
    }
    state.streams = this.streams;
    state.stream_origins = this.origins;
    this.cursor = state.event_cursor = event.seq;
    return true;
  }
}

export function parseBudget(value) {
  const match = /^(\d+(?:\.\d+)?)\s*([km]?)$/i.exec(value.trim());
  if (!match)
    throw new Error("Enter a positive token count, such as 500000 or 10M");
  const count =
    Number(match[1]) * ({ k: 1000, m: 1000000 }[match[2].toLowerCase()] || 1);
  if (!Number.isSafeInteger(count) || count <= 0)
    throw new Error("Token budget must be a positive whole number");
  return count;
}
