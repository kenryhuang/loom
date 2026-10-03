"""Deterministic display projection with sequence and text-offset deduplication."""

from copy import deepcopy

from loom.service.contracts import ServiceError


class SessionProjection:
    def __init__(self, snapshot):
        self.snapshot = deepcopy(snapshot)
        self.cursor = snapshot["event_cursor"]
        self.streams = {}

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
        elif kind.startswith("input."):
            self.snapshot["input_request"] = deepcopy(data)
        elif kind.startswith("plan."):
            self.snapshot["plan_event"] = deepcopy(data)
        elif kind.startswith("llm.") and kind.endswith(".delta") and isinstance(data.get("delta"), str):
            key = f"{data['llm_call_id']}:{kind.split('.')[1]}"
            old = self.streams.get(key, "")
            offset = data.get("offset", len(old))
            if offset > len(old):
                raise ServiceError("Text gap; reload history", 409)
            self.streams[key] = old + data["delta"][max(0, len(old) - offset) :]
        elif kind == "llm.completed":
            response = data.get("response", {})
            if "content" in response:
                self.streams[f"{data['llm_call_id']}:content"] = response["content"] or ""
        self.cursor = event["seq"]
        self.snapshot["event_cursor"] = self.cursor
        return True
