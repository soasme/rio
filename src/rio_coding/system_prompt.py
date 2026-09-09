"""Skill instruction assembly for rio coding skills.

Produces `P`, the immutable skill specification handed to
`rio_agent.HarnessSpec.instructions`. Unlike an append-only conversational
system prompt, `P` is authored once per domain and is the only fixed text the
model sees on every step; the mutable execution state and the single latest
observation carry everything else. This module therefore documents the
SKILL.state step protocol and the coding skill's declared state fields in
addition to the tool-usage guidance, project context, and skills sections
ported from the conversational design.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from rio_ai.tools import AgentTool
from rio_coding.skills import Skill

CODING_STATE_FIELD_DOCS: Mapping[str, str] = {
    "goal": (
        "What the user asked for, restated in your own words. Set it once, near the "
        "start of the turn, and keep it stable. A new user message is a new goal: "
        "replace it, and keep the rest of the state — the session has not restarted."
    ),
    "plan": (
        "An ordered checklist toward the goal: a list of objects with `id`, `title`, "
        "and `status` (`pending`, `in_progress`, `done`, or `blocked`). Update statuses "
        "as you make progress, and add or reorder steps as the plan changes. When a new "
        "user message sets a new goal, replace the finished plan with one for it."
    ),
    "findings": (
        "Durable conclusions you have reached, keyed by a short topic name. Use it for "
        "facts worth remembering across steps, such as where a piece of logic lives or "
        "why an approach was rejected."
    ),
    "files": (
        "A map from file path to what this run knows about that file. The runtime "
        "maintains most of it for you: after a successful read, write, or edit it sets "
        "`status` (`read`, `edited`, or `created`), `hash` (a short digest of the file "
        "on disk), and `context` — the file content it cached, as `total_lines` plus a "
        "`slices` map keyed by the line range each slice covers. Work from `context` "
        "instead of reading a file again; read a file only for a range that is not "
        "there, with `offset`, or when the content you need genuinely is not in it. "
        "`note` is yours: one line on why the file matters and what you concluded about "
        "it. Writing and editing require the recorded `hash` to still match the file on "
        "disk, so if you are told it changed, read the file again first. When the state "
        "runs out of room, forget a file: set its `context` to `null` and move anything "
        "you still need into its `note`. `status` and `note` survive forgetting."
    ),
    "cwd": "The current working directory for file and shell operations.",
    "environment": (
        "A snapshot of the run's environment: operating system, shell, git branch, and "
        "a short digest of the loaded project context. Refresh it when the environment "
        "changes; do not repeat this information elsewhere."
    ),
    "blockers": (
        "Unresolved obstacles preventing progress, such as missing permissions or "
        "failing tests you cannot fix yet. Remove an entry once it is resolved."
    ),
    "last_error": (
        "The error message from the most recently failed action, or `null` once you "
        "have addressed it or it no longer applies — a new user message makes it no "
        "longer apply. Read it before retrying a failed action."
    ),
    "scratch": (
        "Short-lived working notes that do not belong in any other field. Treat it as "
        "disposable; do not rely on it for anything that must survive many steps."
    ),
}


@dataclass(frozen=True, slots=True)
class ProjectContextFile:
    """A project instruction file included in the skill instructions."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class PromptSection:
    """A free-form section appended to the skill instructions."""

    title: str | None
    body: str


@dataclass(frozen=True, slots=True)
class BuildSystemPromptOptions:
    """Options used to build rio's skill instructions (`P`)."""

    cwd: Path
    tools: Sequence[AgentTool] = ()
    skills: Sequence[Skill] = ()
    custom_prompt: str | None = None
    append_system_prompt: str | None = None
    context_files: Sequence[ProjectContextFile] = ()
    current_date: date | None = None
    extra_guidelines: Sequence[str] = field(default_factory=tuple)
    extra_sections: Sequence[PromptSection] = field(default_factory=tuple)
    state_field_docs: Mapping[str, str] = field(default_factory=lambda: CODING_STATE_FIELD_DOCS)


def build_skill_instructions(options: BuildSystemPromptOptions) -> str:
    """Build the deterministic `P` skill instructions for a rio coding skill."""
    current_date = options.current_date or date.today()
    cwd = _format_path(options.cwd)
    append_parts = [options.append_system_prompt] if options.append_system_prompt else []
    append_parts.extend(format_prompt_section(section) for section in options.extra_sections)
    append_section = "".join(f"\n\n{part}" for part in append_parts)

    if options.custom_prompt is not None:
        prompt = options.custom_prompt
        prompt += append_section
        prompt += format_project_context(options.context_files)
        if _has_tool(options.tools, "read"):
            prompt += format_skills_for_prompt(options.skills)
        prompt += f"\nCurrent date: {current_date.isoformat()}"
        prompt += f"\nCurrent working directory: {cwd}"
        return prompt

    prompt = (
        "You are an expert coding assistant operating inside rio, a coding agent skill "
        "running on the SKILL.state runtime. You help users by reading files, "
        "executing commands, editing code, and writing new files."
        f"\n\nAvailable tools:\n{format_available_tools(options.tools)}"
        "\n\nIn addition to the tools above, you may have access to other custom tools "
        "depending on the project."
        f"\n\nGuidelines:\n{format_guidelines(options.tools, options.extra_guidelines)}"
        f"\n\n{format_step_protocol()}"
        f"\n\n{format_state_field_docs(options.state_field_docs)}"
    )

    prompt += append_section
    prompt += format_project_context(options.context_files)
    if _has_tool(options.tools, "read"):
        prompt += format_skills_for_prompt(options.skills)
    prompt += f"\nCurrent date: {current_date.isoformat()}"
    prompt += f"\nCurrent working directory: {cwd}"
    return prompt


