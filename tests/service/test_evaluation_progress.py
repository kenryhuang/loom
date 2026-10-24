import asyncio
import json
from copy import deepcopy

import pytest

from loom.core import make_loom_error
from loom.evaluation.issues import request_failure_event
from loom.service.evaluation_progress import finish_request, present_legacy_request_failure


@pytest.mark.parametrize("error,code,reason", [
    (asyncio.CancelledError(), "request.cancelled", "cancelled"),
    (TimeoutError(), "request.timeout", "timed out"),
    (ValueError(), "ValueError", "ValueError"),
    (make_loom_error("LLM_FAILED", "HTTP 400 invalid_parameter", retryable=False), "LLM_FAILED", "HTTP 400"),
    ({"code": "HTTP_429", "message": "Rate limit", "cause": {"secret": "omit"}}, "HTTP_429", "Rate limit"),
    (None, "request.failed", "no error message was recorded"),
])
def test_request_failure_event_preserves_safe_reason_and_code(error, code, reason):
    event = request_failure_event({"analysis_stage": "batch:one"}, error)
    assert event["error_code"] == code
    assert reason in event["error"]
    assert "secret" not in json.dumps(event)


def test_failed_request_is_retained_once_with_duration_and_reason():
    body = {"current_call": {"stage": "batch:one", "state": "waiting", "started_elapsed_seconds": 623.58}}
    kwargs = dict(elapsed_seconds=997.68, state="cancelled", error="Cancellation requested", error_code="runtime.cancelled")
    assert "Cancellation requested" in finish_request(body, **kwargs)
    assert body["current_call"]["duration_seconds"] == 374.1
    assert body["call_history"] == [body["current_call"]]
    assert finish_request(body, **kwargs) is None
    assert len(body["call_history"]) == 1


def test_old_cancelled_record_explains_only_final_failed_request_without_mutating_evidence():
    body = {"state": "cancelled", "error": "Evaluation stopped; completed batches retained",
            "current_call": {"state": "failed", "stage": "batch:two", "duration_seconds": 374.1},
            "progress": [{"kind": "call.failed", "message": "Evaluator request failed: batch:one"},
                         {"kind": "call.started", "message": "batch:two (call 5)"},
                         {"kind": "call.failed", "message": "Evaluator request failed: batch:two"},
                         {"kind": "cancelled", "message": "Evaluation stopped; completed batches retained"}]}
    original = deepcopy(body)
    public = present_legacy_request_failure(body)
    assert body == original
    assert public["progress"][0] == body["progress"][0]
    assert public["progress"][2]["kind"] == "call.cancelled"
    assert "cancelled while waiting" in public["progress"][2]["message"]
    assert public["current_call"]["error_source"] == "evaluation_terminal_state"
    assert public["current_call"]["state"] == "cancelled"
    body["current_call"]["stage"] = "batch:other"
    assert present_legacy_request_failure(body) is body


def test_terminal_cancellation_does_not_replace_recorded_provider_error():
    body = {"state": "cancelled", "current_call": {"state": "failed", "error": "HTTP 400", "stage": "batch:one"}}
    assert present_legacy_request_failure(body) is body
