"""Tests for project-context discovery, skills, prompt templates, thinking
levels, shell config, and skill-instruction assembly in `rio_coding`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from rio_ai.tools import AgentTool, AgentToolResult
from rio_coding.context import (
    discover_project_context,
    discover_project_context_with_diagnostics,
)
from rio_coding.paths import RioPaths
from rio_coding.prompt_templates import (
    PromptTemplate,
    expand_prompt_template_command,
    load_prompt_templates,
    load_prompt_templates_with_diagnostics,
    render_prompt_template,
)
from rio_coding.resources import ResourceError, RioResourcePaths
from rio_coding.skills import (
    Skill,
    build_skill_index,
    expand_skill_command,
    format_skill_invocation,
    load_skills,
    load_skills_with_diagnostics,
    parse_skill_invocation,
)
from rio_coding.system_prompt import (
    CODING_STATE_FIELD_DOCS,
    BuildSystemPromptOptions,
    ProjectContextFile,
    PromptSection,
    build_skill_instructions,
    build_system_prompt,
    collect_prompt_guidelines,
    format_available_tools,
    format_skills_for_prompt,
    format_state_field_docs,
    format_step_protocol,
)
from rio_coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    next_thinking_level,
    normalize_thinking_level,
    normalize_thinking_levels,
    reasoning_effort_for_level,
)


async def _unused_executor(
    tool_call_id: str,
    _arguments: object,
    signal: object | None = None,
    on_update: object | None = None,
) -> AgentToolResult:
    del tool_call_id, signal, on_update
    return AgentToolResult(content="")


def _read_tool() -> AgentTool:
    return AgentTool(
        name="read",
        label="Read",
        description="Read file contents",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        execute_fn=_unused_executor,  # type: ignore[arg-type]
        prompt_snippet="Read file contents",
        prompt_guidelines=("Use read to examine files instead of cat or sed.",),
    )


def _bash_tool() -> AgentTool:
    return AgentTool(
        name="bash",
        label="Bash",
        description="Run a shell command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        execute_fn=_unused_executor,  # type: ignore[arg-type]
        prompt_snippet="Run a shell command",
        prompt_guidelines=("When using bash, include a brief present-participle description.",),
    )


# --- context.py -------------------------------------------------------------


def test_discovers_user_project_and_agents_context_files(tmp_path: Path) -> None:
    rio_home = tmp_path / "home" / ".rio"
    agents_home = tmp_path / "home" / ".agents"
    project = tmp_path / "project"
    nested = project / "pkg"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    rio_home.mkdir(parents=True)
    agents_home.mkdir(parents=True)
    (project / ".rio").mkdir()
    (project / ".agents").mkdir()

    (rio_home / "AGENTS.md").write_text("User rio instructions", encoding="utf-8")
    (agents_home / "AGENTS.md").write_text("User agents instructions", encoding="utf-8")
    (project / "AGENTS.md").write_text("Project instructions", encoding="utf-8")
    (nested / "AGENTS.md").write_text("Nested instructions", encoding="utf-8")
    (nested / ".rio").mkdir()
    (nested / ".agents").mkdir()
    (nested / ".rio" / "AGENTS.md").write_text("Project rio instructions", encoding="utf-8")
    (nested / ".agents" / "AGENTS.md").write_text("Project agents instructions", encoding="utf-8")

    context_files = discover_project_context(
        RioResourcePaths(
            root=rio_home,
            agents_root=agents_home,
            cwd=nested,
            paths=RioPaths(home=rio_home, agents_home=agents_home),
        )
    )

    assert [Path(context_file.path) for context_file in context_files] == [
        rio_home / "AGENTS.md",
        agents_home / "AGENTS.md",
        project / "AGENTS.md",
        nested / "AGENTS.md",
        nested / ".rio" / "AGENTS.md",
        nested / ".agents" / "AGENTS.md",
    ]
    assert [context_file.content for context_file in context_files] == [
        "User rio instructions",
        "User agents instructions",
        "Project instructions",
        "Nested instructions",
        "Project rio instructions",
        "Project agents instructions",
    ]


def test_discover_project_context_with_diagnostics_reports_unreadable_file(
    tmp_path: Path,
) -> None:
    rio_home = tmp_path / ".rio"
    rio_home.mkdir()
    agents_file = rio_home / "AGENTS.md"
    agents_file.write_text("unreadable", encoding="utf-8")
    agents_file.chmod(0o000)

    try:
        context_files, diagnostics = discover_project_context_with_diagnostics(
            RioResourcePaths(root=rio_home, agents_root=None)
        )
    finally:
        agents_file.chmod(0o644)

    assert context_files == ()
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "context"
    assert "could not read context file" in diagnostics[0].message


# --- skills.py ----------------------------------------------------------------


def test_load_skills_does_not_include_self_knowledge(tmp_path: Path) -> None:
    skills = load_skills(RioResourcePaths(root=tmp_path, agents_root=None))

    assert skills == []


def test_load_skills_from_directory(tmp_path: Path) -> None:
    """Skills must live in ``<dir>/<name>/SKILL.md`` subdirectories."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "python-testing").mkdir(parents=True)
    (skills_dir / "python-testing" / "SKILL.md").write_text(
        "---\ndescription: Test Python code\n---\n# Python Testing\nUse pytest.",
        encoding="utf-8",
    )
    (skills_dir / "git-review").mkdir()
    (skills_dir / "git-review" / "SKILL.md").write_text(
        "# Git Review\nReview diffs.", encoding="utf-8"
    )

    skills = load_skills(RioResourcePaths(root=tmp_path, agents_root=None))

    skill_by_name = {skill.name: skill for skill in skills}
    assert set(skill_by_name) == {"git-review", "python-testing"}
    assert skill_by_name["git-review"].description == "Git Review"
    assert skill_by_name["python-testing"].description == "Test Python code"


