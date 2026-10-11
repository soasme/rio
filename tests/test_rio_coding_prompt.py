"""Tests for thinking levels, project context formatting, and protected resources."""

from __future__ import annotations

import pytest

from rio.coding.system_prompt import ProjectContextFile, format_project_context
from rio.coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    normalize_thinking_level,
    normalize_thinking_levels,
    reasoning_effort_for_level,
)

# --- thinking.py ----------------------------------------------------------------


def test_normalize_thinking_level_accepts_supported_modes() -> None:
    assert normalize_thinking_level("HIGH") == "high"
    assert normalize_thinking_level(None) == DEFAULT_THINKING_LEVEL


def test_normalize_thinking_level_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unknown thinking mode"):
        normalize_thinking_level("maximum")


def test_thinking_levels_are_ordered() -> None:
    assert THINKING_LEVELS == ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def test_normalize_thinking_levels_rejects_empty_and_duplicates() -> None:
    assert normalize_thinking_levels(["OFF", "high"]) == ("off", "high")

    with pytest.raises(ValueError, match="non-empty"):
        normalize_thinking_levels([])

    with pytest.raises(ValueError, match="unique"):
        normalize_thinking_levels(["high", "HIGH"])


def test_reasoning_effort_maps_off_to_none() -> None:
    assert reasoning_effort_for_level("off") == "none"
    assert reasoning_effort_for_level("xhigh") == "xhigh"


# --- system_prompt.py --------------------------------------------------------


def test_format_project_context_escapes_path() -> None:
    text = format_project_context((ProjectContextFile(path="a&b/AGENTS.md", content="rules"),))

    assert '<project_instructions path="a&amp;b/AGENTS.md">' in text
    assert "rules" in text
    assert format_project_context(()) == ""
