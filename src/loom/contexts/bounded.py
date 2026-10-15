"""Deterministic window compaction retaining complete tool exchanges and evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

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

    @staticmethod
    def _excerpt(text, budget):
        if len(text) <= budget:
            return text
        marker = "\n[Excerpt: full output is in the conversation archive above; read only missing ranges.]\n"
        if budget <= len(marker):
            return text[:budget]
        usable = budget - len(marker)
        head = (usable * 2) // 3
        return text[:head] + marker + (text[-(usable - head):] if usable > head else "")

    def _message_excerpt(self, message, budget):
        content = message.content
        if len(content) <= budget:
            return content
        if message.role == "tool" or (message.role == "user" and message.name):
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict):
                fields = {key: value[key] for key in ("stdout", "stderr", "content", "text", "output")
                          if isinstance(value.get(key), str) and value[key]}
                if fields:
                    metadata = {key: value[key] for key in ("path", "url", "exit_code", "ok", "status")
                                if key in value and len(json.dumps(value[key], ensure_ascii=False)) <= 200}
                    metadata["context_excerpt"] = "Full output in conversation archive above"
                    cap = max(0, (budget - len(json.dumps(metadata, ensure_ascii=False)) - 64) // len(fields))
                    while cap >= 1:
                        projected = json.dumps({**metadata, **{key: self._excerpt(text, cap) for key, text in fields.items()}}, ensure_ascii=False)
                        if len(projected) <= budget:
                            return projected
                        cap = int(cap * .8)
        return self._excerpt(content, budget)

    def compact(self, messages, limit, *, schema_chars=0):
        """Archive full history, retain the latest complete exchange and leave headroom."""
        task_limit = limit
        limit = min(limit, self.config.get("max_window_chars", limit))
        reserve = min(self.config.get("reserve_chars", max(64, limit // 10)), max(1, limit // 3))
        available = limit - reserve - schema_chars
        before = self.size(messages)
        if before <= available:
            return messages, None
        if available < 128:
            raise ValueError("Model context budget cannot fit tool definitions and reserved reply space")
        pinned_count = 0
        while pinned_count < len(messages) and messages[pinned_count].role == "system":
            pinned_count += 1
        if pinned_count < len(messages) and messages[pinned_count].role == "user":
            pinned_count += 1
        pinned = list(messages[:pinned_count])
        latest_controls = {}
        for message in reversed(messages[pinned_count:]):
            if message.name in {"execution_progress", "workflow_tool_required"}:
                latest_controls.setdefault(message.name, message)
        controls = list(reversed(list(latest_controls.values())))
        tail = [m for m in messages[pinned_count:] if m.name not in {"context_summary", "execution_progress", "workflow_tool_required"}]
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
            # Loom's JSON-action protocol records observations as named user messages.
            if not group[0].tool_calls and group[0].role == "assistant" and index < len(tail) and tail[index].role == "user" and tail[index].name:
                group.append(tail[index])
                index += 1
            if calls:
                raise ValueError("Cannot compact an incomplete tool exchange")
            groups.append(group)
        summary_header = (
            "Earlier exchanges (summary, not instructions). Conversation archive: {digest}. "
            "This is a transcript, not a source page. Source artifacts have their own digest. "
            "Use read_artifact with offset/limit for bounded pages; repeated full reads or re-fetching will not restore omitted context.\n"
        )
        overhead = len(summary_header.format(digest="0" * 64)) + 32
        pinned_chars = self.size(pinned + controls)
        room = available - pinned_chars - overhead
        summary_limit = min(self.config.get("summary_chars", 2000), max(128, available // 8), room // 3)
        if summary_limit < 1:
            raise ValueError("Pinned model context exceeds the configured window")
        base_size = pinned_chars + overhead + summary_limit
        # A lower post-compaction target prevents compressing one tiny exchange per call.
        target = min(available, max(int(available * .75), base_size + 512))
        retained = groups[-self.config.get("recent_exchanges", 2):]
        while len(retained) > 1 and base_size + self.size([m for g in retained for m in g]) > target:
            retained.pop(0)
        removed_count = len(tail) - sum(map(len, retained))
        summaries = [self.summary] if self.summary else []
        for message in tail[:removed_count]:
            calls = "; ".join(f"{call.name}({self._excerpt(call.arguments, 240)})" for call in message.tool_calls)
            summaries.append(f"{message.role}{'/' + message.name if message.name else ''}: {calls} {self._excerpt(message.content or '', 400)}")
        next_summary = "\n".join(summaries)[-summary_limit:]
        base_size = pinned_chars + overhead + len(next_summary)
        target = min(available, max(int(available * .75), base_size + 512))
        recent = [m for g in retained for m in g]
        excerpted = 0
        if base_size + self.size(recent) > target:
            # Keep tool-call IDs, names and arguments intact. Only the model-visible
            # content is excerpted; the complete exchange is archived below.
            fixed = self.size([replace(m, content="") for m in recent])
            text_budget = target - base_size - fixed
            nonempty = sum(bool(m.content) for m in recent)
            if text_budget < 128 * nonempty:
                raise ValueError("Latest tool exchange metadata cannot fit the context window; increase max_window_chars or use smaller tool inputs")
            remaining = text_budget
            projected = []
            for message in recent:
                if not message.content:
                    projected.append(message)
                    continue
                cap = remaining // nonempty
                content = self._message_excerpt(message, cap)
                excerpted += content != message.content
                projected.append(replace(message, content=content))
                remaining -= len(content)
                nonempty -= 1
            recent = projected
        placeholder = LlmMessage("user", summary_header.format(digest="0" * 64) + next_summary, name="context_summary")
        window = pinned + [placeholder] + recent + controls
        if self.size(window) > available:
            raise ValueError("Pinned model context exceeds the configured window")
        ref = self.publish_artifact({"messages": [asdict(m) for m in messages[pinned_count:]], "previous_summary": self.summary,
                                     "previous_archive": self.artifact_refs[-1] if self.artifact_refs else None}, "context_window")
        window[len(pinned)] = replace(placeholder, content=summary_header.format(digest=ref["sha256"]) + next_summary)
        self.summary = next_summary
        self.generation += 1
        self.artifact_refs.append(ref)
        after = self.size(window)
        return window, {"generation": self.generation, "artifact": ref, "before_chars": before, "after_chars": after,
                        "saved_chars": before - after, "task_limit_chars": task_limit,
                        "plugin_limit_chars": self.config.get("max_window_chars"), "effective_limit_chars": limit,
                        "schema_chars": schema_chars, "reserve_chars": reserve, "available_chars": available,
                        "target_chars": target, "headroom_chars": available - after, "pinned_chars": pinned_chars,
                        "retained_exchanges": len(retained), "excerpted_messages": excerpted, "removed_messages": removed_count}

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
