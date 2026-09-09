"""What a read leaves behind (issue #58).

An observation lives one step. Before this, a file the model read was gone by
the next step and the only way back to it was another `read` -- which is how a
run spent 67 steps re-reading six files. These tests pin the replacement: the
runtime writes the content into `state.files`, keyed by the range it covers,
stamped with a hash that later writes are checked against.

The tools here are the real ones and the files are real files on disk; only
the step sequencing is written out by hand, in the same order the loop uses it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import step_response
from rio_agent import HarnessObservation, apply_state_delta, run_skill_loop
from rio_ai import FakeProvider
from rio_coding.coding_skill import CodingSkillOptions, build_coding_skill
from rio_coding.file_context import FileContextObserver, StaleFileError, short_hash
from rio_coding.tools import create_coding_tools

CALC = '"""A tiny module."""\n\n\ndef add(a, b):\n    return a + b\n'


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC, encoding="utf-8")
    return repo


async def act(observer, state, tools, name, arguments):
    """Run one action the way `run_skill_loop` runs it: check, execute, record."""
    observer.before_action(state, name, arguments)
    tool = next(tool for tool in tools if tool.name == name)
    result = await tool.execute("call", arguments)
    outcome = observer.after_action(state, name, arguments, result)
    return apply_state_delta(state, outcome.delta), outcome


def entry(state, path="calc.py"):
    return state["files"][path]


def slices(state, path="calc.py"):
    return entry(state, path)["context"]["slices"]


# -- reading ------------------------------------------------------------------


async def test_a_read_puts_the_file_contents_in_the_state(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _outcome = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})

    assert entry(state)["status"] == "read"
    assert entry(state)["hash"] == short_hash((repo / "calc.py").read_bytes())
    assert entry(state)["context"]["total_lines"] == 6
    assert slices(state) == {"1-6": CALC}


async def test_the_key_is_the_path_relative_to_the_run(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _outcome = await act(
        observer, {"files": {}}, tools, "read", {"path": str(repo / "calc.py")}
    )

    assert list(state["files"]) == ["calc.py"]


async def test_reading_another_range_adds_a_slice_instead_of_replacing_one(tmp_path) -> None:
    repo = make_repo(tmp_path)
    (repo / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 501)), encoding="utf-8")
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _ = await act(
        observer, {"files": {}}, tools, "read", {"path": "big.py", "offset": 1, "limit": 100}
    )
    state, _ = await act(
        observer, state, tools, "read", {"path": "big.py", "offset": 101, "limit": 100}
    )

    assert sorted(slices(state, "big.py")) == ["1-100", "101-200"]
    assert slices(state, "big.py")["101-200"].startswith("line 101")
    assert entry(state, "big.py")["context"]["total_lines"] == 500


async def test_a_wider_read_absorbs_the_slice_it_covers(tmp_path) -> None:
    repo = make_repo(tmp_path)
    (repo / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 501)), encoding="utf-8")
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _ = await act(
        observer, {"files": {}}, tools, "read", {"path": "big.py", "offset": 1, "limit": 100}
    )
    state, _ = await act(observer, state, tools, "read", {"path": "big.py"})

    assert list(slices(state, "big.py")) == ["1-500"]


async def test_an_image_read_records_nothing_to_cache(tmp_path) -> None:
    repo = make_repo(tmp_path)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001" + "0d0a2db4" + "0000000049454e44ae426082"
    )
    (repo / "pixel.png").write_bytes(png)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "pixel.png"})

    assert state["files"] == {}


# -- writing against the recorded hash ----------------------------------------


async def test_writing_over_a_file_the_state_has_never_seen_is_refused(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    with pytest.raises(StaleFileError, match="Read it first"):
        await act(observer, {"files": {}}, tools, "write", {"path": "calc.py", "content": "gone\n"})

    assert (repo / "calc.py").read_text(encoding="utf-8") == CALC


async def test_creating_a_new_file_needs_no_prior_read(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)

    state, _ = await act(
        observer, {"files": {}}, tools, "write", {"path": "new.py", "content": "x = 1\n"}
    )

    assert entry(state, "new.py")["status"] == "created"
    assert slices(state, "new.py") == {"1-2": "x = 1\n"}


async def test_a_file_that_changed_underneath_must_be_read_again(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)
    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})

    (repo / "calc.py").write_text(CALC.replace("a + b", "a - b"), encoding="utf-8")

    with pytest.raises(StaleFileError, match="changed on disk"):
        await act(
            observer,
            state,
            tools,
            "edit",
            {"path": "calc.py", "edits": [{"oldText": "a + b", "newText": "a * b"}]},
        )


async def test_an_edit_restamps_the_hash_so_the_next_edit_is_allowed(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)
    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})

    state, _ = await act(
        observer,
        state,
        tools,
        "edit",
        {"path": "calc.py", "edits": [{"oldText": "a + b", "newText": "a * b"}]},
    )
    state, _ = await act(
        observer,
        state,
        tools,
        "edit",
        {"path": "calc.py", "edits": [{"oldText": "a * b", "newText": "a**b"}]},
    )

    assert entry(state)["status"] == "edited"
    assert entry(state)["hash"] == short_hash((repo / "calc.py").read_bytes())
    assert "a**b" in slices(state)["1-6"]


async def test_an_edit_replaces_the_content_the_state_was_holding(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)
    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})

    state, _ = await act(
        observer,
        state,
        tools,
        "edit",
        {"path": "calc.py", "edits": [{"oldText": "a + b", "newText": "a - b"}]},
    )

    assert "a - b" in slices(state)["1-6"]
    assert "a + b" not in slices(state)["1-6"]


# -- forgetting ---------------------------------------------------------------


async def test_a_file_that_does_not_fit_the_budget_is_recorded_but_not_cached(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo, state_budget_chars=200)

    state, outcome = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})

    assert entry(state)["hash"] == short_hash((repo / "calc.py").read_bytes())
    assert "context" not in entry(state)
    assert "Forget a file" in outcome.note


async def test_forgetting_a_file_keeps_its_status_and_note(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)
    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})
    state = apply_state_delta(
        state, {"files": {"calc.py": {"context": None, "note": "add() is correct"}}}
    )

    assert entry(state) == {
        "status": "read",
        "hash": short_hash((repo / "calc.py").read_bytes()),
        "note": "add() is correct",
    }


async def test_a_forgotten_file_is_not_re_cached_by_an_edit(tmp_path) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    observer = FileContextObserver(cwd=repo)
    state, _ = await act(observer, {"files": {}}, tools, "read", {"path": "calc.py"})
    state = apply_state_delta(state, {"files": {"calc.py": {"context": None}}})

    state, _ = await act(
        observer,
        state,
        tools,
        "edit",
        {"path": "calc.py", "edits": [{"oldText": "a + b", "newText": "a - b"}]},
    )

    assert "context" not in entry(state)
    assert entry(state)["hash"] == short_hash((repo / "calc.py").read_bytes())


# -- through the loop ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_step_sees_the_file_in_its_state_not_only_in_the_observation(
    tmp_path,
) -> None:
    repo = make_repo(tmp_path)
    tools = create_coding_tools(cwd=repo)
    skill = build_coding_skill(
        CodingSkillOptions(instructions="instructions", cwd=repo, tools=tuple(tools))
    )
    provider = FakeProvider(
        [
            step_response(reasoning="", state_delta={}, action="read", args={"path": "calc.py"}),
            step_response(reasoning="", state_delta={}, action="respond", args={"message": "ok"}),
        ]
    )

    async for _event in run_skill_loop(
        provider=provider,
        model="m",
        skill=skill,
        observation=HarnessObservation(user_message="explain calc.py"),
    ):
        pass

    second_prompt = provider.calls[1][2][0].content
    state_block = second_prompt.partition("Latest Observation:")[0]
    assert "return a + b" in state_block
