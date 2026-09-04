"""Errors raised while validating and committing SKILL.state step output."""

from __future__ import annotations


class StateValidationError(Exception):
    """A proposed state_delta failed deterministic runtime validation.

    Validation happens in the runtime, not the model, so a malformed patch
    never corrupts persistent state -- it triggers a rollback-retry cycle
    instead (see `RetriesExhaustedError`).
    """


class ActionNotFoundError(Exception):
    """The model proposed an action that isn't declared on the skill."""


class RetriesExhaustedError(Exception):
    """The rollback-retry cycle exceeded `max_retries` without a valid step."""