def test_project_agents_skill_overrides_user_agents_skill(tmp_path: Path) -> None:
    rio_home = tmp_path / "home" / ".rio"
    agents_home = tmp_path / "home" / ".agents"
    cwd = tmp_path / "project"
    (agents_home / "skills" / "review").mkdir(parents=True)
    (agents_home / "skills" / "review" / "SKILL.md").write_text("# User Review", encoding="utf-8")
    (cwd / ".agents" / "skills" / "review").mkdir(parents=True)
    (cwd / ".agents" / "skills" / "review" / "SKILL.md").write_text(
        "# Project Review", encoding="utf-8"
    )

    skills = load_skills(RioResourcePaths(root=rio_home, agents_root=agents_home, cwd=cwd))

    review = next(skill for skill in skills if skill.name == "review")
    assert review.path == cwd / ".agents" / "skills" / "review" / "SKILL.md"
    assert review.description == "Project Review"


def test_load_skills_with_diagnostics_reports_bare_md_migration_hint(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "legacy.md").write_text("# Legacy Skill\nOld body.", encoding="utf-8")
    (skills_dir / "good").mkdir()
    (skills_dir / "good" / "SKILL.md").write_text("# Good Skill", encoding="utf-8")

    skills, diagnostics = load_skills_with_diagnostics(
        RioResourcePaths(root=tmp_path, agents_root=None)
    )

    assert "good" in {skill.name for skill in skills}
    migration_diagnostics = [d for d in diagnostics if d.severity == "info"]
    assert len(migration_diagnostics) == 1
    assert migration_diagnostics[0].name == "legacy"
    assert "bare .md files are no longer treated as skills" in migration_diagnostics[0].message


