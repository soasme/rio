"""Code and cmd Cells: the runnable kinds that make rio.agent a coding agent."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from rio.agent.state import CellKind, InvalidPatch

INSTRUCTIONS = """You are a coding agent. Your process working directory is the target project.
Inspect its relative files first; use pathlib or rg scoped to this directory. Do not search /
or /usr for project source. Repository code may differ from an installed package. Check local
files and available project tests before choosing commands. Avoid running broad test suites
when a focused regression test can verify the change.
Read existing code before editing it. Make the smallest correct change and run relevant tests.
"""


def script_metadata(source: str) -> dict:
    blocks = re.findall(r"(?m)^# /// script\n((?:#(?: .*|)\n)*)# ///\s*$", source)
    if len(blocks) != 1:
        raise InvalidPatch("Code requires one PEP 723 script block with dependencies")
    try:
        metadata = tomllib.loads("\n".join(line[2:] for line in blocks[0].splitlines()))
    except tomllib.TOMLDecodeError as exc:
        raise InvalidPatch(str(exc)) from exc
    if not isinstance(metadata.get("dependencies"), list) or not all(
        isinstance(item, str) for item in metadata["dependencies"]
    ):
        raise InvalidPatch("PEP 723 dependencies must be an array of strings")
    if "requires-python" in metadata and not isinstance(metadata["requires-python"], str):
        raise InvalidPatch("requires-python must be a string")
    return metadata


def _validate_code(cell: dict) -> None:
    if cell.get("runtime") != "python" or not isinstance(cell.get("source"), str):
        raise InvalidPatch("Code requires runtime: python and a source string")
    script_metadata(cell["source"])


def _code_command(cell: dict, scratch: Path) -> list[str]:
    script = scratch / f"cell-{cell['id']}.py"
    script.write_text(cell["source"], encoding="utf-8")
    return ["uv", "run", "--script", str(script)]


def _validate_cmd(cell: dict) -> None:
    argv = cell.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise InvalidPatch("Cmd requires a nonempty argv array of strings")


CODE = CellKind(
    name="code",
    fields={"runtime": {"type": "string", "enum": ["python"]}, "source": {"type": "string"}},
    prompt="""Code: {"kind":"code","runtime":"python","source":"..."}.
Code runs via uv run in a fresh process; output has stdout and stderr streams.
Each script MUST include PEP 723 metadata, even with no dependencies:
# /// script
# dependencies = []
# ///
from pathlib import Path
print(Path('README.md').read_text())
No shared variables, kernel, magics, top-level return, or cell imports. Use normal Python,
pathlib, subprocess, and ordinary files.
Check whether a workspace .venv/bin/python exists before using it for project tests.
Otherwise use PEP 723 dependencies for packages your script imports, or run a test
command through uv run --with pytest python -m pytest. uv's script interpreter is
separate from a project environment. When writing multiline files, prefer triple-quoted
Python strings so newline escaping does not break the script.
Scripts can use `from rio.coding.api import timer, records`. timer(request_id,
unix_deadline, payload) durably registers one timer generation; it does not suspend or rerun
the process. records(after=0) retrieves journal events as data; records(N - 1)[0] is
session:event:N.
Files and package caches are not durable State.
""",
    validate=_validate_code,
)

CMD = CellKind(
    name="cmd",
    fields={"argv": {"type": "array", "items": {"type": "string"}}},
    prompt="""Cmd: {"kind":"cmd","argv":["pytest","-q"]}.
A cmd runs argv directly in a fresh process, without a shell; use ["sh","-c","..."]
for pipes or globs.
Prefer cmd Cells to run tests or shell tools.
""",
    validate=_validate_cmd,
)

KINDS = (CODE, CMD)

# How ProcessOwner runs each kind: the argv for one Cell version, given a scratch directory.
COMMANDS = {"code": _code_command, "cmd": lambda cell, scratch: cell["argv"]}
