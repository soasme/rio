"""Tests for the ΔΣ merge rule (arXiv:2608.26263 Algorithm 1)."""

from __future__ import annotations

import pytest

from rio_agent.errors import StateValidationError
from rio_agent.state import apply_state_delta, validate_state_delta


def test_apply_state_delta_replaces_and_adds_fields():
    state = {"a": 1, "b": 2}

    result = apply_state_delta(state, {"b": 3, "c": 4})

    assert result == {"a": 1, "b": 3, "c": 4}
    assert state == {"a": 1, "b": 2}, "apply_state_delta must not mutate the input state"


def test_apply_state_delta_null_deletes_key():
    state = {"a": 1, "b": 2}

    result = apply_state_delta(state, {"a": None})

    assert result == {"b": 2}


def test_apply_state_delta_merges_nested_objects_recursively():
    state = {"files": {"a.py": "old", "b.py": "keep"}}

    result = apply_state_delta(state, {"files": {"a.py": "new", "c.py": "added"}})

    assert result == {"files": {"a.py": "new", "b.py": "keep", "c.py": "added"}}


def test_apply_state_delta_nested_null_deletes_nested_key():
    state = {"files": {"a.py": "x", "b.py": "y"}}

    result = apply_state_delta(state, {"files": {"a.py": None}})

    assert result == {"files": {"b.py": "y"}}


def test_validate_state_delta_rejects_undeclared_fields():
    with pytest.raises(StateValidationError, match="unknown_field"):
        validate_state_delta({"unknown_field": 1}, allowed_fields=("a", "b"))


def test_validate_state_delta_allows_declared_fields():
    validate_state_delta({"a": 1}, allowed_fields=("a", "b"))


def test_validate_state_delta_skips_field_check_without_schema():
    validate_state_delta({"anything": 1}, allowed_fields=None)


def test_validate_state_delta_rejects_non_mapping():
    with pytest.raises(StateValidationError):
        validate_state_delta([1, 2, 3], allowed_fields=None)  # type: ignore[arg-type]
