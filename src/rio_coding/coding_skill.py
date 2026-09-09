"""The coding domain expressed as a SKILL.state skill.

A skill is authored once per domain, not once per task. Everything that varies
between tasks lives in the execution state, which is why the same three fixed
inputs -- instructions, state, latest observation -- can drive an arbitrarily
long run without the prompt growing.

This module owns the coding domain's half of that contract: which state fields
exist, what belongs in each, and which actions the model may take. The
instructions themselves are built in `rio_coding.system_prompt`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rio_agent import HarnessSpec
from rio_ai.tools import AgentTool
from rio_ai.types import JSONObject, JSONValue
from rio_coding.file_context import FileContext
from rio_coding.step_footprint import CHARS_PER_TOKEN, DEFAULT_CONTEXT_WINDOW_TOKENS

#: The coding skill's declared state schema, in the order it is documented to
#: the model. A delta touching anything outside this tuple is rejected by the
#: runtime before it can reach the state.
CODING_STATE_FIELDS: tuple[str, ...] = (
    "goal",
    "plan",
    "findings",
    "files",
    "cwd",
    "environment",
    "blockers",
    "last_error",
    "scratch",
)

#: Statuses a plan item may carry. The plan is state, not a tool: a to-do list
#: that lives in the transcript has to be restated every turn, whereas one that
#: lives in the state is simply there.
PLAN_STATUSES: tuple[str, ...] = ("pending", "in_progress", "done", "blocked")

RESPOND_ACTION = "respond"


@dataclass(frozen=True, slots=True)
class CodingSkillOptions:
    """Inputs that vary per session but not per step."""

    instructions: str
    cwd: Path
    tools: tuple[AgentTool, ...] = ()
    name: str = "rio-coding"
    environment: JSONObject = field(default_factory=dict)
    goal: str | None = None
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS


def initial_coding_state(
    *,
    cwd: Path | str,
    environment: Mapping[str, JSONValue] | None = None,
    goal: str | None = None,
) -> dict[str, JSONValue]:
    """Return the execution state a fresh coding session starts from.

    Every declared field is seeded with an empty value of the right type.
    Leaving a field absent instead would invite the model to guess its shape,
    and schema type coercion is a documented failure mode for smaller models.
    """
    return {
        "goal": goal or "",
        "plan": [],
        "findings": {},
        "files": {},
        "cwd": str(cwd),
        "environment": dict(environment or {}),
        "blockers": [],
        "last_error": None,
        "scratch": {},
    }


def build_coding_skill(options: CodingSkillOptions) -> HarnessSpec:
    """Return the `HarnessSpec` that drives a coding session."""
    # ponytail: approximate token sizing; use provider token counts if this proves too loose.
    budget = int(options.context_window_tokens * CHARS_PER_TOKEN * 0.4)
    return HarnessSpec(
        name=options.name,
        instructions=options.instructions,
        state_fields=CODING_STATE_FIELDS,
        initial_state=initial_coding_state(
            cwd=options.cwd,
            environment=options.environment,
            goal=options.goal,
        ),
        actions=tuple(options.tools),
        state_budget_chars=budget,
        execute_action=FileContext(cwd=Path(options.cwd), state_budget_chars=budget).execute,
    )


def plan_progress(state: Mapping[str, JSONValue]) -> tuple[int, int]:
    """Return `(completed, total)` plan items, for progress display."""
    plan = state.get("plan")
    if not isinstance(plan, list):
        return (0, 0)
    total = len(plan)
    done = sum(1 for item in plan if isinstance(item, Mapping) and item.get("status") == "done")
    return (done, total)


def touched_files(state: Mapping[str, JSONValue]) -> list[str]:
    """Return the paths the run has read, created, or edited."""
    files = state.get("files")
    if not isinstance(files, Mapping):
        return []
    return sorted(str(path) for path in files)


def describe_state(state: Mapping[str, JSONValue]) -> str:
    """Return a one-line summary of an execution state, for logs and status bars."""
    done, total = plan_progress(state)
    parts = [f"plan {done}/{total}"]
    findings = state.get("findings")
    if isinstance(findings, Mapping) and findings:
        parts.append(f"{len(findings)} findings")
    files = touched_files(state)
    if files:
        parts.append(f"{len(files)} files")
    blockers = state.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, str) and blockers:
        parts.append(f"{len(blockers)} blockers")
    return ", ".join(parts)