# Ported call sites still refer to the conversational-era name.
build_system_prompt = build_skill_instructions


def format_prompt_section(section: PromptSection) -> str:
    """Render one optional-title free-form prompt section."""
    if section.title is None:
        return section.body
    return f"## {section.title}\n\n{section.body}"


def format_step_protocol() -> str:
    """Format the SKILL.state step protocol section.

    Every step, the model sees only these instructions, the current execution
    state, and the single latest observation — never earlier observations,
    earlier actions, or its own earlier reasoning. This section teaches that
    contract so the model knows what to persist and how.
    """
    return (
        "Step protocol:\n"
        "- Every step you receive exactly three things: these instructions, the "
        "current execution state as JSON, and the step's latest input. You never "
        "see earlier observations, earlier actions, or your own earlier reasoning — "
        "nothing survives between steps except what you write into the execution "
        "state.\n"
        "- That input is labelled. `Latest Observation` is what the action you took "
        "last step returned. `New User Message` is the person you are working with "
        "speaking: a new request, or a correction to the one in flight. The first "
        "step of a turn has only a message — no action has run yet.\n"
        "- A session runs many turns and the state survives between them, so a new "
        "message continues from what you already know: keep your findings, the files "
        "you have touched and the environment, and do not re-derive them.\n"
        "- You must reply with exactly one `skill_step` tool call carrying three "
        "fields: `reasoning`, `state_delta`, and `action`.\n"
        "- `reasoning` is private scratch space. It is discarded immediately after "
        "this step; nothing in it persists. Anything that must survive to a later "
        "step has to be written into `state_delta` instead.\n"
        "- `state_delta` is an RFC 7396 JSON Merge Patch applied to the execution "
        "state: setting a field to `null` deletes that key, an object value merges "
        "recursively into the existing object, and any other value replaces the "
        "field outright. You may only modify the declared state fields.\n"
        "- `action` is exactly one tool call — never zero, never more than one. To "
        "finish the turn, take the `respond` action: its `message` is your answer to "
        "the user, so there is nothing to write into the state first."
    )


def format_state_field_docs(field_docs: Mapping[str, str] = CODING_STATE_FIELD_DOCS) -> str:
    """Format the execution-state field reference section."""
    lines = ["Execution state fields:"]
    for name, description in field_docs.items():
        lines.append(f"- `{name}`: {description}")
    return "\n".join(lines)


def format_available_tools(tools: Sequence[AgentTool]) -> str:
    """Format visible tools using prompt snippets."""
    lines = [f"- {tool.name}: {tool.prompt_snippet}" for tool in tools if tool.prompt_snippet]
    return "\n".join(lines) if lines else "(none)"


def collect_prompt_guidelines(
    tools: Sequence[AgentTool], extra_guidelines: Sequence[str] = ()
) -> list[str]:
    """Collect and de-duplicate skill-instruction guidelines."""
    names = {tool.name for tool in tools}
    guidelines: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        normalized = value.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        guidelines.append(normalized)

    has_bash = "bash" in names
    has_exploration_tools = bool({"grep", "find", "ls"} & names)
    if has_bash and not has_exploration_tools:
        add("Use bash for file operations like ls, rg, find")
    elif has_bash and has_exploration_tools:
        add(
            "Prefer grep/find/ls tools over bash for file exploration (faster, respects .gitignore)"
        )

    for tool in tools:
        for guideline in tool.prompt_guidelines:
            add(guideline)
    for guideline in extra_guidelines:
        add(guideline)

    add("Inspect relevant files and project instructions before editing")
    add("Make focused changes that preserve the project's architecture and style")
    add("Do not overwrite or discard unrelated user changes")
    add("Use the project's documented commands and package manager")
    add("Run relevant tests, formatting, linting, and type checks after changes")
    add("Report checks honestly; never claim a command passed unless you ran it")
    add("Ask before destructive operations or materially ambiguous design choices")
    add("Be concise in your responses")
    add("Show file paths clearly when working with files")
    return guidelines


def format_guidelines(tools: Sequence[AgentTool], extra_guidelines: Sequence[str] = ()) -> str:
    """Format prompt guidelines as markdown bullets."""
    return "\n".join(
        f"- {guideline}" for guideline in collect_prompt_guidelines(tools, extra_guidelines)
    )


def format_project_context(context_files: Sequence[ProjectContextFile]) -> str:
    """Format project context files using an XML-like wrapper."""
    if not context_files:
        return ""

    lines = [
        "\n\n<project_context>",
        "",
        "Project-specific instructions and guidelines:",
        "",
    ]
    for context_file in context_files:
        lines.append(f'<project_instructions path="{escape(context_file.path)}">')
        lines.append(context_file.content)
        lines.append("</project_instructions>")
        lines.append("")
    lines.append("</project_context>")
    return "\n".join(lines)


def format_skills_for_prompt(skills: Sequence[Skill]) -> str:
    """Format skills for inclusion in the skill instructions using an XML style.

    Skills with ``disable_model_invocation`` set are excluded from the prompt;
    they remain invocable explicitly via ``/skill:<name>``.
    """
    visible_skills = [skill for skill in skills if not skill.disable_model_invocation]
    if not visible_skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Read the full skill file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]
    for skill in sorted(visible_skills, key=lambda item: item.name):
        description = skill.description or "No description"
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(description)}</description>",
                f"    <location>{escape(str(skill.path))}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _has_tool(tools: Sequence[AgentTool], name: str) -> bool:
    return any(tool.name == name for tool in tools)


def _format_path(path: Path) -> str:
    return str(path).replace("\\", "/")
