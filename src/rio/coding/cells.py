"""Code and cmd Cells: the runnable kinds that make rio.agent a coding agent."""

from __future__ import annotations

import sys
from pathlib import Path

from rio.agent.state import CellKind, InvalidPatch

INSTRUCTIONS = """You are a coding agent. Your process working directory is the target project.
Inspect its relative files first; use open() in code or rg in cmd, scoped to this directory.
Do not search / or /usr for project source. Repository code may differ from an installed
package. Check local files and available project tests before choosing commands. Avoid running
broad test suites when a focused regression test can verify the change.
Read existing code before editing it. Make the smallest correct change and run relevant tests.
"""


def _validate_code(cell: dict) -> None:
    if cell.get("runtime") != "python" or not isinstance(cell.get("source"), str):
        raise InvalidPatch("Code requires runtime: python and a source string")


def _code_command(cell: dict, scratch: Path) -> list[str]:
    script = scratch / f"cell-{cell['id']}.py"
    script.write_text(cell["source"], encoding="utf-8")
    return [sys.executable, "-m", "rio.coding.codemode", str(script)]


def _validate_cmd(cell: dict) -> None:
    argv = cell.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise InvalidPatch("Cmd requires a nonempty argv array of strings")


CODE = CellKind(
    name="code",
    fields={"runtime": {"type": "string", "enum": ["python"]}, "source": {"type": "string"}},
    prompt="""Code: {"kind":"code","runtime":"python","source":"..."}.
Code runs in Small Python, a minimal Python interpreter, in a fresh process; output has
stdout and stderr streams. Use builtins only: no import statements, standard library, or
packages. open() reaches the working directory (relative paths) and a scratch /tmp; nothing
else. Use cmd Cells to list files, search, run tests, or run other programs.
print(open('README.md').read())
No shared variables, kernel, magics, top-level return, or cell imports.
When writing multiline files, prefer triple-quoted strings so newline escaping does not
break the script.
Builtins timer(request_id, unix_deadline, payload) and records(after=0) are available.
timer durably registers one timer generation; it does not suspend or rerun the process.
records retrieves journal events as dicts with seq, kind, and changes keys;
records(N - 1)[0] is session:event:N.
Builtins mcp_tools(query="") and mcp_call(name, args) reach MCP servers configured in
~/.rio/mcp.json and ./.mcp.json. mcp_tools returns dicts with name ("server.tool"),
description, and input_schema for tools matching every query word. mcp_call returns
structured content, text, or content blocks, and raises RuntimeError on tool errors.
Search first, then call: print(mcp_call("github.get_issue", {"number": 1}))
Files are not durable State.
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
