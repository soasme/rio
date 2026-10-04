"""Tests for `CodingSession`, the coding-agent environment.

Most of these check ordinary session behaviour -- resource discovery, journaling,
reconfiguration, checkpoints. The notebook is provider-neutral JSON, so a model
swap needs no history translation, and a checkpoint is the whole notebook.
"""

from __future__ import annotations

import pytest

from conftest import add_markdown, step_response
from rio.agent import new_notebook
from rio.ai import FakeProvider
from rio.coding.events import EntryAppendedEvent, SessionRunEndEvent
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import (
    CustomEntry,
    InMemorySessionStorage,
    LabelEntry,
    ModelChangeEntry,
    SessionInfoEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
)


def two_step_streams():
    """A note, then an answer. Markdown cells run nothing, so no kernel starts."""
    return [
        step_response(
            reasoning="Note the entry point.",
            patch=[add_markdown("main.py starts the server")],
        ),
        step_response(reasoning="Ready to answer.", reply="main.py starts the server."),
    ]


def sources(session) -> list[str]:
    return [cell["source"] for cell in session.notebook["cells"]]


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
    from rio.coding.paths import RioPaths
    from rio.coding.resources import RioResourcePaths

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

    async def test_notebook_starts_empty(self, project) -> None:
        session = await make_session(project, [])
        assert session.notebook == new_notebook()

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

    async def test_the_notebook_records_the_run(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await collect(session, "explain main.py")

        assert sources(session) == [
            "explain main.py",
            "main.py starts the server",
            "main.py starts the server.",
        ]

    async def test_the_task_is_the_first_user_cell(self, project) -> None:
        session = await make_session(project, two_step_streams())
        provider = session.provider
        await collect(session, "explain main.py")

        _model, system, first_messages, _tools = provider.calls[0]
        assert '"source": "explain main.py"' in first_messages[0].text
        assert "Jupyter notebook" in system

    async def test_the_run_is_journaled_as_steps(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        events = await collect(session, "explain main.py")

        steps = [e for e in await storage.read_all() if isinstance(e, StepEntry)]
        assert [s.reply for s in steps] == [None, "main.py starts the server."]
        assert any(isinstance(e, EntryAppendedEvent) for e in events)


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


class TestFootprint:
    async def test_the_step_footprint_is_reported_before_any_run(self, project) -> None:
        session = await make_session(project, [])
        footprint = session.step_footprint
        assert footprint.instructions_tokens > 0
        assert footprint.context_tokens > 0  # the empty notebook skeleton
        assert footprint.total_tokens == (
            footprint.instructions_tokens + footprint.context_tokens + footprint.tools_tokens
        )

    async def test_context_usage_grows_with_the_notebook(self, project) -> None:
        session = await make_session(project, two_step_streams())
        before = session.context_usage.utilization
        await collect(session, "explain main.py")
        after = session.context_usage.utilization

        assert 0.0 < before < after < 1.0
        assert session.context_usage.limit_tokens < session.context_window_tokens


class TestReconfiguration:
    async def test_switching_models_is_journaled_and_needs_no_translation(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        await collect(session, "explain main.py")

        await session.set_model("another-model", provider_name="another-provider")
        assert session.model == "another-model"
        changes = [e for e in await storage.read_all() if isinstance(e, ModelChangeEntry)]
        assert changes[-1].model == "another-model"
        # The notebook survives the swap: it is provider-neutral JSON.
        assert "main.py starts the server" in sources(session)

    async def test_thinking_level_changes_are_journaled(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, [], storage=storage)
        event = await session.set_thinking_level("high")
        assert event.level == "high"
        assert session.thinking_level == "high"
        entries = [e for e in await storage.read_all() if isinstance(e, ThinkingLevelChangeEntry)]
        assert entries[-1].thinking_level == "high"

    async def test_reload_picks_up_edited_project_instructions(self, project) -> None:
        """Instructions are sent every step, so a reload takes effect on the next one."""
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
    async def test_new_session_clears_the_notebook(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await collect(session, "explain main.py")
        assert session.answer is not None

        await session.new_session()
        assert session.answer is None
        assert session.notebook == new_notebook()

    async def test_checkpoints_can_be_restored(self, project) -> None:
        storage = InMemorySessionStorage()
        session = await make_session(project, two_step_streams(), storage=storage)
        await collect(session, "explain main.py")

        first_step = next(e for e in await session.checkpoints() if isinstance(e, StepEntry))
        await session.restore(first_step.id, reason="rewind")

        assert session.answer is None
        assert sources(session) == ["explain main.py", "main.py starts the server"]

    async def test_fork_from_adopts_notebook_and_journals_lineage(self, project) -> None:
        parent = await make_session(project, two_step_streams())
        await collect(parent, "explain main.py")

        child_storage = InMemorySessionStorage()
        child = await make_session(project, [], storage=child_storage)
        await child.fork_from(parent.notebook, parent_session_id="parent-id")

        assert child.notebook == parent.notebook
        entries = await child_storage.read_all()
        fork_notes = [e for e in entries if isinstance(e, CustomEntry) and e.namespace == "fork"]
        assert fork_notes[-1].data == {"parent_session_id": "parent-id"}

    async def test_a_prepared_session_writes_nothing_until_adopted(self, project) -> None:
        """An abandoned candidate must leave the journal exactly as it found it."""
        from rio.coding.session_preparation import prepare_coding_session

        repo, home = project
        from rio.coding.paths import RioPaths
        from rio.coding.resources import RioResourcePaths

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
        )

        prepared = await prepare_coding_session(config)
        assert await storage.read_all() == []

        await prepared.abort()
        assert await storage.read_all() == []

        adopted = await (await prepare_coding_session(config)).adopt()
        assert [e.type for e in await storage.read_all()] == ["session_info", "leaf"]
        assert adopted.cwd == repo.resolve()

    async def test_adopting_twice_is_refused(self, project) -> None:
        from rio.coding.session_preparation import prepare_coding_session

        repo, home = project
        from rio.coding.paths import RioPaths
        from rio.coding.resources import RioResourcePaths

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
        assert reopened.notebook == session.notebook
        assert sources(reopened)[0] == "explain main.py"


async def test_metadata_keeps_journal_connected_and_preserves_resumed_notebook(project):
    from rio.coding.session_store import latest_leaf_id, path_to_entry

    storage = InMemorySessionStorage()
    session = await make_session(project, two_step_streams(), storage=storage)
    initial = session.notebook
    resumed = await make_session(project, [], storage=storage)
    assert resumed.notebook == initial
    _ = [event async for event in session.prompt("explain main.py")]
    await session.set_session_name("Entry point")
    await session.set_model("another-model")
    entries = await storage.read_all()
    path = path_to_entry(entries, latest_leaf_id(entries))
    assert path[0].type == "session_info"
    assert [entry.type for entry in path][-2:] == ["label", "model_change"]
    resumed = await make_session(project, [], storage=storage)
    assert resumed.notebook == session.notebook
    assert resumed.session_name == "Entry point"
