"""Tests for the slash-command registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rio_coding.commands import (
    CommandRegistry,
    CommandResult,
    SlashCommand,
    create_default_command_registry,
    format_reload_summary,
)
from rio_coding.reload import CodingReloadSummary, ReloadCategorySummary
from rio_coding.resources import ResourceDiagnostic
from rio_coding.session_manager import SessionManager
from rio_coding.step_footprint import StepFootprint


@dataclass
class FakeUsage:
    footprint: StepFootprint = field(
        default_factory=lambda: StepFootprint(
            instructions_tokens=800,
            state_tokens=120,
            observation_tokens=60,
            tools_tokens=200,
        )
    )
    context_window_tokens: int = 200_000

    @property
    def utilization(self) -> float:
        return self.footprint.total_tokens / self.context_window_tokens

    def projected_tokens(self, steps: int) -> int:
        return self.footprint.total_tokens * steps


@dataclass
class FakeSession:
    """A stand-in satisfying the `CommandSession` protocol."""

    cwd: Path = Path("/repo")
    model: str = "test-model"
    provider_name: str = "test-provider"
    available_models: tuple[str, ...] = ("test-model", "other-model")
    available_providers: tuple[str, ...] = ("test-provider",)
    tools: tuple = ()
    skills: tuple = ()
    prompt_templates: tuple = ()
    context_files: tuple = ()
    state: dict = field(
        default_factory=lambda: {
            "goal": "fix the parser",
            "plan": [{"id": "1", "title": "read", "status": "done"}],
            "findings": {},
            "answer": None,
        }
    )
    context_window_tokens: int = 200_000
    thinking_level: str = "medium"
    available_thinking_levels: tuple[str, ...] = ("off", "medium", "high")
    resource_diagnostics: tuple = ()
    system_prompt: str = "SKILL SPECIFICATION"
    session_id: str | None = "session-1"
    session_title: str | None = "A session"
    session_manager: SessionManager | None = None
    context_usage: FakeUsage = field(default_factory=FakeUsage)
    indexed: bool = False
    set_models: list = field(default_factory=list)
    provider_reload_error: str | None = None

    def ensure_session_indexed(self) -> None:
        self.indexed = True

    def set_model(self, model: str) -> None:
        self.set_models.append(model)
        self.model = model

    def reload_provider_settings(self) -> None:
        if self.provider_reload_error is not None:
            raise ValueError(self.provider_reload_error)


@pytest.fixture
def registry() -> CommandRegistry:
    return create_default_command_registry()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


class TestParsing:
    def test_plain_text_is_not_a_command(self, registry, session) -> None:
        assert registry.execute(session, "fix the parser").handled is False

    def test_unknown_commands_are_unhandled(self, registry, session) -> None:
        assert registry.execute(session, "/nope").handled is False

    def test_skill_invocations_are_left_to_the_session(self, registry, session) -> None:
        assert registry.execute(session, "/skill:review the diff").handled is False

    def test_names_are_case_insensitive(self, registry, session) -> None:
        assert registry.execute(session, "/QUIT").exit_requested is True

    def test_aliases_resolve(self, registry, session) -> None:
        assert registry.execute(session, "/exit").exit_requested is True

    def test_arguments_are_split_off_and_trimmed(self, registry, session) -> None:
        assert registry.execute(session, "/state   goal  ").message.startswith("goal:")

    def test_two_word_scoped_models_form(self, registry, session) -> None:
        assert registry.execute(session, "/scoped models").scoped_models_picker_requested is True


class TestRegistration:
    def test_duplicate_names_are_rejected(self) -> None:
        registry = CommandRegistry()
        command = SlashCommand(
            name="dup", usage="/dup", description="", handler=lambda _c: CommandResult(True)
        )
        registry.register(command)
        with pytest.raises(ValueError, match="Duplicate slash command"):
            registry.register(command)

    def test_duplicate_aliases_are_rejected(self) -> None:
        registry = CommandRegistry()
        handler = lambda _c: CommandResult(True)  # noqa: E731
        registry.register(
            SlashCommand(name="a", usage="", description="", handler=handler, aliases=("x",))
        )
        with pytest.raises(ValueError, match="Duplicate slash command alias"):
            registry.register(
                SlashCommand(name="b", usage="", description="", handler=handler, aliases=("x",))
            )

    def test_commands_are_listed_sorted(self, registry) -> None:
        names = [command.name for command in registry.list_commands()]
        assert names == sorted(names)

    def test_get_resolves_names_and_aliases(self, registry) -> None:
        assert registry.get("quit") is registry.get("exit")
        assert registry.get("missing") is None


class TestNoCompaction:
    def test_there_is_no_compact_command(self, registry, session) -> None:
        """Compaction summarized a growing transcript; rio's prompt is fixed."""
        assert registry.get("compact") is None
        assert registry.execute(session, "/compact").handled is False

    def test_the_result_type_has_no_compaction_field(self) -> None:
        assert not hasattr(CommandResult(handled=True), "compact_summary")


