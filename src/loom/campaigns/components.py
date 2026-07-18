"""Versioned narrow component registry and sandbox-only adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.campaigns.contracts import PromotionDecision, PromotionDisposition, RiskLevel
from loom.campaigns.sandbox import LengthBoundedChannel, RootlessContainerSandbox
from loom.campaigns.serialization import canonical_json_bytes
from loom.core import ActorAssertion, Result, StaticIdentityProvider, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class ComponentDescriptor:
    component_type: str
    candidate_id: str
    version: str
    source_digest: str
    protocol_version: str
    max_output_bytes: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class SandboxComponentInvoker:
    """Typed bridge to an isolation backend; candidate code never runs here."""

    backend: Any
    spec: Any
    mounts: Any
    max_frame_bytes: int

    def __post_init__(self) -> None:
        if self.max_frame_bytes <= 0:
            raise ValueError("Component frame limit must be positive")

    async def invoke(self, payload: Mapping[str, Any]) -> Result:
        channel = LengthBoundedChannel(self.max_frame_bytes)
        encoded = channel.encode(canonical_json_bytes(payload))
        if not encoded.ok:
            return encoded
        result = await self.backend.run(self.spec, self.mounts, encoded.value)
        if not result.ok:
            return result
        if not isinstance(result.value, bytes):
            return _component_error("Sandbox returned a non-byte output frame")
        decoded = channel.decode(result.value)
        if not decoded.ok:
            return decoded
        try:
            output = json.loads(decoded.value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return err(
                make_loom_error(
                    "CANDIDATE_COMPONENT_INVALID",
                    "Sandbox returned invalid component JSON",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        if not isinstance(output, dict):
            return _component_error("Component output must be a structured object")
        return ok(output)


@dataclass(frozen=True, slots=True)
class RegisteredComponent:
    descriptor: ComponentDescriptor
    invoker: SandboxComponentInvoker


class ComponentAdapter:
    def __init__(self, registered: RegisteredComponent):
        self.registered = registered

    async def invoke(self, payload: Any) -> Result:
        if not isinstance(payload, Mapping):
            return _component_error("Component input must be a structured object")
        result = await self.registered.invoker.invoke(dict(payload))
        if not result.ok:
            return result
        output = result.value
        if len(canonical_json_bytes(output)) > self.registered.descriptor.max_output_bytes:
            return _component_error("Component output exceeds its byte limit")
        return ok(dict(output))


class CandidateComponentRegistry:
    def __init__(self):
        self._items: dict[tuple[str, str], RegisteredComponent] = {}

    def install(
        self,
        descriptor: ComponentDescriptor,
        invoker: SandboxComponentInvoker,
        *,
        actor: ActorAssertion,
        identity_provider: StaticIdentityProvider,
        promotion: PromotionDecision,
    ) -> Result:
        authorized = identity_provider.authorize(
            actor,
            "registry_operator",
            forbidden_roles=("campaign_controller", "campaign_finalizer", "governance_approver", "governance_automation"),
        )
        if not authorized.ok:
            return authorized
        if not isinstance(invoker, SandboxComponentInvoker):
            return _component_error("Component registration requires a sandbox invoker")
        if not isinstance(invoker.backend, RootlessContainerSandbox):
            return _component_error("Component registration requires the admitted rootless container backend")
        if (
            promotion.candidate_id != descriptor.candidate_id
            or promotion.decision is not PromotionDisposition.PROMOTED
            or promotion.computed_risk is not RiskLevel.HIGH
            or promotion.human_approval is None
            or promotion.active_artifact is None
            or promotion.active_artifact.sha256 != descriptor.source_digest
        ):
            return _component_error("Executable component registration requires its approved high-risk promotion decision")
        key = (descriptor.component_type, descriptor.version)
        if key in self._items:
            return err(make_loom_error("REGISTRY_CONFLICT", "Component version is already registered", retryable=False))
        registered = RegisteredComponent(descriptor, invoker)
        self._items[key] = registered
        return ok(registered)

    def get(self, component_type: str, version: str) -> Result:
        value = self._items.get((component_type, version))
        if value is None:
            return err(make_loom_error("VALIDATION_FAILED", "Component version was not found", retryable=False))
        return ok(value)

    def adapter(self, component_type: str, version: str) -> Result:
        registered = self.get(component_type, version)
        return registered if not registered.ok else ok(ComponentAdapter(registered.value))


def _component_error(message: str) -> Result:
    return err(make_loom_error("CANDIDATE_COMPONENT_INVALID", message, retryable=False))


__all__ = [
    "CandidateComponentRegistry",
    "ComponentAdapter",
    "ComponentDescriptor",
    "RegisteredComponent",
    "SandboxComponentInvoker",
]
