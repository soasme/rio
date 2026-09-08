"""Tests for `CodingSession`, the coding-agent environment.

Most of these check ordinary session behaviour -- resource discovery, journaling,
reconfiguration. The ones worth reading closely are the ones that would be hard
or impossible for a transcript-based session: the prompt footprint is identical
on step 1 and step 500, a model swap needs no history translation, and reloading
resources mid-session takes effect immediately because nothing was ever written
against the old instructions.
"""

from __future__ import annotations

import pytest

from conftest import step_response
from rio_ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio_coding.events import EntryAppendedEvent, SessionRunEndEvent
from rio_coding.session import CodingSession, CodingSessionConfig
from rio_coding.session_store import (
    InMemorySessionStorage,
    LabelEntry,
    ModelChangeEntry,
    SessionInfoEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
)


async def _read(tool_call_id, arguments, signal=None, on_update=None):
    path = arguments.get("path", "")
    return AgentToolResult(content=[TextContent(text=f"read {path}\ncontents of {path}")])


async def _respond(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(
        content=[TextContent(text=str(arguments.get("message", "")))], terminate=True
    )


READ = AgentTool(
    name="read",
    label="Read",
    description="Read a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    execute_fn=_read,
)
RESPOND = AgentTool(
    name="respond",
    label="Respond",
    description="Answer the user and end the turn.",
    parameters={"type": "object", "properties": {"message": {"type": "string"}}},
    execute_fn=_respond,
)


def two_step_streams():
    return [
        step_response(
            reasoning="Check the entry point.",
            state_delta={
                "goal": "explain main.py",
                "files": {"main.py": {"status": "read", "note": "entry point"}},
                "findings": {"entrypoint": "main.py"},
            },
            action="read",
            args={"path": "main.py"},
        ),
        step_response(
            reasoning="Ready to answer.",
            state_delta={"plan": [{"id": "1", "title": "explain", "status": "done"}]},
            action="respond",
            args={"message": "main.py starts the server."},
        ),
    ]


@pytest.fixture
def project(tmp_path):
    """A project directory with an AGENTS.md and an isolated rio home."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Always run the linter before finishing.\n")
    home = tmp_path / "home"
    (home / ".rio").mkdir(parents=True)
    return repo, home


async def make_session(project, streams, *, storage=None, monkeypatch=None, **overrides):
    repo, home = project
    from rio_coding.paths import RioPaths
    from rio_coding.resources import RioResourcePaths

    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    config = CodingSessionConfig(
        provider=FakeProvider(streams),
        model="test-model",
        cwd=repo,
        paths=paths,
        resource_paths=RioResourcePaths(
            root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
        ),
        storage=storage if storage is not None else InMemorySessionStorage(),
        tools=(READ, RESPOND),
        **overrides,
    )
    return await CodingSession.load(config)


async def collect(session, text):
    return [event async for event in session.prompt(text)]


class TestLoad:
    async def test_discovers_project_context(self, project) -> None:
        session = await make_session(project, [])
        bodies = [f.content for f in session.context_files]
        assert any("Always run the linter" in body for body in bodies)

    async def test_project_context_reaches_the_skill_instructions(self, project) -> None:
        session = await make_session(project, [])
        assert "Always run the linter" in session.system_prompt

    async def test_untrusted_projects_do_not_contribute_resources(self, project) -> None:
        session = await make_session(project, [], project_resources_trusted=False)
        assert "Always run the linter" not in session.system_prompt

    async def test_declared_actions_are_the_session_tools(self, project) -> None:
        session = await make_session(project, [])
        assert [tool.name for tool in session.tools] == ["read", "respond"]
        assert session.skill.actions == session.tools

    async def test_state_starts_seeded_with_every_declared_field(self, project) -> None:
        session = await make_session(project, [])
        state = session.state
        for name in session.skill.state_fields:
            assert name in state, f"{name} was not seeded"
        assert state["cwd"] == str(session.cwd)

    async def test_session_info_is_journaled_once(self, project) -> None:
        storage = InMemorySessionStorage()
        await make_session(project, [], storage=storage)
        await make_session(project, [], storage=storage)
        infos = [e for e in await storage.read_all() if isinstance(e, SessionInfoEntry)]
        assert len(infos) == 1


class TestPrompting:
    async def test_a_prompt_runs_to_an_answer(self, project) -> None:
        session = await make_session(project, two_step_streams())
        events = await collect(session, "explain main.py")

        run_end = next(e for e in events if isinstance(e, SessionRunEndEvent))
        assert run_end.answer == "main.py starts the server."
        assert session.answer == "main.py starts the server."

    async def test_state_accessors_reflect_the_run(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await collect(session, "explain main.py")

        assert session.plan_progress == (1, 1)
        assert session.touched_files == ["main.py"]
        assert "plan 1/1" in session.state_summary

    async def test_the_user_message_is_a_prompt_section_not_a_transcript(self, project) -> None:
        """There is nowhere to append it to. It is a labelled section of the one message."""
        session = await make_session(project, two_step_streams())
        provider = session.provider
        await collect(session, "explain main.py")

        _model, _system, first_messages, _tools = provider.calls[0]
        assert len(first_messages) == 1
        assert "New User Message:\nexplain main.py" in first_messages[0].content
        assert "Latest Observation:" not in first_messages[0].content

    async def test_the_run_is_journaled_as_steps(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        events = await collect(session, "explain main.py")

        steps = [e for e in await storage.read_all() if isinstance(e, StepEntry)]
        assert [s.action.name for s in steps] == ["read", "respond"]
        assert any(isinstance(e, EntryAppendedEvent) for e in events)

    async def test_continue_runs_a_queued_follow_up(self, project) -> None:
        streams = [
            *two_step_streams(),
            step_response(
                reasoning="",
                state_delta={},
                action="respond",
                args={"message": "tests pass"},
            ),
        ]
        session = await make_session(project, streams)
        await collect(session, "explain main.py")
        session.queue_follow_up_message("now run the tests")

        events = [event async for event in session.continue_()]
        run_end = next(e for e in events if isinstance(e, SessionRunEndEvent))
        assert run_end.answer == "tests pass"

    async def test_continue_with_nothing_queued_yields_nothing(self, project) -> None:
        session = await make_session(project, [])
        assert [event async for event in session.continue_()] == []


def write_skill(root, name: str, body: str = "Body") -> None:
    """Write `<root>/skills/<name>/SKILL.md`, creating the directories."""
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")


def write_prompt_template(root, name: str, body: str = "Template body") -> None:
    """Write `<root>/prompts/<name>.md`, creating the directories."""
    directory = root / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


class TestSlashInvocation:
    """`/<name>` resolution: built-in command, then prompt template, then skill."""

    async def test_a_bare_slash_name_runs_the_skill(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "review", "# Review\nRead the diff first.")
        session = await make_session(project, [])

        expanded = session.expand_prompt_text("/review")

        assert '<skill name="review"' in expanded
        assert "Read the diff first." in expanded

    async def test_the_rest_of_the_line_becomes_additional_instructions(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "review", "# Review")
        session = await make_session(project, [])

        assert session.expand_prompt_text("/review do the thing").endswith(
            "</skill>\n\ndo the thing"
        )

    async def test_the_explicit_prefix_still_works(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "review", "# Review")
        session = await make_session(project, [])

        assert session.expand_prompt_text("/skill:review do the thing") == (
            session.expand_prompt_text("/review do the thing")
        )

    async def test_every_skill_directory_is_reachable_by_name(self, project) -> None:
        repo, home = project
        write_skill(home / ".rio", "user", "# User skill")
        write_skill(home / ".agents", "agents", "# Agents skill")
        write_skill(repo / ".rio", "project", "# Project skill")
        write_skill(repo / ".agents", "project-agents", "# Project agents skill")
        session = await make_session(project, [])

        for name in ("user", "agents", "project", "project-agents"):
            assert f'<skill name="{name}"' in session.expand_prompt_text(f"/{name}")

    async def test_the_highest_precedence_skill_directory_wins(self, project) -> None:
        repo, home = project
        write_skill(home / ".rio", "review", "# User review")
        write_skill(repo / ".agents", "review", "# Project review")
        session = await make_session(project, [])

        expanded = session.expand_prompt_text("/review")

        assert "Project review" in expanded
        assert "User review" not in expanded

    async def test_a_prompt_template_wins_over_a_skill(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "review", "# Review skill")
        write_prompt_template(repo / ".rio", "review", "Review the diff.")
        session = await make_session(project, [])

        expanded = session.expand_prompt_text("/review")

        assert expanded.strip() == "Review the diff."
        assert '<skill name="review"' in session.expand_prompt_text("/skill:review")

    async def test_a_builtin_command_wins_over_both(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "model", "# Model skill")
        write_prompt_template(repo / ".rio", "model", "Model template.")
        session = await make_session(project, [])

        assert session.expand_prompt_text("/model gpt") == "/model gpt"

    async def test_shadowed_skills_are_reported_at_load(self, project) -> None:
        repo, _home = project
        write_skill(repo / ".rio", "model", "# Model skill")
        write_skill(repo / ".rio", "review", "# Review skill")
        write_skill(repo / ".rio", "testing", "# Testing skill")
        write_prompt_template(repo / ".rio", "review", "Review the diff.")
        session = await make_session(project, [])

        shadowed = {
            d.name: d.message for d in session.resource_diagnostics if "is taken by" in d.message
        }

        assert "testing" not in shadowed
        assert "the built-in /model command" in shadowed["model"]
        assert "invoke it as /skill:model" in shadowed["model"]
        assert "the prompt template at" in shadowed["review"]

    async def test_a_name_that_matches_nothing_reaches_the_model(self, project) -> None:
        session = await make_session(project, [])

        assert session.expand_prompt_text("/nope go") == "/nope go"
        assert session.expand_prompt_text("//review") == "//review"

    async def test_reload_picks_up_a_newly_added_skill(self, project) -> None:
        repo, _home = project
        session = await make_session(project, [])
        assert session.expand_prompt_text("/review") == "/review"

        write_skill(repo / ".rio", "review", "# Review")
        await session.reload()

        assert '<skill name="review"' in session.expand_prompt_text("/review")


class TestBoundedFootprint:
    async def test_the_step_footprint_is_reported_before_any_run(self, project) -> None:
        session = await make_session(project, [])
        footprint = session.step_footprint
        assert footprint.instructions_tokens > 0
        assert footprint.total_tokens == (
            footprint.instructions_tokens
            + footprint.state_tokens
            + footprint.observation_tokens
            + footprint.tools_tokens
        )

    async def test_context_usage_does_not_creep_toward_the_window(self, project) -> None:
        """tau's equivalent was a countdown. This one is a constant."""
        session = await make_session(project, two_step_streams())
        before = session.context_usage.utilization
        await collect(session, "explain main.py")
        after = session.context_usage.utilization

        assert 0.0 < before < 1.0
        # The state grew by a handful of fields, not by a turn of transcript.
        assert abs(after - before) < 0.05

    async def test_projected_cost_is_linear_in_steps(self, project) -> None:
        session = await make_session(project, [])
        usage = session.context_usage
        assert usage.projected_tokens(200) == 2 * usage.projected_tokens(100)


class TestReconfiguration:
    async def test_switching_models_is_journaled_and_needs_no_translation(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        await collect(session, "explain main.py")

        await session.set_model("another-model", provider_name="another-provider")
        assert session.model == "another-model"
        changes = [e for e in await storage.read_all() if isinstance(e, ModelChangeEntry)]
        assert changes[-1].model == "another-model"
        # The findings survive the swap because they live in the state.
        assert session.state["findings"] == {"entrypoint": "main.py"}

    async def test_a_new_provider_starts_from_the_existing_state(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await collect(session, "explain main.py")

        replacement = FakeProvider(
            [
                step_response(
                    reasoning="",
                    state_delta={},
                    action="respond",
                    args={"message": "still main.py"},
                )
            ]
        )
        await session.set_provider(replacement, name="other", model="other-model")
        await collect(session, "are you sure")

        _model, _system, messages, _tools = replacement.calls[0]
        assert "entrypoint" in messages[0].content

    async def test_thinking_level_changes_are_journaled(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, [], storage=storage)
        event = await session.set_thinking_level("high")
        assert event.level == "high"
        assert session.thinking_level == "high"
        entries = [e for e in await storage.read_all() if isinstance(e, ThinkingLevelChangeEntry)]
        assert entries[-1].thinking_level == "high"

    async def test_reload_picks_up_edited_project_instructions(self, project) -> None:
        """`P` is rebuilt per step, so a reload takes effect on the next one."""
        repo, _home = project
        session = await make_session(project, [])
        assert "Always run the linter" in session.system_prompt

        (repo / "AGENTS.md").write_text("Never run the linter.\n")
        await session.reload()

        assert "Never run the linter" in session.system_prompt
        assert "Always run the linter" not in session.system_prompt

    async def test_setting_a_session_name_is_journaled(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, [], storage=storage)
        event = await session.set_session_name("bugfix run")
        assert event.name == "bugfix run"
        assert session.session_name == "bugfix run"
        labels = [e for e in await storage.read_all() if isinstance(e, LabelEntry)]
        assert labels[-1].label == "bugfix run"


class TestCheckpoints:
    async def test_new_session_clears_the_state(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await collect(session, "explain main.py")
        assert session.answer is not None

        await session.new_session()
        assert session.answer is None
        assert session.state["findings"] == {}
        assert session.state["cwd"] == str(session.cwd)

    async def test_checkpoints_can_be_restored(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        await collect(session, "explain main.py")

        first_step = next(e for e in await session.checkpoints() if isinstance(e, StepEntry))
        await session.restore(first_step.id, reason="rewind")

        assert session.answer is None
        assert session.state["findings"] == {"entrypoint": "main.py"}

    async def test_a_prepared_session_writes_nothing_until_adopted(self, project) -> None:
        """An abandoned candidate must leave the journal exactly as it found it."""
        from rio_coding.session_preparation import prepare_coding_session

        repo, home = project
        from rio_coding.paths import RioPaths
        from rio_coding.resources import RioResourcePaths

        storage = InMemorySessionStorage()
        paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
        config = CodingSessionConfig(
            provider=FakeProvider([]),
            model="test-model",
            cwd=repo,
            paths=paths,
            resource_paths=RioResourcePaths(
                root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
            ),
            storage=storage,
            tools=(READ, RESPOND),
        )

        prepared = await prepare_coding_session(config)
        assert await storage.read_all() == []

        await prepared.abort()
        assert await storage.read_all() == []

        adopted = await (await prepare_coding_session(config)).adopt()
        assert [e.type for e in await storage.read_all()] == ["session_info", "leaf"]
        assert adopted.cwd == repo.resolve()

    async def test_adopting_twice_is_refused(self, project) -> None:
        from rio_coding.session_preparation import prepare_coding_session

        repo, home = project
        from rio_coding.paths import RioPaths
        from rio_coding.resources import RioResourcePaths

        paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
        prepared = await prepare_coding_session(
            CodingSessionConfig(
                provider=FakeProvider([]),
                model="test-model",
                cwd=repo,
                paths=paths,
                resource_paths=RioResourcePaths(
                    root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
                ),
                storage=InMemorySessionStorage(),
                tools=(READ, RESPOND),
            )
        )
        await prepared.adopt()
        with pytest.raises(RuntimeError, match="already adopted"):
            await prepared.adopt()

    async def test_resume_reads_the_journal_back(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        await collect(session, "explain main.py")

        reopened = await make_session(project, [], storage=storage)
        assert reopened.state["findings"] == {"entrypoint": "main.py"}
        assert reopened.state["plan"] == [{"id": "1", "title": "explain", "status": "done"}]


async def test_metadata_keeps_journal_connected_and_preserves_resumed_state(project):
    from rio_coding.session_store import latest_leaf_id, path_to_entry

    storage = InMemorySessionStorage()
    session = await make_session(project, two_step_streams(), storage=storage)
    initial = session.state
    resumed = await make_session(project, [], storage=storage)
    assert resumed.state == initial
    _ = [event async for event in session.prompt("explain main.py")]
    await session.set_session_name("Entry point")
    await session.set_model("another-model")
    entries = await storage.read_all()
    path = path_to_entry(entries, latest_leaf_id(entries))
    assert path[0].type == "session_info"
    assert [entry.type for entry in path][-2:] == ["label", "model_change"]
    resumed = await make_session(project, [], storage=storage)
    assert resumed.state == session.state
    assert resumed.session_name == "Entry point"