class TestStateCommand:
    def test_shows_the_whole_execution_state(self, registry, session) -> None:
        message = registry.execute(session, "/state").message
        assert "Execution state:" in message
        assert "fix the parser" in message

    def test_shows_a_single_field(self, registry, session) -> None:
        message = registry.execute(session, "/state goal").message
        assert message.startswith("goal:")
        assert "fix the parser" in message
        assert "plan" not in message

    def test_rejects_an_undeclared_field(self, registry, session) -> None:
        message = registry.execute(session, "/state nonsense").message
        assert "Unknown state field" in message
        assert "goal" in message

    def test_reports_an_empty_state(self, registry) -> None:
        message = create_default_command_registry().execute(FakeSession(state={}), "/state").message
        assert message == "Execution state is empty."


class TestStatus:
    def test_reports_a_per_step_footprint_not_a_running_total(self, registry, session) -> None:
        message = registry.execute(session, "/session").message
        assert "Per-step prompt: 1180 tokens" in message
        assert "constant across the run" in message
        assert "Projected over 100 steps: 118,000 tokens (linear)" in message

    def test_reports_the_basics(self, registry, session) -> None:
        message = registry.execute(session, "/session").message
        assert "Model: test-model" in message
        assert "Provider: test-provider" in message
        assert "Session: session-1" in message
        assert "Session name: A session" in message

    def test_counts_actions_not_tool_calls(self, registry, session) -> None:
        assert "Actions: 0" in registry.execute(session, "/session").message

    def test_thinking_status_appears(self, registry, session) -> None:
        assert "Thinking mode: medium" in registry.execute(session, "/session").message

    def test_thinking_unavailable_is_explained(self, registry) -> None:
        session = FakeSession(available_thinking_levels=())
        session.thinking_unavailable_reason = "model does not support it"
        message = create_default_command_registry().execute(session, "/session").message
        assert "Thinking mode: unavailable" in message
        assert "model does not support it" in message


class TestSimpleCommands:
    def test_help_lists_every_command(self, registry, session) -> None:
        message = registry.execute(session, "/help").message
        for command in registry.list_commands():
            assert command.usage in message

    def test_system_prints_the_skill_instructions(self, registry, session) -> None:
        assert registry.execute(session, "/system").message == "SKILL SPECIFICATION"

    def test_system_rejects_arguments(self, registry, session) -> None:
        assert registry.execute(session, "/system extra").message == "Usage: /system"

    def test_new_requests_a_new_session(self, registry, session) -> None:
        assert registry.execute(session, "/new").new_session_requested is True

    def test_reload_defers_to_the_async_path(self, registry, session) -> None:
        assert registry.execute(session, "/reload").reload_requested is True

    def test_tree_opens_the_checkpoint_picker(self, registry, session) -> None:
        assert registry.execute(session, "/tree").tree_picker_requested is True

    def test_tree_is_described_as_branching_from_a_checkpoint(self, registry) -> None:
        command = registry.get("tree")
        assert "checkpoint" in command.description
        assert "checkpoint" in command.search_terms

    def test_context_reports_no_files(self, registry, session) -> None:
        assert "No project context files loaded." in registry.execute(session, "/context").message

    def test_resources_reports_diagnostics(self, registry) -> None:
        session = FakeSession(
            resource_diagnostics=(
                ResourceDiagnostic(kind="skill", message="duplicate name", name="review"),
            )
        )
        message = create_default_command_registry().execute(session, "/resources").message
        assert "duplicate name" in message


class TestExport:
    def test_bare_export(self, registry, session) -> None:
        result = registry.execute(session, "/export")
        assert result.export_requested is True
        assert result.export_format is None
        assert result.export_destination is None

    def test_format_flag_forms(self, registry, session) -> None:
        assert registry.execute(session, "/export --format html").export_format == "html"
        assert registry.execute(session, "/export --format=jsonl").export_format == "jsonl"

    def test_destination(self, registry, session) -> None:
        result = registry.execute(session, "/export --format html out.html")
        assert result.export_destination == Path("out.html")

    def test_missing_format_value_is_an_error(self, registry, session) -> None:
        assert "Usage:" in registry.execute(session, "/export --format").message

    def test_unknown_option_is_an_error(self, registry, session) -> None:
        assert "Unknown export option" in registry.execute(session, "/export --nope").message

    def test_two_destinations_is_an_error(self, registry, session) -> None:
        assert "Usage:" in registry.execute(session, "/export one two").message


