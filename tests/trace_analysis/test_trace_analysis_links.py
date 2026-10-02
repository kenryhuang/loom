from loom.trace_analysis.graph import build_episode_graph
from loom.trace_analysis.links import linked_tools_for_round, tool_output
from loom.trace_analysis.schemas import NormalizedEvent


def _event(event_type, *, hash, llm_call_id=None, tool_call_id=None, payload=None):
    event_payload = {
        "type": event_type,
        "run_id": "run-1",
        "loop_id": "loop-1",
        "trace_id": "trace-1",
        "step_number": 0,
    }
    if payload:
        event_payload.update(payload)
    return NormalizedEvent(
        record_id=hash,
        event_type=event_type,
        run_id="run-1",
        loop_id="loop-1",
        trace_id="trace-1",
        step_number=0,
        llm_call_id=llm_call_id,
        tool_call_id=tool_call_id,
        tool_id="read_file" if event_type.startswith("tool.") else None,
        at=None,
        payload=event_payload,
        hash=hash,
    )


def test_linked_tools_prefers_explicit_parent_llm_call_id():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request", llm_call_id="llm-1"),
            _event("llm.completed", hash="complete", llm_call_id="llm-1", payload={"response": {"tool_calls": []}}),
            _event("tool.started", hash="tool-start", llm_call_id="llm-1", tool_call_id="opaque-call"),
            _event("tool.completed", hash="tool-complete", llm_call_id="llm-1", tool_call_id="opaque-call"),
        )
    )

    linked = linked_tools_for_round(graph, graph.llm_rounds[0])

    assert [(tool.tool_call_id, basis) for tool, basis in linked] == [("opaque-call", "explicit_parent_id")]


def test_linked_tools_uses_unique_native_tool_call_id():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request", llm_call_id="llm-1"),
            _event(
                "llm.completed",
                hash="complete",
                llm_call_id="llm-1",
                payload={"response": {"tool_calls": ({"id": "native-call"},)}},
            ),
            _event("tool.started", hash="tool-start", tool_call_id="native-call"),
            _event("tool.completed", hash="tool-complete", tool_call_id="native-call"),
        )
    )

    linked = linked_tools_for_round(graph, graph.llm_rounds[0])

    assert [(tool.tool_call_id, basis) for tool, basis in linked] == [("native-call", "native_tool_call_id")]


def test_linked_tools_uses_unique_legacy_json_action_id_and_leaves_ambiguity_unlinked():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request-1", llm_call_id="trace-llm-1"),
            _event("llm.completed", hash="complete-1", llm_call_id="trace-llm-1", payload={"response": {"tool_calls": []}}),
            _event("llm.requested", hash="request-2", llm_call_id="trace-llm-2"),
            _event("llm.completed", hash="complete-2", llm_call_id="trace-llm-2", payload={"response": {"tool_calls": ({"id": "shared"},)}}),
            _event("llm.requested", hash="request-3", llm_call_id="trace-llm-3"),
            _event("llm.completed", hash="complete-3", llm_call_id="trace-llm-3", payload={"response": {"tool_calls": ({"id": "shared"},)}}),
            _event("tool.started", hash="legacy-start", tool_call_id="trace-llm-1-json-tool-1"),
            _event("tool.completed", hash="legacy-complete", tool_call_id="trace-llm-1-json-tool-1"),
            _event("tool.started", hash="ambiguous-start", tool_call_id="shared"),
            _event("tool.completed", hash="ambiguous-complete", tool_call_id="shared"),
        )
    )

    first_linked = linked_tools_for_round(graph, graph.llm_rounds[0])
    second_linked = linked_tools_for_round(graph, graph.llm_rounds[1])
    third_linked = linked_tools_for_round(graph, graph.llm_rounds[2])

    assert [(tool.tool_call_id, basis) for tool, basis in first_linked] == [
        ("trace-llm-1-json-tool-1", "legacy_id_convention")
    ]
    assert second_linked == ()
    assert third_linked == ()


