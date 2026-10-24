"""Request outcomes and conservative presentation of older evaluation records."""


def finish_request(body, *, elapsed_seconds, state, error, error_code):
    call = body.get("current_call")
    if not call or call.get("state") != "waiting":
        return None
    call.update(state=state, error=error, error_code=error_code,
                duration_seconds=round(max(0, elapsed_seconds - call["started_elapsed_seconds"]), 2))
    body["call_history"] = [*body.get("call_history", []), dict(call)][-500:]
    label = "stopped" if state == "budget_exhausted" else state
    return f"Evaluator request {label}: {call.get('stage')} · [{error_code}] {error}"


def present_legacy_request_failure(body):
    """Use only a matching final request and recorded terminal state, never earlier failures."""
    call = body.get("current_call") or {}
    if call.get("error") or call.get("state") not in {"failed", "interrupted"}:
        return body
    if body.get("state") not in {"cancelled", "interrupted", "failed", "budget_exhausted"}:
        return body
    progress = body.get("progress", [])
    requests = [i for i, row in enumerate(progress) if row.get("kind", "").startswith("call.")]
    if not requests:
        return body
    index = requests[-1]
    if progress[index].get("kind") != "call.failed" or progress[index].get("message") != f"Evaluator request failed: {call.get('stage')}":
        return body
    if not progress or progress[-1].get("kind") != body["state"]:
        return body
    state = body["state"] if body["state"] in {"cancelled", "interrupted"} else "failed"
    reason = {"cancelled": "Evaluation was cancelled while waiting for the evaluator response",
              "interrupted": "Evaluation was interrupted while waiting for the evaluator response"}.get(state)
    reason = reason or body.get("error") or "This service version did not record the request failure reason"
    code = "recorded." + body["state"]
    updated = {**call, "state": state, "error": reason, "error_code": code, "error_source": "evaluation_terminal_state"}
    rows = [dict(row) for row in progress]
    rows[index].update(kind="call." + state, message=f"Evaluator request {state}: {call.get('stage')} · {reason}",
                       error_source="evaluation_terminal_state")
    return {**body, "current_call": updated, "progress": rows}
