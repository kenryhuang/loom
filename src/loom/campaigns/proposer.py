"""Vendor-neutral proposer protocol and bounded local debug adapter."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from loom.campaigns.contracts import (
    ArtifactRef,
    CandidateDraft,
    CandidateHypothesis,
    CandidateKind,
    CapabilityManifest,
    MetricPrediction,
)
from loom.campaigns.sandbox import LengthBoundedChannel
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, utc_now
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    campaign_id: str
    iteration: int
    max_candidates: int
    remaining_budget: FrozenDict
    editable_surfaces: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]
    output_contract: str = "loom.candidate-drafts.v1"

    def __post_init__(self) -> None:
        frozen = freeze_json(self.remaining_budget)
        if not isinstance(frozen, FrozenDict):
            raise TypeError("Proposal remaining budget must be a mapping")
        object.__setattr__(self, "remaining_budget", frozen)
        object.__setattr__(self, "editable_surfaces", tuple(self.editable_surfaces))
        object.__setattr__(self, "forbidden_surfaces", tuple(self.forbidden_surfaces))
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")


@dataclass(frozen=True, slots=True)
class ProposalUsage:
    proposer_tokens: int
    cost: str
    wall_time_seconds: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.proposer_tokens, bool)
            or not isinstance(self.proposer_tokens, int)
            or self.proposer_tokens < 0
            or isinstance(self.wall_time_seconds, bool)
            or not isinstance(self.wall_time_seconds, int)
            or self.wall_time_seconds < 0
        ):
            raise ValueError("Proposal usage counts must be non-negative integers")
        try:
            cost = Decimal(self.cost)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Proposal cost must be a decimal string") from exc
        if not cost.is_finite() or cost < 0:
            raise ValueError("Proposal cost must be finite and non-negative")
        object.__setattr__(self, "cost", format(cost, "f"))


@dataclass(frozen=True, slots=True)
class ProposalBatch:
    drafts: tuple[CandidateDraft | Mapping[str, Any], ...]
    usage: ProposalUsage

    def __post_init__(self) -> None:
        object.__setattr__(self, "drafts", tuple(self.drafts))
        if not self.drafts:
            raise ValueError("Proposal batch must contain at least one draft")


@dataclass(frozen=True, slots=True)
class MeteredProposalResult:
    output_frame: bytes
    usage: ProposalUsage

    def __post_init__(self) -> None:
        if not isinstance(self.output_frame, bytes):
            raise TypeError("Metered proposer output must be bytes")


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    campaign_id: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    max_tokens: int
    request_digest: str


class InferenceBroker(Protocol):
    async def infer(self, request: InferenceRequest) -> Result: ...


class ProposerAdapter(Protocol):
    async def propose(self, request: ProposalRequest, history, workspace) -> Result: ...


class SubprocessProposerAdapter:
    """Local adapter for development; production callers must provide real OS isolation."""

    def __init__(
        self,
        command: tuple[str, ...],
        artifact_store,
        *,
        trusted_local_debug: bool = False,
        timeout_seconds: float = 300,
        max_output_bytes: int = 1_000_000,
    ):
        self.command = tuple(command)
        self.artifact_store = artifact_store
        self.trusted_local_debug = trusted_local_debug
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.last_session_ref: ArtifactRef | None = None

    async def propose(self, request: ProposalRequest, history, workspace) -> Result:
        del history
        if not self.trusted_local_debug:
            return err(
                make_loom_error(
                    "SANDBOX_UNAVAILABLE",
                    "A plain subprocess is not an acceptable proposer sandbox",
                    retryable=False,
                )
            )
        request_payload = {
            "schema_version": "loom.proposal-request.v1",
            "campaign_id": request.campaign_id,
            "iteration": request.iteration,
            "max_candidates": request.max_candidates,
            "remaining_budget": request.remaining_budget,
            "editable_surfaces": request.editable_surfaces,
            "forbidden_surfaces": request.forbidden_surfaces,
            "output_contract": request.output_contract,
        }
        written = workspace.write_text("proposal-request.json", canonical_json_bytes(request_payload).decode())
        if not written.ok:
            return written
        started_at = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=workspace.root,
            env={},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        stdout = stdout[: self.max_output_bytes]
        stderr = stderr[: self.max_output_bytes]
        elapsed = max(0, math.ceil(time.monotonic() - started_at))
        session = {
            "schema_version": "loom.proposer-session.v1",
            "created_at": utc_now(),
            "request": request_payload,
            "command": list(self.command),
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "usage": {"proposer_tokens": 0, "cost": "0", "wall_time_seconds": elapsed},
        }
        published = self.artifact_store.publish_bytes(
            canonical_json_bytes(session),
            kind="proposer_session",
            schema_version="loom.proposer-session.v1",
            suffix=".json",
        )
        if published.ok:
            self.last_session_ref = published.value
        if timed_out:
            return err(make_loom_error("TIMEOUT", "Proposer process timed out", retryable=True))
        if process.returncode != 0:
            return _proposal_error("Proposer process failed", returncode=process.returncode)
        output = workspace.resolve("candidate-drafts.json")
        if not output.ok:
            return output
        try:
            payload = json.loads(Path(output.value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _proposal_error("Proposer output is missing or malformed", cause={"name": type(exc).__name__, "message": str(exc)})
        if not isinstance(payload, list) or not payload or len(payload) > request.max_candidates:
            return _proposal_error("Proposer returned an invalid number of drafts", count=len(payload) if isinstance(payload, list) else None)
        try:
            drafts = tuple(candidate_draft_from_mapping(item) for item in payload)
        except (KeyError, TypeError, ValueError) as exc:
            return _proposal_error("Proposer draft schema is invalid", cause={"name": type(exc).__name__, "message": str(exc)})
        digests = tuple(canonical_digest(draft) for draft in drafts)
        if len(set(digests)) != len(digests):
            return _proposal_error("Proposer returned duplicate drafts")
        return ok(ProposalBatch(drafts, ProposalUsage(0, "0", elapsed)))


class SandboxProposerAdapter:
    """Production proposer bridge to a strong isolation backend.

    The controller sends only the minimal proposal request. The runtime image
    owns its broker and read-only history clients; no provider credentials or
    hidden phase signals are serialized into this frame.
    """

    def __init__(self, backend, *, spec, mounts, artifact_store, max_frame_bytes: int = 1_000_000):
        self.backend = backend
        self.spec = spec
        self.mounts = mounts
        self.artifact_store = artifact_store
        self.max_frame_bytes = max_frame_bytes
        self.last_session_ref: ArtifactRef | None = None

    async def propose(self, request: ProposalRequest, history, workspace) -> Result:
        del history, workspace
        payload = {
            "schema_version": "loom.proposal-request.v1",
            "campaign_id": request.campaign_id,
            "iteration": request.iteration,
            "max_candidates": request.max_candidates,
            "remaining_budget": request.remaining_budget,
            "editable_surfaces": request.editable_surfaces,
            "forbidden_surfaces": request.forbidden_surfaces,
            "output_contract": request.output_contract,
        }
        channel = LengthBoundedChannel(self.max_frame_bytes)
        framed = channel.encode(canonical_json_bytes(payload))
        if not framed.ok:
            return framed
        result = await self.backend.run(self.spec, self.mounts, framed.value)
        session = {
            "schema_version": "loom.proposer-session.v1",
            "created_at": utc_now(),
            "request_digest": canonical_digest(payload),
            "sandboxed": True,
            "result": "ok" if result.ok else "failed",
        }
        published = self.artifact_store.publish_bytes(
            canonical_json_bytes(session),
            kind="proposer_session",
            schema_version="loom.proposer-session.v1",
            suffix=".json",
        )
        if not published.ok:
            return published
        self.last_session_ref = published.value
        if not result.ok:
            return result
        if not isinstance(result.value, MeteredProposalResult):
            return _proposal_error("Sandbox proposer backend omitted trusted metering")
        decoded = channel.decode(result.value.output_frame)
        if not decoded.ok:
            return decoded
        try:
            envelope = json.loads(decoded.value)
            if not isinstance(envelope, Mapping) or envelope.get("schema_version") != "loom.proposal-batch.v1":
                raise ValueError("proposal batch envelope is missing")
            values = envelope["drafts"]
            if not isinstance(values, list) or not values or len(values) > request.max_candidates:
                raise ValueError("invalid draft count")
            drafts = tuple(candidate_draft_from_mapping(item) for item in values)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return _proposal_error(
                "Sandbox proposer output is invalid",
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        digests = tuple(canonical_digest(draft) for draft in drafts)
        if len(set(digests)) != len(digests):
            return _proposal_error("Sandbox proposer returned duplicate drafts")
        return ok(ProposalBatch(drafts, result.value.usage))


def candidate_draft_from_mapping(value: Any) -> CandidateDraft:
    if not isinstance(value, Mapping):
        raise TypeError("draft must be an object")
    hypothesis = value["hypothesis"]
    capability = value["capability_manifest"]
    if not isinstance(hypothesis, Mapping) or not isinstance(capability, Mapping):
        raise TypeError("draft hypothesis and capability manifest must be objects")
    return CandidateDraft(
        CandidateKind(str(value["kind"])),
        tuple(value.get("parent_ids", ())),
        tuple(value.get("inspiration_ids", ())),
        CandidateHypothesis(
            str(hypothesis["problem"]),
            str(hypothesis["mechanism"]),
            tuple(MetricPrediction(str(item["metric_id"]), float(item["expected_delta"])) for item in hypothesis["expected_improvements"]),
            tuple(MetricPrediction(str(item["metric_id"]), float(item["expected_delta"])) for item in hypothesis["expected_regressions"]),
            tuple(str(item) for item in hypothesis["preserved_behaviors"]),
        ),
        tuple(str(item) for item in value["evidence_refs"]),
        tuple(str(item) for item in value["changed_surfaces"]),
        str(value["artifact_path"]),
        None if value.get("patch_path") is None else str(value["patch_path"]),
        CapabilityManifest(
            tuple(str(item) for item in capability.get("imports", ())),
            tuple(str(item) for item in capability.get("dependencies", ())),
            tuple(str(item) for item in capability.get("filesystem_read_roots", ())),
            tuple(str(item) for item in capability.get("filesystem_write_roots", ())),
            tuple(str(item) for item in capability.get("callable_tools", ())),
            capability["input_schema"],
            capability["output_schema"],
            capability.get("resource_limits", {}),
            bool(capability.get("subprocess_required", False)),
            bool(capability.get("network_required", False)),
        ),
    )


def _proposal_error(message: str, **metadata) -> Result:
    cause = metadata.pop("cause", None)
    return err(make_loom_error("PROPOSAL_FAILED", message, retryable=False, cause=cause, metadata=metadata))


__all__ = [
    "InferenceBroker",
    "InferenceRequest",
    "MeteredProposalResult",
    "ProposalRequest",
    "ProposalBatch",
    "ProposalUsage",
    "ProposerAdapter",
    "SandboxProposerAdapter",
    "SubprocessProposerAdapter",
    "candidate_draft_from_mapping",
]
