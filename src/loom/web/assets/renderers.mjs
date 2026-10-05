/** Rendering descriptors are data; register new event types without changing the feed. */
export class RendererRegistry {
  constructor() {
    this.entries = [];
  }
  register(id, matches, render, priority = 0) {
    this.entries = this.entries.filter((entry) => entry.id !== id);
    this.entries.push({ id, matches, render, priority });
    this.entries.sort((a, b) => b.priority - a.priority);
    return this;
  }
  describe(event, context = {}) {
    return (
      this.entries
        .find((entry) => entry.matches(event))
        ?.render(event, context) || {
        key: `event:${event.seq}`,
        summary: event.type,
        details: event.payload,
      }
    );
  }
}

export function reportContent(value, depth = 0) {
  if (depth > 12) return value;
  if (typeof value === "string") {
    const fenced = /^\s*```(?:json)?\s*\n([\s\S]*?)\n```\s*$/i.exec(value);
    try {
      return reportContent(JSON.parse(fenced ? fenced[1] : value), depth + 1);
    } catch {
      return value;
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  for (const key of [
    "report",
    "markdown",
    "final_answer",
    "answer",
    "content",
  ]) {
    if (typeof value[key] === "string" && value[key].trim())
      return reportContent(value[key], depth + 1);
  }
  if (value.action && value.action.kind !== "tool" && value.action.input)
    return reportContent(value.action.input, depth + 1);
  for (const key of ["result", "output", "value", "data"]) {
    if (
      value[key] &&
      Object.keys(value).every((name) =>
        [key, "sources", "metadata", "artifact", "usage", "status"].includes(
          name,
        ),
      )
    ) {
      return reportContent(value[key], depth + 1);
    }
  }
  return value;
}

export function firstLine(value) {
  return String(value || "")
    .split(/\n|(?<=[。！？])|(?<=[.!?])\s/)[0]
    .trim();
}
export function modelSummary(details, status, stage) {
  if (status === "failed") {
    const error = details.error || {};
    return `Model failed · ${error.code || "LLM_FAILED"} · ${error.message || "Request failed"}`;
  }
  let content = details.response?.content || details.content || "",
    reasoning;
  try {
    reasoning = JSON.parse(
      content.replace(/^\s*```(?:json)?\s*\n|\n```\s*$/g, ""),
    ).reasoning;
  } catch {}
  const text = reasoning || details.reasoning || content;
  return `${text ? `Thought: ${firstLine(text).slice(0, 200)}` : "Model"} · ${stage}`;
}
export function builtinRenderers() {
  return new RendererRegistry()
    .register(
      "internal-lifecycle",
      (event) =>
        ["operation.started", "operation.completed"].includes(event.type) ||
        event.type.startsWith("llm.tool_call.") ||
        (["task.", "run.", "session.", "budget.", "command.", "step."].some(
          (prefix) => event.type.startsWith(prefix),
        ) &&
          !event.type.endsWith(".failed") &&
          event.type !== "run.recovery.required"),
      (event) => ({
        hidden: true,
        affectsProcess: [
          "run.started",
          "run.state.changed",
          "run.completed",
          "run.stopped",
        ].includes(event.type),
      }),
    )
    .register(
      "model",
      (event) =>
        [
          "llm.requested",
          "llm.stream.started",
          "llm.stream.completed",
          "llm.completed",
          "llm.failed",
          "llm.reasoning.delta",
          "llm.reasoning_context.delta",
          "llm.content.delta",
        ].includes(event.type),
      (event, context) => {
        const data = event.payload,
          details = {};
        let stage = "Streaming",
          status = "running";
        if (event.type === "llm.requested") {
          details.request = data;
          stage = "Waiting";
        } else if (event.type === "llm.completed") {
          details.response = data.response;
          stage = "Done";
          status = "";
        } else if (event.type === "llm.failed") {
          details.error = data.error || data;
          stage = "Failed";
          status = "failed";
        } else if (event.type === "llm.stream.completed")
          stage = "Response received";
        else if (event.type.endsWith(".delta"))
          details[event.type.slice(4, -6)] =
            context.streamText ?? data.delta ?? "";
        return {
          key: `thought:${data.llm_call_id || event.seq}`,
          kind: "model",
          details,
          stage,
          status,
          summary: modelSummary(details, status, stage),
          artifact: data.artifact,
        };
      },
    )
    .register(
      "execution-failure",
      (event) =>
        [
          "run.failed",
          "task.failed",
          "step.failed",
          "operation.uncertain",
        ].includes(event.type),
      (event) => {
        const error = event.payload.error || event.payload;
        return {
          key: `event:${event.seq}`,
          status: "failed",
          summary:
            event.type === "operation.uncertain"
              ? "Recovery required · Operation effects are uncertain"
              : `Failed · ${error.code || event.type} · ${error.message || "Execution failed"}`,
          details: event.payload,
        };
      },
    )
    .register(
      "tool",
      (event) => event.type.startsWith("tool."),
      (event) => {
        const data = event.payload,
          name = data.tool_id || data.tool_name || "tool";
        const output = data.output?.value || data.output || {},
          failed =
            event.type === "tool.failed" ||
            output.ok === false ||
            (output.exit_code != null &&
              output.exit_code !== 0 &&
              output.status !== "no_match"),
          running = event.type === "tool.started";
        const argument =
          data.input?.path || data.input?.url || data.input?.command || "";
        return {
          key: `tool:${data.tool_call_id || event.seq}`,
          kind: "tool",
          summary: `Tool call (${name})${argument ? ` · ${Array.isArray(argument) ? argument.join(" ") : argument}` : ""}${failed ? ` · ${data.error?.message || output.error?.message || `Failed${output.exit_code != null ? ` (exit ${output.exit_code})` : ""}`}` : running ? " · Running" : " · Done"}`,
          status: failed ? "failed" : running ? "running" : "",
          details: data,
          artifact: data.artifact,
        };
      },
    )
    .register(
      "thought",
      (event) => event.type === "decision.recorded",
      (event, context) => {
        const data = event.payload;
        const text =
          context.streamText ?? data.decision?.reasoning ?? data.delta ?? "";
        return {
          key: `thought:${data.llm_call_id || event.seq}`,
          kind: "thought",
          summary: `Thought: ${firstLine(text)}`,
          details: text,
          status: event.type.endsWith(".delta") ? "running" : "",
        };
      },
    )
    .register(
      "model-stream",
      (event) => event.type.startsWith("llm.") && event.type.endsWith(".delta"),
      (event, context) => ({
        key: `stream:${context.streamKey || event.payload.llm_call_id}`,
        summary: `Model output · ${event.type.slice(4, -6)}`,
        details: context.streamText || event.payload.delta,
      }),
    );
}
