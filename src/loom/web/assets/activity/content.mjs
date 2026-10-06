/** Bounded protocol decoding; ordinary text and user JSON remain intact. */
export function decode(value) {
  for (
    let depth = 0;
    depth < 5 && typeof value === "string" && value.length < 240000;
    depth++
  ) {
    const text = value.replace(/^\s*```(?:json)?\s*\n|\n```\s*$/g, "");
    try {
      value = JSON.parse(text);
    } catch {
      break;
    }
  }
  return value;
}
export function outputValue(value) {
  value = decode(value);
  for (
    let depth = 0;
    depth < 5 && value && typeof value === "object";
    depth++
  ) {
    if (
      "value" in value &&
      ("ok" in value || "source" in value || Object.keys(value).length === 1)
    )
      value = decode(value.value);
    else break;
  }
  return value;
}
export const text = (value) =>
  typeof value === "string"
    ? value
    : typeof value === "number"
      ? String(value)
      : "";
export const compact = (value, limit = 160) =>
  text(value).replace(/\s+/g, " ").trim().slice(0, limit);
export function modelText(data) {
  const content = data.response?.content ?? data.content ?? "";
  const parsed = decode(content);
  if (parsed && typeof parsed === "object") {
    // Action envelopes describe intent, never proof that a tool executed.
    if (parsed.action || parsed.reasoning)
      return text(parsed.reasoning || parsed.explanation);
    return "";
  }
  const prose = data.response?.reasoning || data.reasoning || content;
  // A partial JSON/fenced protocol must not flicker into the conversation.
  return /^\s*(?:[\[{]|```(?:json)?(?:\s|$))/.test(prose) ? "" : text(prose);
}

/** Prefer authored summaries/intent; never mistake a prose excerpt for a summary. */
export function modelHeadline(data, running = false) {
  const parsed = decode(data.response?.content ?? data.content);
  const decision =
    parsed && typeof parsed === "object" ? parsed : data.decision;
  const summary =
    decision?.summary ||
    data.response?.summary ||
    (!data.artifact ? data.summary : "");
  const action = decision?.action;
  const description = text(summary) || text(action?.description);
  if (description.trim()) return description.replace(/\s+/g, " ").trim();
  const target = action?.target || data.response?.tool_calls?.[0]?.name;
  if (target) return `Preparing ${target}`;
  if (action?.kind === "none" || action?.kind === "finish")
    return "Preparing the answer";
  return running ? "Considering the next step" : "Response received";
}
