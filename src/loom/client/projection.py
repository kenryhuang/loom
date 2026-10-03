"""Deterministic display projection with sequence and text-offset deduplication."""

from copy import deepcopy

from loom.service.contracts import ServiceError, stream_key


class SessionProjection:
    def __init__(self, snapshot):
        self.snapshot = deepcopy(snapshot)
        self.cursor = snapshot["event_cursor"]
        self.streams = deepcopy(snapshot.get("streams", {}))
        self.stream_origins = deepcopy(snapshot.get("stream_origins", {}))

    def apply(self, event):
        if event["session_id"] != self.snapshot["session_id"] or event["seq"] <= self.cursor:
            return False
        if event["seq"] != self.cursor + 1:
            raise ServiceError("Event gap; reload snapshot", 409)
        data, kind = event["payload"], event["type"]
        if kind == "message.created":
            self.snapshot["messages"].append({**data, "seq": event["seq"]})
        elif kind == "command.applied":
            cid = event.get("command_id") or data.get("command_id")
            for message in self.snapshot["messages"]:
                if message.get("command_id") == cid:
                    message["state"] = "applied"
        elif kind == "task.state.changed":
            self.snapshot["task"]["state"] = data["state"]
            if "revision" in data:
                self.snapshot["task"]["revision"] = data["revision"]
        elif kind == "task.goal.revised":
            self.snapshot["task"].update(objective=data["objective"], goal_revision=data["goal_revision"])
        elif kind.startswith("input."):
            self.snapshot["input_request"] = deepcopy(data)
        elif kind.startswith("plan."):
            self.snapshot["plan_event"] = deepcopy(data)
            if "plan" in data:
                self.snapshot["plan"] = deepcopy(data["plan"])
        elif kind.startswith("llm.") and kind.endswith(".delta") and isinstance(data.get("delta"), str):
            key = stream_key({**data, "type": kind})
            old = self.streams.get(key, "")
            origin = self.stream_origins.get(key, 0)
            end = origin + len(old)
            offset = data.get("offset", end)
            if offset > end:
                raise ServiceError("Text gap; reload history", 409)
            text = old + data["delta"][max(0, end - offset) :]
            limit = self.snapshot["task"].get("limits", {}).get("max_window_chars", 240000)
            dropped = max(0, len(text) - limit)
            self.streams[key] = text[dropped:]
            self.stream_origins[key] = origin + dropped
        elif kind == "llm.completed":
            response = data.get("response", {})
            if "content" in response:
                self.streams[f"{data['llm_call_id']}:content"] = response["content"] or ""
        self.cursor = event["seq"]
        self.snapshot["event_cursor"] = self.cursor
        return True
