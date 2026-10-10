"""Recognize task-bearing messages without treating the user role as provenance.

Loom also transports state snapshots, tool observations and historical messages in
that role. Prefer its explicit current-request envelope, then its structured system
goal; plain messages are a fallback for generic traces or known session inputs.
"""

from collections.abc import Mapping

REQUEST_PREFIX = "Current request for this round:\n"
SYSTEM_GOAL = "\nGoal:\n- Objective: "


def request_goals(messages, path, *, known=(), allow_plain=False):
    messages = messages if isinstance(messages, (list, tuple)) else ()
    candidates = []
    for i, message in enumerate(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        text = message.get("content")
        if not isinstance(text, str):
            continue
        if message.get("name") == "current_request" and text.startswith(REQUEST_PREFIX):
            start = len(REQUEST_PREFIX)
            candidates.append((text[start:], f"{path}.{i}.content", start, len(text), True, "current_request"))
    if candidates:
        return candidates[-1:]
    for i, message in enumerate(messages):
        if not isinstance(message, Mapping) or message.get("role") != "system":
            continue
        text = message.get("content")
        if not isinstance(text, str) or not text.startswith("You are the loop brain for this Loom context.") or SYSTEM_GOAL not in text:
            continue
        start = text.index(SYSTEM_GOAL) + len(SYSTEM_GOAL)
        end = text.find("\nSuccess criteria:", start)
        if end > start:
            return [(text[start:end], f"{path}.{i}.content", start, end, True, "runtime_goal")]
    background = False
    for i, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        if message.get("name") == "session_background":
            background = True
        text = message.get("content")
        if background or message.get("role") != "user" or message.get("name") or not isinstance(text, str):
            continue
        if text.startswith(("Current loop state:", "Call exactly one of enter_plan or continue_react", "Earlier conversation (background;")):
            continue
        if text in known or allow_plain:
            candidates.append((text, f"{path}.{i}.content", 0, len(text), False, "request_user_message"))
    return candidates
