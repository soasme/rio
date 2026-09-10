"""Tests for `SessionManager`: the user-home index of coding sessions."""

from __future__ import annotations

import pytest

from rio.coding.paths import RioPaths
from rio.coding.session_manager import (
    CodingSessionRecord,
    SessionManager,
    normalize_session_name,
    validate_session_id,
)


@pytest.fixture
def manager(tmp_path):
    return SessionManager(RioPaths(home=tmp_path / ".rio", agents_home=tmp_path / ".agents"))


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


class TestSessionIds:
    @pytest.mark.parametrize(
        "session_id", ["abc", "a1", "my-session", "my_session", "my.session", "A9"]
    )
    def test_portable_ids_are_accepted(self, session_id: str) -> None:
        validate_session_id(session_id)

    @pytest.mark.parametrize(
        "session_id",
        ["", "-lead", "trail-", ".dot", "has space", "has/slash", "sl\\ash", "emoji-🙂"],
    )
    def test_unsafe_ids_are_rejected(self, session_id: str) -> None:
        with pytest.raises(ValueError):
            validate_session_id(session_id)

    @pytest.mark.parametrize("session_id", ["default", "index", "DEFAULT", "Index"])
    def test_reserved_ids_are_rejected(self, session_id: str) -> None:
        with pytest.raises(ValueError, match="reserved"):
            validate_session_id(session_id)

    @pytest.mark.parametrize("session_id", ["con", "aux", "nul.jsonl", "COM1", "lpt9"])
    def test_windows_reserved_stems_are_rejected(self, session_id: str) -> None:
        with pytest.raises(ValueError, match="portable"):
            validate_session_id(session_id)

    def test_overlong_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            validate_session_id("a" * 129)


class TestSessionNames:
    def test_names_are_trimmed(self) -> None:
        assert normalize_session_name("  bugfix run  ") == "bugfix run"

    def test_empty_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            normalize_session_name("   ")

    @pytest.mark.parametrize("name", ["two\nlines", "tab\there", "carriage\rreturn"])
    def test_multiline_names_are_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="single line"):
            normalize_session_name(name)


class TestLifecycle:
    def test_create_then_list(self, manager: SessionManager, repo) -> None:
        record = manager.create_session(cwd=repo, model="test-model", title="first")
        listed = manager.list_sessions(repo)
        assert [item.id for item in listed] == [record.id]
        assert listed[0].title == "first"
        assert listed[0].cwd == repo.resolve()

    def test_get_session_by_id(self, manager: SessionManager, repo) -> None:
        record = manager.create_session(cwd=repo, model="test-model")
        assert manager.get_session(record.id) == record
        assert manager.get_session("missing") is None

    def test_sessions_are_listed_newest_first(self, manager: SessionManager, repo) -> None:
        first = manager.create_session(cwd=repo, model="m", session_id="first")
        second = manager.create_session(cwd=repo, model="m", session_id="second")
        manager.touch_session(first.id, title="touched")
        assert [item.id for item in manager.list_sessions(repo)] == [first.id, second.id]

    def test_latest_session_for_cwd(self, manager: SessionManager, repo) -> None:
        assert manager.latest_session_for_cwd(repo) is None
        manager.create_session(cwd=repo, model="m", session_id="one")
        second = manager.create_session(cwd=repo, model="m", session_id="two")
        assert manager.latest_session_for_cwd(repo).id == second.id

    def test_sessions_are_scoped_to_their_project(self, manager: SessionManager, tmp_path) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        manager.create_session(cwd=first, model="m", session_id="in-first")
        manager.create_session(cwd=second, model="m", session_id="in-second")

        assert [r.id for r in manager.list_sessions(first)] == ["in-first"]
        assert [r.id for r in manager.list_sessions(second)] == ["in-second"]
        assert {r.id for r in manager.list_sessions()} == {"in-first", "in-second"}

    def test_prepare_does_not_index(self, manager: SessionManager, repo) -> None:
        record = manager.prepare_session(cwd=repo, model="m")
        assert manager.get_session(record.id) is None
        manager.index_session(record)
        assert manager.get_session(record.id) == record

    def test_prepared_paths_live_under_the_project_session_dir(
        self, manager: SessionManager, repo
    ) -> None:
        record = manager.prepare_session(cwd=repo, model="m", session_id="mine")
        assert record.path.parent == manager.paths.project_session_dir(repo.resolve())
        assert record.path.name == "mine.jsonl"
        assert record.path.parent.exists()

    def test_touch_updates_metadata_and_timestamp(self, manager: SessionManager, repo) -> None:
        record = manager.create_session(cwd=repo, model="old-model")
        updated = manager.touch_session(record.id, model="new-model", title="renamed")
        assert updated.model == "new-model"
        assert updated.title == "renamed"
        assert updated.created_at == record.created_at
        assert updated.updated_at >= record.updated_at

    def test_touch_of_a_missing_session_returns_none(self, manager: SessionManager) -> None:
        assert manager.touch_session("nope") is None