def test_expand_skill_command_includes_skill_and_user_request(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills" / "testing"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("# Testing\nRun pytest.", encoding="utf-8")
    skills = load_skills(RioResourcePaths(root=tmp_path, agents_root=None))

    expanded = expand_skill_command("/skill:testing add parser tests", skills)

    assert expanded is not None
    testing = next(skill for skill in skills if skill.name == "testing")
    assert f'<skill name="testing" location="{testing.path}">' in expanded
    assert expanded.endswith("</skill>\n\nadd parser tests")


def test_format_skill_invocation_without_extra_instructions(tmp_path: Path) -> None:
    skill = Skill(
        name="testing",
        path=tmp_path / "skills" / "testing" / "SKILL.md",
        content="# Testing\nRun pytest.",
        description="Test code",
    )

    formatted = format_skill_invocation(skill)

    assert formatted == (
        f'<skill name="testing" location="{skill.path}">\n'
        f"References are relative to {skill.path.parent}.\n\n"
        "# Testing\n"
        "Run pytest.\n"
        "</skill>"
    )


def test_parse_skill_invocation_extracts_display_metadata(tmp_path: Path) -> None:
    skill = Skill(
        name="testing",
        path=tmp_path / "skills" / "testing" / "SKILL.md",
        content="# Testing\nRun pytest.",
        description="Test Python code",
    )
    formatted = format_skill_invocation(skill, "add parser tests")

    parsed = parse_skill_invocation(formatted)

    assert parsed is not None
    assert parsed.name == "testing"
    assert parsed.additional_instructions == "add parser tests"


def test_expand_skill_command_rejects_unknown_skill() -> None:
    with pytest.raises(ResourceError, match="Unknown skill"):
        expand_skill_command("/skill:missing", [])


def test_build_skill_index_excludes_disabled_skills(tmp_path: Path) -> None:
    visible = Skill(
        name="visible",
        path=tmp_path / "visible" / "SKILL.md",
        content="Body",
        description="Visible skill",
    )
    hidden = Skill(
        name="hidden",
        path=tmp_path / "hidden" / "SKILL.md",
        content="Body",
        description="Hidden skill",
        disable_model_invocation=True,
    )

    index = build_skill_index([visible, hidden])

    assert "- visible: Visible skill" in index
    assert "hidden" not in index
    assert build_skill_index([hidden]) == "Available skills: none"


# --- prompt_templates.py -------------------------------------------------------


def test_load_prompt_templates_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert load_prompt_templates(RioResourcePaths(root=tmp_path, agents_root=None)) == []


def test_load_prompt_templates_from_markdown_files(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "review.md").write_text(
        "---\ndescription: Review code\n---\nReview {{ topic }}.",
        encoding="utf-8",
    )

    templates = load_prompt_templates(RioResourcePaths(root=tmp_path, agents_root=None))

    assert len(templates) == 1
    assert templates[0].name == "review"
    assert templates[0].description == "Review code"


def test_project_prompt_template_overrides_user_template(tmp_path: Path) -> None:
    rio_home = tmp_path / "home" / ".rio"
    agents_home = tmp_path / "home" / ".agents"
    cwd = tmp_path / "project"
    (agents_home / "prompts").mkdir(parents=True)
    (agents_home / "prompts" / "review.md").write_text("User review", encoding="utf-8")
    (cwd / ".agents" / "prompts").mkdir(parents=True)
    (cwd / ".agents" / "prompts" / "review.md").write_text("Project review", encoding="utf-8")

    templates = load_prompt_templates(
        RioResourcePaths(root=rio_home, agents_root=agents_home, cwd=cwd)
    )

    assert len(templates) == 1
    assert templates[0].path == cwd / ".agents" / "prompts" / "review.md"
    assert templates[0].content == "Project review"


def test_load_prompt_templates_with_diagnostics_reports_overrides(tmp_path: Path) -> None:
    rio_home = tmp_path / "home" / ".rio"
    agents_home = tmp_path / "home" / ".agents"
    cwd = tmp_path / "project"
    (rio_home / "prompts").mkdir(parents=True)
    (rio_home / "prompts" / "review.md").write_text("User rio review", encoding="utf-8")
    (cwd / ".rio" / "prompts").mkdir(parents=True)
    (cwd / ".rio" / "prompts" / "review.md").write_text("Project rio review", encoding="utf-8")

    templates, diagnostics = load_prompt_templates_with_diagnostics(
        RioResourcePaths(root=rio_home, agents_root=agents_home, cwd=cwd)
    )

    assert [template.name for template in templates] == ["review"]
    assert templates[0].path == cwd / ".rio" / "prompts" / "review.md"
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "prompt"
    assert "overrides lower-precedence resource" in diagnostics[0].message


@pytest.mark.parametrize("reserved_name", ["prompts", "skills", "tools"])
def test_reserved_picker_template_is_ignored_with_diagnostic(
    tmp_path: Path, reserved_name: str
) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    reserved_path = prompts_dir / f"{reserved_name.upper()}.md"
    reserved_path.write_text("Shadow the picker", encoding="utf-8")

    templates, diagnostics = load_prompt_templates_with_diagnostics(
        RioResourcePaths(root=tmp_path, agents_root=None)
    )

    assert templates == []
    assert len(diagnostics) == 1
    assert diagnostics[0].path == reserved_path
    assert f"reserved by the built-in /{reserved_name} command" in diagnostics[0].message


def test_render_prompt_template_replaces_variables() -> None:
    template = PromptTemplate(
        name="review",
        path=Path("review.md"),
        content="Review {{ topic }} for {{ focus }}.",
    )

    assert render_prompt_template(template, {"topic": "auth", "focus": "security"}) == (
        "Review auth for security."
    )


def test_render_prompt_template_rejects_missing_variables() -> None:
    template = PromptTemplate(name="review", path=Path("review.md"), content="Review {{ topic }}.")

    with pytest.raises(ResourceError, match="Missing prompt template variable"):
        render_prompt_template(template, {})


def test_expand_prompt_template_command_supports_pi_argument_variables() -> None:
    template = PromptTemplate(
        name="example",
        path=Path("example.md"),
        content="first=$1 second=$2 all=$@ named=$ARGUMENTS",
    )

    assert expand_prompt_template_command('/example one "two words"', [template]) == (
        "first=one second=two words all=one two words named=one two words"
    )


def test_expand_prompt_template_command_supports_defaults_and_slices() -> None:
    template = PromptTemplate(
        name="example",
        path=Path("example.md"),
        content="count=${1:-7} rest=${@:2} limited=${@:2:1} all=${@:-none}",
    )

    assert expand_prompt_template_command("/example", [template]) == (
        "count=7 rest= limited= all=none"
    )
    assert expand_prompt_template_command("/example one two three", [template]) == (
        "count=one rest=two three limited=two all=one two three"
    )


def test_expand_prompt_template_command_appends_arguments_without_placeholder() -> None:
    template = PromptTemplate(name="review", path=Path("review.md"), content="Review this code.")

    assert expand_prompt_template_command("/review src/app.py", [template]) == (
        "Review this code.\n\nsrc/app.py"
    )


def test_expand_prompt_template_command_ignores_unknown_commands() -> None:
    template = PromptTemplate(name="review", path=Path("review.md"), content="Review this code.")

    assert expand_prompt_template_command("/missing src/app.py", [template]) is None


# --- thinking.py ----------------------------------------------------------------


def test_normalize_thinking_level_accepts_supported_modes() -> None:
    assert normalize_thinking_level("HIGH") == "high"
    assert normalize_thinking_level(None) == DEFAULT_THINKING_LEVEL


def test_normalize_thinking_level_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unknown thinking mode"):
        normalize_thinking_level("maximum")


def test_next_thinking_level_cycles_supported_modes() -> None:
    assert next_thinking_level("medium") == "high"
    assert next_thinking_level("xhigh") == "max"
    assert next_thinking_level("max") == "off"
    assert next_thinking_level("missing", available=("low", "high")) == "low"
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


# --- shell_config.py --------------------------------------------------------
#
# NOTE: shell_config.py imports `TrustDefault` from `rio_coding.project_trust`,
# which is a sibling slice not yet ported at the time this file was written.
# Its tests are intentionally omitted here (see final report) rather than
# left broken; re-add them once `rio_coding.project_trust` exists, porting
# tau's tests/test_shell_config.py verbatim with the tau->rio renames.


# --- system_prompt.py --------------------------------------------------------


def test_default_prompt_includes_tools_guidelines_date_and_cwd(tmp_path: Path) -> None:
    tools = [_read_tool(), _bash_tool()]

    prompt = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=tmp_path,
            tools=tools,
            current_date=date(2026, 6, 17),
        )
    )

    assert "You are an expert coding assistant operating inside rio" in prompt
    assert "SKILL.state runtime" in prompt
    assert "Available tools:\n- read: Read file contents" in prompt
    assert "- Prefer grep/find/ls tools over bash" not in prompt
    assert "- Use read to examine files instead of cat or sed." in prompt
    assert "- When using bash, include a brief present-participle description." in prompt
    assert "- Inspect relevant files and project instructions before editing" in prompt
    assert "- Do not overwrite or discard unrelated user changes" in prompt
    assert "- Report checks honestly; never claim a command passed unless you ran it" in prompt
    assert prompt.endswith(f"Current date: 2026-06-17\nCurrent working directory: {tmp_path}")


