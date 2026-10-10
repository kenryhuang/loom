/** Order-independent lifecycle projection, scoped by the owning process. */
export function activityKey(descriptor, event) {
  if (["plan", "acceptance"].includes(descriptor.kind)) return descriptor.key;
  const scope = event?.payload?.trace_id || event?.trace_id;
  return scope ? `${scope}:${descriptor.key}` : descriptor.key;
}
export function mergeActivity(previous, incoming) {
  if (!previous)
    return { ...incoming, sources: incoming.source ? [incoming.source] : [] };
  const newer = (incoming.seq || 0) >= (previous.seq || 0);
  const latest = newer ? incoming : previous;
  const older = newer ? previous : incoming;
  const merge = ["tool", "model", "thought", "plan"].includes(latest.kind);
  const sources = [...(previous.sources || [])];
  if (
    incoming.source &&
    !sources.some((source) => source.seq === incoming.source.seq)
  ) {
    const tail = sources.at(-1);
    if (
      tail &&
      incoming.source.type.endsWith(".delta") &&
      tail.type === incoming.source.type &&
      incoming.source.seq === tail.seq + 1
    ) {
      sources[sources.length - 1] = {
        ...incoming.source,
        first_seq: tail.first_seq || tail.seq,
      };
    } else sources.push(incoming.source);
  }
  sources.sort((a, b) => a.seq - b.seq);
  return {
    ...older,
    ...latest,
    artifact: latest.artifact || older.artifact,
    details:
      merge &&
      typeof older.details === "object" &&
      typeof latest.details === "object"
        ? { ...older.details, ...latest.details }
        : latest.details,
    sources,
  };
}
