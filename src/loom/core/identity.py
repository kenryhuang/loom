"""Shared authenticated identity assertions for authority-bearing stores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from loom.core.models import Result, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class ActorAssertion:
    subject: str
    roles: tuple[str, ...]
    issued_at: str
    expires_at: str
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))


class StaticIdentityProvider:
    """Deterministic exact-assertion identity provider for local deployments."""

    def __init__(self, assertions: tuple[ActorAssertion, ...], *, revoked_signatures: tuple[str, ...] = ()):
        self._assertions = {(item.subject, item.signature): item for item in assertions}
        self._revoked = frozenset(revoked_signatures)

    def authenticate(self, assertion: ActorAssertion) -> Result:
        if assertion.signature in self._revoked:
            return _authentication_error("Identity assertion has been revoked")
        trusted = self._assertions.get((assertion.subject, assertion.signature))
        if trusted != assertion:
            return _authentication_error("Identity assertion is not trusted")
        try:
            if _parse_time(assertion.expires_at) <= datetime.now(UTC):
                return _authentication_error("Identity assertion has expired")
        except ValueError:
            return _authentication_error("Identity assertion timestamp is invalid")
        return ok(assertion)

    def authorize(self, assertion: ActorAssertion, required_role: str, *, forbidden_roles: tuple[str, ...] = ()) -> Result:
        authenticated = self.authenticate(assertion)
        if not authenticated.ok:
            return authenticated
        if required_role not in assertion.roles or set(assertion.roles) & set(forbidden_roles):
            return err(
                make_loom_error(
                    "AUTHORIZATION_FAILED",
                    "Actor role is not authorized or violates separation of duties",
                    retryable=False,
                    metadata={
                        "subject": assertion.subject,
                        "required_role": required_role,
                        "roles": assertion.roles,
                        "forbidden_roles": forbidden_roles,
                    },
                )
            )
        return ok(assertion)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _authentication_error(message: str) -> Result:
    return err(make_loom_error("AUTHENTICATION_FAILED", message, retryable=False))


__all__ = ["ActorAssertion", "StaticIdentityProvider"]