def test_build_system_prompt_is_an_alias_for_build_skill_instructions(tmp_path: Path) -> None:
    assert build_system_prompt is build_skill_instructions


def test_tool_without_prompt_snippet_is_hidden_from_available_tools() -> None:
    tool = AgentTool(
        name="hidden",
        label="Hidden",
        description="Still sent to provider",
        parameters={"type": "object"},
        execute_fn=_unused_executor,  # type: ignore[arg-type]
    )

    assert format_available_tools([tool]) == "(none)"


def test_guidelines_are_deduplicated() -> None:
    tools = [_read_tool()]
    duplicate = tools[0].prompt_guidelines[0]

    guidelines = collect_prompt_guidelines(tools, [duplicate])

    assert guidelines.count(duplicate) == 1


def test_custom_prompt_replaces_default_but_keeps_append_context_and_date(tmp_path: Path) -> None:
    prompt = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=tmp_path,
            tools=[_read_tool(), _bash_tool()],
            custom_prompt="Custom base.",
            append_system_prompt="Extra rules.",
            context_files=(ProjectContextFile(path="/repo/AGENTS.md", content="Follow rules."),),
            current_date=date(2026, 6, 17),
        )
    )

    assert prompt.startswith("Custom base.\n\nExtra rules.")
    assert "Available tools:" not in prompt
    assert "Step protocol:" not in prompt
    assert "Execution state fields:" not in prompt
    assert '<project_instructions path="/repo/AGENTS.md">' in prompt
    assert "Follow rules." in prompt
    assert "Current date: 2026-06-17" in prompt


