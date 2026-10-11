"""Helpers for surfacing safe provider HTTP error details."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rio.ai._parsing import loads_object

_MAX_ERROR_DETAIL_LENGTH = 1000


def provider_http_error_message(
    *,
    provider_name: str,
    status_code: int,
    body: str,
    model: str | None = None,
) -> str:
    """Return an actionable, secret-free HTTP error message for a provider response."""
    prefix = f"{provider_name} request failed with status {status_code}"
    if model:
        prefix = f"{prefix} for model {model}"
    detail = provider_http_error_detail(body)
    if detail:
        return f"{prefix}: {detail}"
    return prefix


def provider_http_error_detail(body: str) -> str:
    """Extract a concise provider-supplied error detail from an HTTP body."""
    parsed = loads_object(body)
    if parsed is not None:
        detail = provider_error_detail_from_mapping(parsed)
        if detail:
            return detail
    return body.strip()[:_MAX_ERROR_DETAIL_LENGTH]


def provider_error_detail_from_mapping(value: Mapping[str, Any]) -> str:
    """Return the most useful message/code from a provider error object."""
    error = value.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    for key in ("message", "detail", "error"):
        detail = value.get(key)
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, Mapping):
            nested = provider_error_detail_from_mapping(detail)
            if nested:
                return nested
    return ""
