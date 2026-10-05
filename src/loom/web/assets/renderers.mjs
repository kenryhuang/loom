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
export function builtinRenderers() {
  return new RendererRegistry()
    .register(
      "tool",
      (event) => event.type.startsWith("tool."),
      (event) => {
        const data = event.payload,
          name = data.tool_id || data.tool_name || "tool";
        const failed = event.type === "tool.failed",
          running = event.type === "tool.started";
        const argument =
          data.input?.path || data.input?.url || data.input?.command || "";
        return {
          key: `tool:${data.tool_call_id || event.seq}`,
          kind: "tool",
          summary: `Tool call (${name})${argument ? ` · ${Array.isArray(argument) ? argument.join(" ") : argument}` : ""}${failed ? ` · ${data.error?.message || "Failed"}` : running ? " · Running" : " · Done"}`,
          status: failed ? "failed" : running ? "running" : "",
          details: data,
          artifact: data.artifact,
        };
      },
    )
    .register(
      "thought",
      (event) =>
        event.type === "llm.reasoning.delta" ||
        event.type === "decision.recorded",
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
