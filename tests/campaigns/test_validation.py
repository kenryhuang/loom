from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loom.campaigns.contracts import (
    CandidateDraft,
    CandidateHypothesis,
    CandidateKind,
    CandidatePolicy,
    CapabilityManifest,
    MetricPrediction,
)
from loom.campaigns.materialization import DeclarativePatchCompiler, SurfaceDefinition
from loom.campaigns.sandbox import LengthBoundedChannel, RootlessContainerSandbox
from loom.campaigns.serialization import canonical_digest
from loom.campaigns.validation import (
    DefaultCandidateValidator,
    SandboxExecutableSmokeValidator,
    ValidationResult,
    publish_validation_evidence,
    validate_executable_source,
)
from loom.campaigns.workspace import CandidateWorkspace

from .conftest import campaign_actor, make_campaign_spec, make_campaign_store


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        kinds=(CandidateKind.DECLARATIVE_PATCH,),
        editable_surfaces=("context_policy",),
        forbidden_surfaces=("evaluator", "permissions"),
        allowed_patch_operations=("set",),
    )


def _draft(patch_path: str, *, mechanism: str = "Reduce irrelevant context.", evidence=("ev-1",), changed=("context_policy",)) -> CandidateDraft:
    return CandidateDraft(
        CandidateKind.DECLARATIVE_PATCH,
        (),
        (),
        CandidateHypothesis(
            "Context is too large.",
            mechanism,
            (MetricPrediction("total_tokens", -100),),
            (MetricPrediction("task_success", -0.01),),
            ("workspace confinement",),
        ),
        evidence,
        changed,
        patch_path,
        patch_path,
        CapabilityManifest((), (), (), (), (), {"type": "object"}, {"type": "object"}, {}),
    )


def test_validator_runs_manifest_then_compiles_declarative_patch(tmp_path: Path):
    workspace = CandidateWorkspace.allocate(tmp_path, "cand_valid").unwrap()
    patch = {
        "base": {"context_policy": {"max_chars": 2000}},
        "operations": [{"op": "set", "path": "context_policy.max_chars", "value": 1500}],
    }
    workspace.write_text("patch.json", json.dumps(patch)).unwrap()
    compiler = DeclarativePatchCompiler((SurfaceDefinition("context_policy", ("max_chars",), {"max_chars": int}),))
    validator = DefaultCandidateValidator(workspace, compiler)

    result = asyncio.run(validator.validate(_draft("patch.json"), _policy()))

    assert result.ok
    assert result.value.valid
    assert result.value.stages == ("manifest", "declarative_patch", "inverse_smoke")
    assert result.value.compiled.inverse_operations


def test_validator_rejects_non_falsifiable_or_unauthorized_manifest_before_reading_patch(tmp_path: Path):
    workspace = CandidateWorkspace.allocate(tmp_path, "cand_invalid").unwrap()
    compiler = DeclarativePatchCompiler((SurfaceDefinition("context_policy", ("max_chars",), {"max_chars": int}),))
    validator = DefaultCandidateValidator(workspace, compiler)

    invalid = (
        _draft("missing.json", mechanism=""),
        _draft("missing.json", evidence=()),
        _draft("missing.json", changed=("evaluator",)),
    )
    for draft in invalid:
        result = asyncio.run(validator.validate(draft, _policy()))
        assert not result.ok
        assert result.error.code == "CANDIDATE_MANIFEST_INVALID"
        assert result.error.metadata["stage"] == "manifest"


def test_default_validator_wires_executable_artifact_through_static_capability_checks(tmp_path: Path):
    class SmokeBackend(RootlessContainerSandbox):
        def __init__(self):
            pass

        async def run(self, spec, mounts, input_frame):
            del spec, mounts
            channel = LengthBoundedChannel(1024 * 1024)
            request = json.loads(channel.decode(input_frame).unwrap())
            return channel.encode(
                json.dumps(
                    {
                        "schema_version": "loom.executable-smoke.v1",
                        "required_function": request["required_function"],
                        "passed": True,
                    }
                ).encode()
            )

    workspace = CandidateWorkspace.allocate(tmp_path, "cand_executable").unwrap()
    workspace.write_text(
        "component.json",
        json.dumps(
            {
                "required_function": "build_context",
                "sources": {"candidate.py": "import json\n\ndef build_context(payload):\n    return {'context': json.dumps(payload)}\n"},
            }
        ),
    ).unwrap()
    policy = CandidatePolicy(
        kinds=(CandidateKind.EXECUTABLE_COMPONENT,),
        editable_surfaces=("context_policy",),
        forbidden_surfaces=("evaluator", "permissions"),
        allowed_patch_operations=(),
        allowed_imports=("json",),
        max_source_files=2,
        max_source_bytes=4096,
        max_ast_nodes=200,
        executable_enabled=True,
    )
    draft = CandidateDraft(
        CandidateKind.EXECUTABLE_COMPONENT,
        (),
        (),
        _draft("unused").hypothesis,
        ("ev-1",),
        ("context_policy",),
        "component.json",
        None,
        CapabilityManifest(
            ("json",),
            (),
            ("/candidate", "/workspace"),
            ("/scratch",),
            (),
            {"type": "object"},
            {"type": "object"},
            {"memory_mb": 128},
        ),
    )
    compiler = DeclarativePatchCompiler((SurfaceDefinition("context_policy", ("max_chars",), {"max_chars": int}),))

    smoke = SandboxExecutableSmokeValidator(SmokeBackend(), object(), object())
    result = asyncio.run(DefaultCandidateValidator(workspace, compiler, smoke).validate(draft, policy))

    assert result.ok and result.value.executable is not None
    assert result.value.stages == ("manifest", "source", "capability", "sandbox_interface")

    no_sandbox = asyncio.run(DefaultCandidateValidator(workspace, compiler).validate(draft, policy))
    assert not no_sandbox.ok and no_sandbox.error.code == "SANDBOX_UNAVAILABLE"


def test_validation_evidence_requires_the_authenticated_campaign_controller(tmp_path: Path):
    store = make_campaign_store(tmp_path / "campaign")
    spec = make_campaign_spec(store)
    candidate_ref = store.artifacts.publish_bytes(
        b"{}",
        kind="candidate_bundle",
        schema_version="loom.candidate-bundle.v1",
    ).unwrap()

    denied = publish_validation_evidence(
        store,
        campaign_id=spec.campaign_id,
        candidate_id="candidate",
        candidate_ref=candidate_ref,
        result=ValidationResult(True, ("manifest",)),
        policy_digest=canonical_digest(spec.candidate_policy),
        actor=campaign_actor("campaign_finalizer"),
    )

    assert not denied.ok and denied.error.code == "AUTHORIZATION_FAILED"


def test_initial_executable_profile_rejects_candidate_callable_tools():
    policy = CandidatePolicy(
        kinds=(CandidateKind.EXECUTABLE_COMPONENT,),
        editable_surfaces=("context_policy",),
        forbidden_surfaces=("evaluator",),
        allowed_patch_operations=(),
        max_source_files=2,
        max_source_bytes=4096,
        max_ast_nodes=200,
        executable_enabled=True,
    )
    capability = CapabilityManifest(
        (),
        (),
        ("/candidate",),
        ("/scratch",),
        ("solver_infer",),
        {"type": "object"},
        {"type": "object"},
        {},
    )

    result = validate_executable_source(
        {"candidate.py": "def build_context(payload):\n    return payload\n"},
        capability,
        policy,
        required_function="build_context",
    )

    assert not result.ok
    assert result.error.code == "CAPABILITY_MISMATCH"
