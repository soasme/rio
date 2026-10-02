"""The context as a file.

A context is a list of turns, each a JSON object `{"role": str, "text": str}`.
The runtime renders it to a file before every step, one `[[CTX_TURN i role=r]]`
block per turn. The model edits that file with its ordinary tools; the runtime
parses the edited file back into turns and uses them for the next step.

Parsing is tolerant: text before the first header becomes a `notes` turn, a
turn whose body is emptied is dropped, and any role label is accepted. Body
lines that look like a header are escaped with a backslash when rendered, so a
tool result that quotes the file cannot inject turns.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from rio.ai.types import JSONObject

type Turn = JSONObject

HEADER_PREFIX = "[[CTX_TURN "
_HEADER_RE = re.compile(r"^\[\[CTX_TURN\s+\d+\s+role=([A-Za-z_-]+)\]\][ \t]*$", re.M)
CHARS_PER_TOKEN = 4


def turn(role: str, text: str) -> Turn:
    return {"role": role, "text": text}


def render_context(context: Sequence[Turn]) -> str:
    """Render turns as the editable context file."""
    blocks = [
        f"{HEADER_PREFIX}{index} role={item['role']}]]\n{_escape(str(item['text']))}"
        for index, item in enumerate(context, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def parse_context(text: str) -> list[Turn]:
    """Parse an edited context file back into turns."""
    matches = list(_HEADER_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    lead = text[: matches[0].start()] if matches else text
    if lead.strip():
        sections.append(("notes", lead))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1), text[match.end() : end]))
    return [turn(role, _unescape(body.strip())) for role, body in sections if body.strip()]


def write_context(path: Path, context: Sequence[Turn]) -> str:
    """Render `context` to `path` and return the rendered text."""
    rendered = render_context(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)
    return rendered


def read_context(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def context_tokens(context: Sequence[Turn]) -> int:
    return estimate_tokens(render_context(context)) if context else 0


def withhold_oldest(context: list[Turn], *, over_tokens: int) -> list[Turn]:
    """Replace the oldest turn bodies until `over_tokens` are freed.

    The overflow guard runs only when the model let its context outgrow the
    limit. User turns are kept, as is the newest turn: it is the observation
    the model has not seen yet.
    """
    result = [dict(item) for item in context]
    freed = 0
    for item in result[:-1]:
        if freed >= over_tokens:
            break
        if item["role"] == "user":
            continue
        tokens = estimate_tokens(str(item["text"]))
        note = (
            f"[{tokens} tokens withheld by the runtime: the context was over its limit. "
            "Run the action again if you still need this.]"
        )
        if tokens <= estimate_tokens(note):
            continue
        item["text"] = note
        freed += tokens - estimate_tokens(note)
    return result


def _escape(body: str) -> str:
    return "\n".join(
        "\\" + line if line.lstrip("\\").startswith(HEADER_PREFIX) else line
        for line in body.split("\n")
    )


def _unescape(body: str) -> str:
    return "\n".join(
        line[1:] if line.startswith("\\") and line.lstrip("\\").startswith(HEADER_PREFIX) else line
        for line in body.split("\n")
    )