def test_extra_sections_follow_user_append_in_registration_order(tmp_path: Path) -> None:
    prompt = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=tmp_path,
            custom_prompt="Custom base.",
            append_system_prompt="User append.",
            extra_sections=(
                PromptSection(
                    title="Extension procedure", body="First step.\n\n```bash\nuv run pytest\n```"
                ),
                PromptSection(title=None, body="Untitled extension context."),
            ),
            context_files=(ProjectContextFile(path="/repo/AGENTS.md", content="Project rules."),),
            current_date=date(2026, 6, 17),
        )
    )

    expected = (
        "Custom base.\n\nUser append.\n\n## Extension procedure\n\n"
        "First step.\n\n```bash\nuv run pytest\n```\n\n"
        "Untitled extension context."
    )
    assert prompt.startswith(expected)
    assert prompt.index("User append.") < prompt.index("## Extension procedure")
    assert prompt.index("Untitled extension context.") < prompt.index("<project_instructions")


def test_empty_custom_prompt_is_still_custom(tmp_path: Path) -> None:
    prompt = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=tmp_path,
            tools=[_read_tool(), _bash_tool()],
            custom_prompt="",
            append_system_prompt="Extra rules.",
            current_date=date(2026, 6, 17),
        )
    )

    assert prompt.startswith("\n\nExtra rules.")
    assert "Available tools:" not in prompt
    assert "Current date: 2026-06-17" in prompt