class TestExclusiveCreation:
    def test_reserves_the_journal_file(self, manager: SessionManager, repo) -> None:
        record = manager.create_session_exclusive(cwd=repo, model="m", session_id="only")
        assert record.path.exists()

    def test_refuses_to_overwrite_an_existing_session(self, manager: SessionManager, repo) -> None:
        manager.create_session_exclusive(cwd=repo, model="m", session_id="only")
        with pytest.raises(RuntimeError, match="already exists"):
            manager.create_session_exclusive(cwd=repo, model="m", session_id="only")

    def test_refuses_when_the_file_exists_but_the_index_does_not(
        self, manager: SessionManager, repo
    ) -> None:
        prepared = manager.prepare_session(cwd=repo, model="m", session_id="orphan")
        prepared.path.write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError, match="already exists"):
            manager.create_session_exclusive(cwd=repo, model="m", session_id="orphan")


class TestDefaultSession:
    def test_is_created_once_and_reused(self, manager: SessionManager, repo) -> None:
        first = manager.get_or_create_default_session(cwd=repo, model="m")
        second = manager.get_or_create_default_session(cwd=repo, model="other")
        assert first.id == second.id
        assert second.model == "m", "an existing default is returned unchanged"

    def test_uses_the_project_default_journal_path(self, manager: SessionManager, repo) -> None:
        record = manager.get_or_create_default_session(cwd=repo, model="m")
        assert record.path == manager.paths.default_session_path(repo.resolve())

    def test_the_default_id_cannot_be_claimed_explicitly(
        self, manager: SessionManager, repo
    ) -> None:
        default = manager.get_or_create_default_session(cwd=repo, model="m")
        with pytest.raises(ValueError, match="reserved"):
            manager.prepare_session(cwd=repo, model="m", session_id=default.id)


class TestPersistence:
    def test_the_index_survives_a_new_manager(self, tmp_path, repo) -> None:
        paths = RioPaths(home=tmp_path / ".rio", agents_home=tmp_path / ".agents")
        SessionManager(paths).create_session(cwd=repo, model="m", session_id="kept")
        assert SessionManager(paths).get_session("kept") is not None

    def test_a_missing_index_is_an_empty_list(self, manager: SessionManager, repo) -> None:
        assert manager.list_sessions(repo) == []
        assert manager.list_sessions() == []

    def test_records_round_trip_through_json(self, manager: SessionManager, repo) -> None:
        record = manager.create_session(
            cwd=repo, model="m", provider_name="anthropic", title="round trip"
        )
        restored = CodingSessionRecord.from_model(record.to_model())
        assert restored == record

    def test_blank_lines_in_the_index_are_ignored(self, manager: SessionManager, repo) -> None:
        manager.create_session(cwd=repo, model="m", session_id="kept")
        index = manager.project_index_path(repo.resolve())
        index.write_text(index.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        assert [r.id for r in manager.list_sessions(repo)] == ["kept"]
