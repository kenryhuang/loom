from __future__ import annotations

from loom.campaigns.contracts import CandidateKind, CandidatePolicy
from loom.campaigns.materialization import DeclarativePatchCompiler, SurfaceDefinition


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        kinds=(CandidateKind.DECLARATIVE_PATCH,),
        editable_surfaces=("context_policy", "completion_policy", "tool_description"),
        forbidden_surfaces=("evaluator", "permissions"),
        allowed_patch_operations=("set", "replace"),
    )


def test_declarative_compiler_materializes_mutation_and_exact_inverse():
    base = {
        "context_policy": {"max_chars": 2000},
        "completion_policy": {"extra_rounds": 1},
        "tool_description": {"finish": "Finish the task."},
    }
    compiler = DeclarativePatchCompiler(
        (
            SurfaceDefinition("context_policy", ("max_chars",), {"max_chars": int}),
            SurfaceDefinition("completion_policy", ("extra_rounds",), {"extra_rounds": int}),
            SurfaceDefinition("tool_description", ("finish",), {"finish": str}),
        )
    )
    operations = (
        {"op": "set", "path": "context_policy.max_chars", "value": 1500},
        {"op": "replace", "path": "tool_description.finish", "old": "Finish the task.", "value": "Finish with evidence."},
    )

    compiled = compiler.compile(base, operations, _policy(), evidence_trace_ids=("trace-1",)).unwrap()
    restored = compiler.apply(compiled.materialized, compiled.inverse_operations, _policy()).unwrap()

    assert compiled.materialized["context_policy"]["max_chars"] == 1500
    assert compiled.mutation_bundle.base_version == compiled.base_digest
    assert compiled.mutation_bundle.created_from_trace_ids == ("trace-1",)
    assert restored == compiled.base
    assert compiled.result_digest != compiled.base_digest


def test_declarative_compiler_rejects_forbidden_unknown_ambiguous_and_wrong_type_operations():
    compiler = DeclarativePatchCompiler((SurfaceDefinition("context_policy", ("max_chars",), {"max_chars": int}),))
    base = {"context_policy": {"max_chars": 2000}, "evaluator": {"rubric": "v1"}}
    invalid = (
        ({"op": "set", "path": "evaluator.rubric", "value": "v2"},),
        ({"op": "set", "path": "unknown.value", "value": 1},),
        ({"op": "set", "path": "context_policy", "value": {}},),
        ({"op": "set", "path": "context_policy.max_chars", "value": "large"},),
        ({"op": "replace", "path": "context_policy.max_chars", "old": 10, "value": 1000},),
    )

    for operations in invalid:
        result = compiler.compile(base, operations, _policy(), evidence_trace_ids=("trace-1",))
        assert not result.ok
        assert result.error.code == "CANDIDATE_PATCH_INVALID"


def test_declarative_compiler_implements_append_and_bounded_limit_semantics():
    policy = CandidatePolicy(
        kinds=(CandidateKind.DECLARATIVE_PATCH,),
        editable_surfaces=("context_policy",),
        forbidden_surfaces=(),
        allowed_patch_operations=("append_rule", "set_limit", "set"),
    )
    compiler = DeclarativePatchCompiler(
        (
            SurfaceDefinition(
                "context_policy",
                ("rules", "max_chars"),
                {"rules": list, "max_chars": int},
                {"max_chars": (256, 4096)},
            ),
        )
    )
    base = {"context_policy": {"rules": ["preserve evidence"], "max_chars": 2000}}

    compiled = compiler.compile(
        base,
        (
            {"op": "append_rule", "path": "context_policy.rules", "value": "prefer local files"},
            {"op": "set_limit", "path": "context_policy.max_chars", "value": 1500},
        ),
        policy,
        evidence_trace_ids=("trace-1",),
    ).unwrap()
    outside = compiler.compile(
        base,
        ({"op": "set_limit", "path": "context_policy.max_chars", "value": 100_000},),
        policy,
        evidence_trace_ids=("trace-1",),
    )

    assert compiled.materialized["context_policy"]["rules"] == ("preserve evidence", "prefer local files")
    assert compiled.materialized["context_policy"]["max_chars"] == 1500
    assert compiler.apply(compiled.materialized, compiled.inverse_operations, policy).unwrap() == compiled.base
    assert not outside.ok


def test_compiler_resolves_longest_dotted_surface_prefix():
    policy = CandidatePolicy(
        kinds=(CandidateKind.DECLARATIVE_PATCH,),
        editable_surfaces=("agent.loop_policy",),
        forbidden_surfaces=(),
        allowed_patch_operations=("set_limit", "set"),
    )
    compiler = DeclarativePatchCompiler(
        (
            SurfaceDefinition(
                "agent.loop_policy",
                ("max_tool_calls_per_step",),
                {"max_tool_calls_per_step": int},
                {"max_tool_calls_per_step": (1, 64)},
            ),
        )
    )

    compiled = compiler.compile(
        {"agent.loop_policy": {"max_tool_calls_per_step": 32}},
        (
            {
                "op": "set_limit",
                "path": "agent.loop_policy.max_tool_calls_per_step",
                "value": 8,
            },
        ),
        policy,
        evidence_trace_ids=("trace-1",),
    ).unwrap()

    assert compiled.materialized["agent.loop_policy"]["max_tool_calls_per_step"] == 8
    assert compiler.apply(compiled.materialized, compiled.inverse_operations, policy).unwrap() == compiled.base
