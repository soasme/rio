"""Shared trust-aware staging for coding-session startup and replacement.

A session that is being stood up is a *candidate* until it is adopted. Until
then its journal writes are held back, so a startup that ends in a declined
trust prompt or an unusable provider leaves the journal exactly as it found it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from rio.ai.provider import ModelProvider
from rio.coding.session import CodingSession, CodingSessionConfig


@dataclass(frozen=True, slots=True)
class SessionPreparationRequest:
    """Frontend-neutral request for a staged coding session."""

    storage: object
    destination_cwd: Path
    model: str
    provider: str | None = None
    session_provider: str | None = None
    session_id: str | None = None


@dataclass(slots=True)
class PreparedCodingSession:
    """A candidate session whose resources are not authoritative until adopted."""

    session: CodingSession
    _state: str = "prepared"

    @property
    def provider(self) -> ModelProvider:
        return self.session.provider

    async def adopt(self) -> CodingSession:
        """Commit staged entries, then transfer the candidate's ownership."""
        if self._state != "prepared":
            raise RuntimeError(f"Prepared session is already {self._state}")
        trust_resolution = getattr(self.session, "project_trust_resolution", None)
        if trust_resolution is not None and trust_resolution.cancelled:
            await self.abort()
            raise ValueError("Project trust decision cancelled; session was not adopted")
        try:
            await self.session._commit_prepared_entries()
        except BaseException:
            await self.abort()
            raise
        self._state = "adopted"
        return self.session

    async def abort(self) -> None:
        """Close an unpublished candidate exactly once."""
        if self._state != "prepared":
            return
        self._state = "aborted"
        await self.session.aclose()


async def prepare_coding_session(
    config: CodingSessionConfig,
    *,
    session_loader: type[CodingSession] | None = None,
) -> PreparedCodingSession:
    """Prepare a session through the shared trust/provider lifecycle.

    Application frontends use this entry point with authoritative writes
    deferred. Adoption appends the complete staged batch before exposing the
    candidate.
    """
    staged_config = replace(config, defer_authoritative_writes=True)
    loader = session_loader or CodingSession
    session = await loader.load(staged_config)
    return PreparedCodingSession(session)


__all__ = [
    "PreparedCodingSession",
    "SessionPreparationRequest",
    "prepare_coding_session",
]