class TestModel:
    def test_bare_model_opens_the_picker(self, registry, session) -> None:
        assert registry.execute(session, "/model").model_picker_requested is True

    def test_named_model_is_selected(self, registry, session) -> None:
        result = registry.execute(session, "/model other-model")
        assert session.set_models == ["other-model"]
        assert "Current model: other-model" in result.message

    def test_unknown_model_is_rejected(self, registry, session) -> None:
        message = registry.execute(session, "/model nope").message
        assert "Unknown model" in message
        assert "other-model" in message

    def test_a_provider_refresh_failure_is_reported(self, registry) -> None:
        session = FakeSession(provider_reload_error="settings file is corrupt")
        message = create_default_command_registry().execute(session, "/model").message
        assert "Could not refresh provider settings" in message
        assert "settings file is corrupt" in message


class TestThinking:
    def test_bare_thinking_shows_status_and_modes(self, registry, session) -> None:
        message = registry.execute(session, "/thinking").message
        assert "Thinking mode: medium" in message
        assert "off, medium, high" in message

    def test_setting_a_mode(self, registry, session) -> None:
        assert registry.execute(session, "/thinking high").thinking_level == "high"

    def test_unsupported_mode_is_rejected(self, registry, session) -> None:
        message = registry.execute(session, "/thinking off_the_charts").message
        assert "not available" in message or "Unknown" in message

    def test_unavailable_thinking_is_explained(self, registry) -> None:
        session = FakeSession(available_thinking_levels=())
        message = create_default_command_registry().execute(session, "/thinking high").message
        assert "unavailable" in message


class TestLogin:
    def test_bare_login_opens_the_picker(self, registry, session) -> None:
        assert registry.execute(session, "/login").login_picker_requested is True

    def test_custom_provider(self, registry, session) -> None:
        assert registry.execute(session, "/login custom").custom_provider_login_requested is True

    def test_alias_selects_a_method(self, registry, session) -> None:
        result = registry.execute(session, "/login anthropic-subscription")
        assert result.login_provider == "anthropic"
        assert result.login_method == "subscription"

    def test_unknown_provider_is_rejected(self, registry, session) -> None:
        assert "Unknown login provider" in registry.execute(session, "/login nope").message

    def test_bare_logout_opens_the_picker(self, registry, session) -> None:
        assert registry.execute(session, "/logout").logout_picker_requested is True

    def test_unknown_logout_provider_is_rejected(self, registry, session) -> None:
        assert "Unknown logout provider" in registry.execute(session, "/logout nope").message


class TestReloadSummary:
    def test_says_the_instructions_apply_from_the_next_step(self) -> None:
        category = ReloadCategorySummary(before=1, after=2, changed=True)
        summary = CodingReloadSummary(
            skills=category,
            prompt_templates=category,
            context_files=category,
            extensions=category,
            diagnostics=category,
            system_prompt_rebuilt=True,
        )
        message = format_reload_summary(summary)
        assert "Next-step skill instructions: rebuilt" in message
        assert "2 total (changed, +1)" in message

    def test_unchanged_categories_omit_a_delta(self) -> None:
        category = ReloadCategorySummary(before=3, after=3, changed=False)
        summary = CodingReloadSummary(
            skills=category,
            prompt_templates=category,
            context_files=category,
            extensions=category,
            diagnostics=category,
            system_prompt_rebuilt=False,
        )
        message = format_reload_summary(summary)
        assert "3 total (unchanged)" in message
        assert "Next-step skill instructions: unchanged" in message


class TestStatePanel:
    def test_requests_a_toggle(self, registry, session) -> None:
        result = registry.execute(session, "/state-panel")
        assert result.handled
        assert result.state_panel_toggle_requested

    def test_sidebar_alias_requests_a_toggle(self, registry, session) -> None:
        assert registry.execute(session, "/sidebar").state_panel_toggle_requested

    def test_rejects_arguments(self, registry, session) -> None:
        result = registry.execute(session, "/state-panel on")
        assert not result.state_panel_toggle_requested
        assert result.message == "Usage: /state-panel"

    def test_is_listed_in_help(self, registry, session) -> None:
        message = registry.execute(session, "/help").message
        assert "/state-panel\tShow or hide the execution-state panel." in message