def test_skills_are_formatted_as_xml_and_escaped(tmp_path: Path) -> None:
    skill_path = tmp_path / "skills" / "review" / "SKILL.md"
    skill = Skill(
        name="review&check",
        path=skill_path,
        content="ignored",
        description="Review <code>",
    )

    formatted = format_skills_for_prompt([skill])

    assert "<available_skills>" in formatted
    assert "<name>review&amp;check</name>" in formatted
    assert "<description>Review &lt;code&gt;</description>" in formatted
    assert f"<location>{skill_path}</location>" in formatted


def test_format_skills_for_prompt_excludes_disabled_skills(tmp_path: Path) -> None:
    visible = Skill(
        name="visible",
        path=tmp_path / "skills" / "visible" / "SKILL.md",
        content="",
        description="Visible skill",
    )
    hidden = Skill(
        name="hidden",
        path=tmp_path / "skills" / "hidden" / "SKILL.md",
        content="",
        description="Hidden skill",
        disable_model_invocation=True,
    )

    formatted = format_skills_for_prompt([visible, hidden])

    assert "<name>visible</name>" in formatted
    assert "hidden" not in formatted
    assert format_skills_for_prompt([hidden]) == ""


def test_skills_are_included_only_when_read_tool_is_available(tmp_path: Path) -> None:
    skill = Skill(name="testing", path=tmp_path / "testing.md", content="", description="Test")
    no_read_tool = AgentTool(
        name="custom",
        label="Custom",
        description="Custom",
        parameters={"type": "object"},
        execute_fn=_unused_executor,  # type: ignore[arg-type]
        prompt_snippet="Custom tool",
    )

    without_read = build_skill_instructions(
        BuildSystemPromptOptions(cwd=tmp_path, tools=[no_read_tool], skills=[skill])
    )
    with_read = build_skill_instructions(
        BuildSystemPromptOptions(cwd=tmp_path, tools=[_read_tool()], skills=[skill])
    )

    assert "<available_skills>" not in without_read
    assert "<available_skills>" in with_read


# --- new: step protocol and state field sections ----------------------------


def test_default_prompt_includes_step_protocol_section() -> None:
    prompt = build_skill_instructions(BuildSystemPromptOptions(cwd=Path("/repo")))

    assert "Step protocol:" in prompt
    assert "skill_step" in prompt
    assert "state_delta" in prompt
    assert "RFC 7396 JSON Merge Patch" in prompt
    assert "reasoning" in prompt
    assert "is discarded immediately" in prompt
    assert "`respond` action" in prompt


def test_default_prompt_includes_state_field_docs_section() -> None:
    prompt = build_skill_instructions(BuildSystemPromptOptions(cwd=Path("/repo")))

    assert "Execution state fields:" in prompt
    for field_name in CODING_STATE_FIELD_DOCS:
        assert f"`{field_name}`" in prompt


def test_format_step_protocol_describes_the_full_contract() -> None:
    text = format_step_protocol()

    assert "exactly one `skill_step` tool call" in text
    assert "`reasoning`, `state_delta`, and `action`" in text
    assert "discarded immediately" in text
    assert "null` deletes that key" in text
    assert "merges recursively" in text
    assert "never zero, never more than one" in text


def test_format_state_field_docs_lists_every_declared_field() -> None:
    text = format_state_field_docs()

    for field_name, description in CODING_STATE_FIELD_DOCS.items():
        assert f"- `{field_name}`: {description}" in text


def test_format_state_field_docs_accepts_a_custom_mapping() -> None:
    text = format_state_field_docs({"foo": "does foo things"})

    assert text == "Execution state fields:\n- `foo`: does foo things"


def test_coding_state_field_docs_covers_the_declared_schema() -> None:
    assert set(CODING_STATE_FIELD_DOCS) == {
        "goal",
        "plan",
        "findings",
        "files",
        "cwd",
        "environment",
        "blockers",
        "last_error",
        "scratch",
        "answer",
    }