def test_tool_output_unwraps_observation_value_and_reports_field_path():
    event = _event(
        "tool.completed",
        hash="tool-complete",
        tool_call_id="call-1",
        payload={
            "output": {
                "at": "2026-01-01T00:00:00Z",
                "id": "observation-1",
                "source": "read_file",
                "value": {"content": "the useful result"},
            }
        },
    )

    assert tool_output(event) == ({"content": "the useful result"}, "output.value")


def test_tool_output_preserves_domain_mapping_with_value_key():
    event = _event(
        "tool.completed",
        hash="tool-complete",
        tool_call_id="call-1",
        payload={"output": {"value": 42, "unit": "kg"}},
    )

    assert tool_output(event) == ({"value": 42, "unit": "kg"}, "output")


def test_legacy_link_requires_numeric_producer_suffix():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request", llm_call_id="abc"),
            _event("llm.completed", hash="complete", llm_call_id="abc", payload={"response": {"tool_calls": []}}),
            _event("tool.started", hash="tool-start", tool_call_id="abc-json-tool-not-generated"),
            _event("tool.completed", hash="tool-complete", tool_call_id="abc-json-tool-not-generated"),
        )
    )

    assert linked_tools_for_round(graph, graph.llm_rounds[0]) == ()


def test_reused_explicit_parent_id_is_ambiguous_without_native_identity():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request-1", llm_call_id="llm-1"),
            _event("llm.completed", hash="complete-1", llm_call_id="llm-1", payload={"response": {"tool_calls": []}}),
            _event("llm.requested", hash="request-2", llm_call_id="llm-1"),
            _event("llm.completed", hash="complete-2", llm_call_id="llm-1", payload={"response": {"tool_calls": []}}),
            _event("tool.started", hash="tool-start", llm_call_id="llm-1", tool_call_id="opaque-call"),
            _event("tool.completed", hash="tool-complete", llm_call_id="llm-1", tool_call_id="opaque-call"),
        )
    )

    assert linked_tools_for_round(graph, graph.llm_rounds[0]) == ()
    assert linked_tools_for_round(graph, graph.llm_rounds[1]) == ()


def test_unique_native_identity_resolves_reused_explicit_parent_id():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request-1", llm_call_id="llm-1"),
            _event("llm.completed", hash="complete-1", llm_call_id="llm-1", payload={"response": {"tool_calls": []}}),
            _event("llm.requested", hash="request-2", llm_call_id="llm-1"),
            _event(
                "llm.completed",
                hash="complete-2",
                llm_call_id="llm-1",
                payload={"response": {"tool_calls": ({"id": "native-call"},)}},
            ),
            _event("tool.started", hash="tool-start", llm_call_id="llm-1", tool_call_id="native-call"),
            _event("tool.completed", hash="tool-complete", llm_call_id="llm-1", tool_call_id="native-call"),
        )
    )

    first = linked_tools_for_round(graph, graph.llm_rounds[0])
    second = linked_tools_for_round(graph, graph.llm_rounds[1])

    assert first == ()
    assert [(tool.tool_call_id, basis) for tool, basis in second] == [("native-call", "native_tool_call_id")]


def test_reused_native_tool_id_does_not_resolve_ambiguous_parent():
    graph = build_episode_graph(
        (
            _event("llm.requested", hash="request-1", llm_call_id="llm-1"),
            _event("llm.completed", hash="complete-1", llm_call_id="llm-1", payload={"response": {"tool_calls": []}}),
            _event("llm.requested", hash="request-2", llm_call_id="llm-1"),
            _event(
                "llm.completed",
                hash="complete-2",
                llm_call_id="llm-1",
                payload={"response": {"tool_calls": ({"id": "native-call"},)}},
            ),
            _event("tool.started", hash="tool-start-1", llm_call_id="llm-1", tool_call_id="native-call"),
            _event("tool.completed", hash="tool-complete-1", llm_call_id="llm-1", tool_call_id="native-call"),
            _event("tool.started", hash="tool-start-2", llm_call_id="llm-1", tool_call_id="native-call"),
            _event("tool.completed", hash="tool-complete-2", llm_call_id="llm-1", tool_call_id="native-call"),
        )
    )

    assert linked_tools_for_round(graph, graph.llm_rounds[0]) == ()
    assert linked_tools_for_round(graph, graph.llm_rounds[1]) == ()
