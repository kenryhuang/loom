from __future__ import annotations

from dataclasses import replace

from loom.runtime.workflow_routing import (
    WorkflowRouteController,
    WorkflowRoutePhase,
    WorkflowRoutePolicy,
    workflow_route_state_dict,
    workflow_route_state_from_mapping,
)

NOW = "2026-08-02T00:00:00.000Z"


def test_auto_route_starts_undecided_and_emits_initial_request():
    controller = WorkflowRouteController("auto", now=lambda: NOW)

    assert controller.state.phase is WorkflowRoutePhase.UNDECIDED
    assert controller.state.task_execution_started is False
    events = controller.drain_events()
    assert [event.event_type for event in events] == ["workflow.routing.requested"]
    assert events[0].trigger == "initial"
    assert events[0].reason == "Determine whether this task needs an explicit plan"


def test_continue_react_records_reason_and_requires_a_real_task_step():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    controller.drain_events()

    result = controller.select_react("One direct edit is sufficient")

    assert result.ok
    assert result.value.phase is WorkflowRoutePhase.REACT
    assert result.value.reason == "One direct edit is sufficient"
    assert result.value.task_execution_started is False
    events = controller.drain_events()
    assert [event.event_type for event in events] == ["workflow.route.selected"]
    assert events[0].route is WorkflowRoutePhase.REACT


def test_force_route_starts_in_plan_without_initial_router():
    controller = WorkflowRouteController("force", now=lambda: NOW)

    assert controller.state.phase is WorkflowRoutePhase.PLAN
    assert controller.drain_events() == ()


def test_route_selection_rejects_empty_reason_without_mutating_state():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    original = controller.state

    result = controller.select_plan("  ")

    assert not result.ok
    assert result.error.code == "WORKFLOW_ROUTE_REASON_REQUIRED"
    assert controller.state == original


def test_two_failures_request_one_bounded_review():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    controller.drain_events()
    controller.select_react("start directly")
    controller.drain_events()
    controller.mark_task_execution_started()

    assert controller.observe_tool(failed=True) is False
    assert controller.observe_tool(failed=True) is True
    assert controller.state.phase is WorkflowRoutePhase.REVIEWING
    assert controller.state.review_count == 1
    events = controller.drain_events()
    assert [event.event_type for event in events] == ["workflow.routing.requested"]
    assert events[0].trigger == "tool_failures"


def test_tool_call_threshold_requests_review_at_literal_budget():
    controller = WorkflowRouteController(
        "auto",
        policy=WorkflowRoutePolicy(tool_call_threshold=3),
        now=lambda: NOW,
    )
    controller.drain_events()
    controller.select_react("start directly")
    controller.drain_events()
    controller.mark_task_execution_started()

    assert controller.observe_tool(failed=False) is False
    assert controller.observe_tool(failed=False) is False
    assert controller.observe_tool(failed=False) is True
    assert controller.state.trigger == "tool_budget"


def test_continue_after_review_resets_counts_and_starts_cooldown():
    controller = WorkflowRouteController(
        "auto",
        policy=WorkflowRoutePolicy(failure_threshold=1, cooldown_calls=2),
        now=lambda: NOW,
    )
    controller.drain_events()
    controller.select_react("start directly")
    controller.drain_events()
    controller.mark_task_execution_started()
    assert controller.observe_tool(failed=True) is True

    result = controller.select_react("continue with gathered evidence")

    assert result.ok
    assert result.value.calls_since_review == 0
    assert result.value.consecutive_failures == 0
    assert result.value.cooldown_remaining == 2
    assert result.value.task_execution_started is False


def test_cooldown_and_review_cap_suppress_repeated_reviews():
    controller = WorkflowRouteController(
        "auto",
        policy=WorkflowRoutePolicy(
            failure_threshold=1,
            tool_call_threshold=100,
            cooldown_calls=2,
            max_reviews=1,
        ),
        now=lambda: NOW,
    )
    controller.drain_events()
    controller.select_react("start directly")
    controller.mark_task_execution_started()
    assert controller.observe_tool(failed=True) is True
    controller.select_react("keep going")
    controller.mark_task_execution_started()

    assert controller.observe_tool(failed=True) is False
    assert controller.observe_tool(failed=True) is False
    assert controller.observe_tool(failed=True) is False
    assert controller.state.phase is WorkflowRoutePhase.REACT
    assert controller.state.review_count == 1


def test_external_review_signal_obeys_eligibility():
    controller = WorkflowRouteController("auto", now=lambda: NOW)

    inactive = controller.request_review("context_compaction", "Context compacted")
    assert inactive.ok
    assert inactive.value is False

    controller.select_react("start directly")
    controller.mark_task_execution_started()
    requested = controller.request_review("context_compaction", "Context compacted")

    assert requested.ok
    assert requested.value is True
    assert controller.state.phase is WorkflowRoutePhase.REVIEWING
    assert controller.state.trigger == "context_compaction"


def test_route_snapshot_round_trips_literal_values():
    controller = WorkflowRouteController("auto", now=lambda: NOW)
    controller.select_react("start directly")
    controller.mark_task_execution_started()
    expected = replace(controller.state, consecutive_failures=1, calls_since_review=4)

    restored = workflow_route_state_from_mapping(workflow_route_state_dict(expected))

    assert restored == expected
