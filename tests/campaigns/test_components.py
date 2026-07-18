from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from loom.campaigns.components import (
    CandidateComponentRegistry,
    ComponentDescriptor,
    SandboxComponentInvoker,
)
from loom.campaigns.contracts import (
    ApprovalRecord,
    ArtifactRef,
    GateDecision,
    GateResult,
    PromotionDecision,
    PromotionDisposition,
    RiskLevel,
)
from loom.campaigns.sandbox import LengthBoundedChannel, RootlessContainerSandbox
from loom.core import ActorAssertion, StaticIdentityProvider


def _identity():
    actor = ActorAssertion(
        subject="governance",
        roles=("registry_operator",),
        issued_at="2026-07-18T00:00:00Z",
        expires_at="2999-07-18T00:00:00Z",
        signature="trusted",
    )
    return actor, StaticIdentityProvider((actor,))


def _promotion(descriptor: ComponentDescriptor) -> PromotionDecision:
    active = ArtifactRef("loom.governed-candidate.v1", "governed_candidate", "candidate.json", descriptor.source_digest, 1)
    rollback = ArtifactRef("baseline.v1", "baseline", "baseline.json", "b" * 64, 1)
    rule_set = ArtifactRef("loom.risk-rule-set.v1", "risk_rules", "rules.json", "a" * 64, 1)
    gate = GateDecision("security", True, GateResult.PASSED, (), "passed")
    approval = ApprovalRecord(
        "approver",
        "governance_approver",
        "2026-07-18T00:00:00Z",
        "2999-07-18T00:00:00Z",
        descriptor.source_digest,
        rollback.sha256,
        "c" * 64,
        rule_set.sha256,
        "d" * 64,
        "approve",
    )
    return PromotionDecision(
        "loom.promotion-decision.v1",
        "decision_test",
        "cmp_test",
        descriptor.candidate_id,
        "2026-07-18T00:00:00Z",
        rollback.sha256,
        "c" * 64,
        rule_set,
        (gate,),
        RiskLevel.HIGH,
        ("executable_component",),
        approval,
        PromotionDisposition.PROMOTED,
        active,
        rollback,
    )


class _Backend(RootlessContainerSandbox):
    def __init__(self, output, max_bytes=128):
        self.output = output
        self.max_bytes = max_bytes
        self.calls = []

    async def run(self, spec, mounts, input_frame):
        del spec, mounts
        payload = json.loads(LengthBoundedChannel(self.max_bytes).decode(input_frame).unwrap())
        self.calls.append(payload)
        output = json.dumps(self.output, separators=(",", ":")).encode()
        return LengthBoundedChannel(self.max_bytes).encode(output)


def _invoker(output, max_bytes=128):
    backend = _Backend(output, max_bytes=max_bytes)
    return SandboxComponentInvoker(backend, spec=object(), mounts=object(), max_frame_bytes=max_bytes), backend


def test_component_registry_requires_governance_authority_and_versions_are_immutable():
    registry = CandidateComponentRegistry()
    descriptor = ComponentDescriptor("context_builder", "cand_a", "v1", "a" * 64, "context_builder.v1")
    invoker, _ = _invoker({"context": "bounded"})
    actor, provider = _identity()

    denied_actor = ActorAssertion(
        subject="controller",
        roles=("campaign_controller",),
        issued_at=actor.issued_at,
        expires_at=actor.expires_at,
        signature="denied",
    )
    denied_provider = StaticIdentityProvider((denied_actor,))
    promotion = _promotion(descriptor)
    denied = registry.install(descriptor, invoker, actor=denied_actor, identity_provider=denied_provider, promotion=promotion)
    unapproved = registry.install(
        descriptor,
        invoker,
        actor=actor,
        identity_provider=provider,
        promotion=replace(promotion, human_approval=None),
    )
    installed = registry.install(descriptor, invoker, actor=actor, identity_provider=provider, promotion=promotion)
    duplicate = registry.install(descriptor, invoker, actor=actor, identity_provider=provider, promotion=promotion)

    assert not denied.ok and denied.error.code == "AUTHORIZATION_FAILED"
    assert not unapproved.ok and unapproved.error.code == "CANDIDATE_COMPONENT_INVALID"
    assert installed.ok
    assert not duplicate.ok and duplicate.error.code == "REGISTRY_CONFLICT"
    assert registry.get("context_builder", "v1").unwrap().descriptor == descriptor


def test_component_adapter_validates_input_output_schema_and_size_without_in_process_candidate_call():
    registry = CandidateComponentRegistry()
    descriptor = ComponentDescriptor("context_builder", "cand_a", "v1", "b" * 64, "context_builder.v1", max_output_bytes=64)
    invoker, backend = _invoker({"context": "bounded"})
    actor, provider = _identity()
    registry.install(descriptor, invoker, actor=actor, identity_provider=provider, promotion=_promotion(descriptor)).unwrap()
    adapter = registry.adapter("context_builder", "v1").unwrap()

    assert asyncio.run(adapter.invoke({"observation": "task local"})).unwrap() == {"context": "bounded"}
    assert backend.calls == [{"observation": "task local"}]
    assert not asyncio.run(adapter.invoke("raw string")).ok


def test_sandbox_component_invoker_uses_framed_json_channel():
    class Backend:
        def __init__(self):
            self.input_frame = None

        async def run(self, spec, mounts, input_frame):
            self.input_frame = input_frame
            output = json.dumps({"context": "safe"}, separators=(",", ":")).encode()
            return LengthBoundedChannel(spec.output_bytes).encode(output)

    backend = Backend()
    invoker = SandboxComponentInvoker(backend, spec=type("Spec", (), {"output_bytes": 128})(), mounts=object(), max_frame_bytes=128)

    result = asyncio.run(invoker.invoke({"observation": "x"}))

    assert result.unwrap() == {"context": "safe"}
    assert LengthBoundedChannel(128).decode(backend.input_frame).unwrap() == b'{"observation":"x"}'
