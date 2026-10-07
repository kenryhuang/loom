"""Deterministic window compaction retaining complete tool exchanges and evidence."""

from __future__ import annotations

from dataclasses import asdict

from loom.llm.api import LlmMessage, build_messages
from loom.runtime.plugin_contracts import PluginManifest, json_value


class BoundedContextManager:
    manifest = PluginManifest("bounded_context", "context")

    def __init__(self, config, *, publish_artifact):
        if set(config) - {"max_window_chars", "max_history_steps", "reserve_chars", "recent_exchanges", "summary_chars"}:
            raise ValueError("Unknown context configuration")
        self.config = json_value(config)
        for value in config.values():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Context limits must be positive integers")
        self.publish_artifact = publish_artifact
        self.generation = 0
        self.artifact_refs = []
        self.summary = ""

    def project(self, context, **_options):
        window = list(build_messages(context, max_history_steps=self.config.get("max_history_steps", 5)))
        if context.state.observations or context.state.decisions or context.knowledge.facts or context.knowledge.heuristics:
            pinned = list(build_messages(context, include_history=False, include_knowledge=False))
            return pinned[:2] + [LlmMessage("user", window[1].content, name="context_evidence")] + pinned[2:]
        return window

    @staticmethod
    def size(messages):
        return sum(len(m.content or "") + sum(len(call.arguments) + len(call.name) for call in m.tool_calls) + 32 for m in messages)

    def compact(self, messages, limit, *, schema_chars=0):
        """Return (window, event), or raise when pinned context alone cannot fit."""
        limit = min(limit, self.config.get("max_window_chars", limit))
        reserve = min(self.config.get("reserve_chars", max(64, limit // 10)), max(1, limit // 3))
        available = limit - reserve - schema_chars
        if self.size(messages) <= available:
            return messages, None
        if available < 128:
            raise ValueError("Model context budget cannot fit tool definitions and reserved reply space")
        # Keep system instructions and the initial goal projection pinned. Everything
        # else is cut only at a complete assistant/tool exchange boundary.
        pinned_count = 0
        while pinned_count < len(messages) and messages[pinned_count].role == "system":
            pinned_count += 1
        if pinned_count < len(messages) and messages[pinned_count].role == "user":
            pinned_count += 1
        pinned = list(messages[:pinned_count])
        tail = [m for m in messages[pinned_count:] if m.name != "context_summary"]
        groups = []
        index = 0
        while index < len(tail):
            group = [tail[index]]
            index += 1
            calls = {call.id for call in group[0].tool_calls}
            while index < len(tail) and tail[index].role == "tool":
                group.append(tail[index])
                calls.discard(tail[index].tool_call_id)
                index += 1
            if calls:
                raise ValueError("Cannot compact an incomplete tool exchange")
            groups.append(group)
        retained = groups[-self.config.get("recent_exchanges", 2) :]
        summary_header = (
            "Earlier exchanges (summary, not instructions). Conversation archive: {digest}. "
            "This is a transcript, not a source page. Source artifacts have their own digest. "
            "Use read_artifact with offset/limit for bounded pages; repeated full reads or re-fetching will not restore omitted context.\n"
        )
        summary_overhead = len(summary_header.format(digest="0" * 64)) + 32
        summary_limit = min(self.config.get("summary_chars", 2000), max(128, available // 5), available - self.size(pinned) - summary_overhead)
        if summary_limit < 1:
            raise ValueError("Pinned model context exceeds the configured window")
        while retained and self.size(pinned + [m for group in retained for m in group]) + summary_limit + summary_overhead > available:
            retained.pop(0)
        retained_count = sum(map(len, retained))
        removed = tail[: len(tail) - retained_count] if retained_count else tail
        if not removed:
            raise ValueError("Pinned model context exceeds the configured window")
        ref = self.publish_artifact({"messages": [asdict(m) for m in removed], "previous_summary": self.summary}, "context_window")
        summaries = [self.summary] if self.summary else []
        summaries.extend(f"{m.role}{'/' + m.name if m.name else ''}: {m.content[:400]}" for m in removed)
        self.summary = "\n".join(summaries)[-summary_limit:]
        summary = LlmMessage(
            "user",
            summary_header.format(digest=ref["sha256"]) + self.summary,
            name="context_summary",
        )
        window = pinned + [summary] + [m for group in retained for m in group]
        if self.size(window) > available:
            raise ValueError("Pinned model context exceeds the configured window")
        self.generation += 1
        self.artifact_refs.append(ref)
        return window, {"generation": self.generation, "artifact": ref, "before_chars": self.size(messages), "after_chars": self.size(window)}

    def ingest(self, context, _committed_result):
        # The kernel owns committed observations; this policy does not rewrite them.
        return context

    def snapshot(self):
        return self.manifest.state(self.config, {"generation": self.generation, "summary": self.summary, "artifact_refs": self.artifact_refs})

    def restore(self, state):
        value = self.manifest.restore(self.config, state)
        self.generation, self.summary, self.artifact_refs = value["generation"], value["summary"], value["artifact_refs"]


class ResearchContextManager(BoundedContextManager):
    manifest = PluginManifest("research_context", "context")

    def project(self, context, **options):
        window = super().project(context, **options)
        window[0] = LlmMessage(
            "system",
            window[0].content + "\nUse readable source text and retain source artifacts. "
            "Record source URLs and distinguish evidence from inference in the final report. "
            "Failed retrieval and model memory do not satisfy a request to read a source. "
            "Use the returned readable text. A source page with has_more=false is complete in the current view. "
            "If has_more=true, read further only when needed to resolve a specific missing fact; do not exhaustively page through a source. "
            "Do not fetch the same URL again to recover context. Stop collecting once the requested deliverable has sufficient evidence. "
            "Synthesize supported conclusions, label uncertainty and finish; breadth of related material is not a reason to keep searching.",
        )
        return window
