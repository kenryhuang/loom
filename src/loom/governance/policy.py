"""Governance identities, roles, and authenticated commands."""

from __future__ import annotations

from dataclasses import dataclass

from loom.campaigns.contracts import ApprovalRecord
from loom.core import ActorAssertion, StaticIdentityProvider


@dataclass(frozen=True, slots=True)
class GovernanceOperation:
    operation_id: str
    surface_id: str
    actor: ActorAssertion
    action: str


@dataclass(frozen=True, slots=True)
class GovernanceReview:
    review_id: str
    candidate_id: str
    actor: ActorAssertion
    approval: ApprovalRecord


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    review_id: str
    candidate_id: str
    subject: str
    decision: str
    gate_digest: str


__all__ = [
    "ActorAssertion",
    "GovernanceOperation",
    "GovernanceReview",
    "ReviewDecision",
    "StaticIdentityProvider",
]
