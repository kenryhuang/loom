from loom.runtime.planning import PlanController, PlanMode, PlanPhase


def _now():
    return "2026-07-31T00:00:00Z"


def _ids():
    values = iter(("plan_test", "step_1", "step_2", "step_3", "step_4"))
    return lambda _prefix: next(values)


def _controller(mode=PlanMode.AUTO):
    return PlanController(mode, id_factory=_ids(), now=_now)


def test_controller_enters_and_submits_plan_with_stable_ids():
    controller = _controller()

    entered = controller.enter("The task has dependent steps")
    submitted = controller.submit("Inspect, implement, verify", ("Inspect loop", "Implement planning"))

    assert entered.ok
    assert submitted.ok
    assert submitted.value.phase is PlanPhase.EXECUTING
    assert submitted.value.revision == 1
    assert [(item.id, item.content, item.status.value) for item in submitted.value.items] == [
        ("step_1", "Inspect loop", "pending"),
        ("step_2", "Implement planning", "pending"),
    ]


def test_force_mode_starts_in_planning_and_queues_entered_event():
    controller = _controller(PlanMode.FORCE)

    assert controller.state.phase is PlanPhase.PLANNING
    events = controller.drain_events()
    assert [event.event_type for event in events] == ["plan.entered"]
    assert events[0].trigger == "cli_force"


def test_update_allows_future_replan_and_freezes_terminal_history():
    controller = _controller()
    controller.enter("complex")
    controller.submit("initial", ("Inspect", "Implement"))

    first = controller.update(
        "inspection started",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Implement", "status": "pending", "note": None},
        ),
    )
    second = controller.update(
        "inspection complete",
        (
            {"id": "step_1", "content": "Inspect", "status": "completed", "note": None},
            {"id": "step_2", "content": "Implement carefully", "status": "in_progress", "note": None},
            {"content": "Verify", "status": "pending", "note": None},
        ),
    )
    illegal = controller.update(
        "rewrite history",
        (
            {"id": "step_1", "content": "Different", "status": "pending", "note": None},
            {"id": "step_2", "content": "Implement carefully", "status": "in_progress", "note": None},
            {"id": "step_3", "content": "Verify", "status": "pending", "note": None},
        ),
    )

    assert first.ok and second.ok
    assert second.value.items[-1].id == "step_3"
    assert not illegal.ok
    assert illegal.error.code == "PLAN_TERMINAL_HISTORY_IMMUTABLE"


def test_update_rejects_two_active_items():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect", "Implement"))

    result = controller.update(
        "too much",
        (
            {"id": "step_1", "content": "Inspect", "status": "in_progress", "note": None},
            {"id": "step_2", "content": "Implement", "status": "in_progress", "note": None},
        ),
    )

    assert not result.ok
    assert result.error.code == "PLAN_MULTIPLE_ACTIVE_ITEMS"


def test_update_requires_skip_reason():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect",))

    result = controller.update(
        "skip",
        ({"id": "step_1", "content": "Inspect", "status": "skipped", "note": ""},),
    )

    assert not result.ok
    assert result.error.code == "PLAN_SKIP_REASON_REQUIRED"


def test_finish_requires_all_items_terminal():
    controller = _controller()
    controller.enter("complex")
    controller.submit("plan", ("Inspect",))

    premature = controller.complete()
    controller.update("done", ({"id": "step_1", "content": "Inspect", "status": "completed", "note": None},))
    completed = controller.complete()

    assert not premature.ok
    assert premature.error.code == "PLAN_INCOMPLETE"
    assert completed.ok
    assert controller.state.phase is PlanPhase.COMPLETED
    assert [event.event_type for event in controller.drain_events()][-1] == "plan.completed"


def test_wrong_phase_calls_are_rejected_and_controllers_are_isolated():
    left = _controller()
    right = _controller()

    wrong = left.submit("too early", ("Inspect",))
    entered = right.enter("complex")

    assert not wrong.ok
    assert wrong.error.code == "PLAN_PHASE_INVALID"
    assert left.state.phase is PlanPhase.INACTIVE
    assert entered.ok
    assert right.state.phase is PlanPhase.PLANNING
