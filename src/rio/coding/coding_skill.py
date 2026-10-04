"""The coding domain expressed as a notebook skill: fixed instructions plus an executor.

The instructions are built in `rio.coding.system_prompt`. Everything that
varies between steps lives in the notebook, which the model manages itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rio.agent import HarnessSpec, NotebookExecutor


@dataclass(frozen=True, slots=True)
class CodingSkillOptions:
    """Inputs that vary per session but not per step."""

    instructions: str
    executor: NotebookExecutor
    name: str = "rio-coding"
    #: Where removed cells are saved; see `HarnessSpec.archive`.
    archive: Path | None = None


def build_coding_skill(options: CodingSkillOptions) -> HarnessSpec:
    """Return the `HarnessSpec` that drives a coding session."""
    return HarnessSpec(
        name=options.name,
        instructions=options.instructions,
        executor=options.executor,
        archive=options.archive,
    )
