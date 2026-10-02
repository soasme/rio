"""Errors raised by the context loop."""

from __future__ import annotations


class RetriesExhaustedError(Exception):
    """The model failed to produce a usable action within `max_retries` retries."""


class ProviderResponseError(Exception):
    """The provider failed to produce an assistant message.

    Distinct from an unusable reply: the model never got to answer, so a retry
    of the same prompt cannot correct it.
    """
