"""The prompt a CLM step sends: instructions, the context protocol, and the context.

The model sees its context as ordinary chat messages. Assistant turns become
assistant messages; every other turn becomes user text, labelled with its role
when that role is not `user`. Consecutive turns that map to the same message
role are merged so any edited context is a legal request. Tool-call structure
is not kept: a step's action is recorded as text in its assistant turn, so the
model can edit it like anything else.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rio.agent.context import Turn
from rio.ai.messages import AgentMessage, AssistantMessage, UserMessage


def context_protocol(path: Path, limit_tokens: int) -> str:
    """Return the system-prompt section that teaches the model to edit its context."""
    return (
        "## Your context\n\n"
        f"Your context is mirrored to `{path}`, rewritten before every step. It holds "
        "every turn after this system prompt, one `[[CTX_TURN <i> role=<role>]]` block "
        "per turn. You manage your own context by editing that file with your tools: "
        "shorten stale output, delete dead ends, merge turns into notes, reorder them, or "
        "add blocks with any role label. The edited file becomes your context on the next "
        f"step. Your context limit is about {limit_tokens} tokens; each observation ends "
        "with the current size.\n\n"
        "- Do not read the whole file: its text is already in your context.\n"
        "- Locate text with code or by unique lines; do not retype long bodies.\n"
        "- Keep the header of every turn you keep. A turn with an empty body is dropped.\n"
        "- Batch edits: everything after an edit is re-read, so one large edit beats "
        "many small ones.\n"
        "- Keep the task, decisions, open items, file paths, and exact values. Drop tool "
        "output you have already used."
    )


def build_messages(context: Sequence[Turn], *, error_note: str | None = None) -> list[AgentMessage]:
    """Return the request messages for `context`, plus a transient correction."""
    parts: list[tuple[str, str]] = []
    for item in context:
        role, text = str(item["role"]), str(item["text"])
        api_role = "assistant" if role == "assistant" else "user"
        if role not in ("user", "assistant"):
            text = f"[{role}]\n{text}"
        if parts and parts[-1][0] == api_role:
            parts[-1] = (api_role, parts[-1][1] + "\n\n" + text)
        else:
            parts.append((api_role, text))
    if error_note:
        note = f"Rejected reply: {error_note}\nRetry with a corrected reply."
        if parts and parts[-1][0] == "user":
            parts[-1] = ("user", parts[-1][1] + "\n\n" + note)
        else:
            parts.append(("user", note))
    if not parts or parts[0][0] != "user":
        parts.insert(0, ("user", "(start of context)"))
    if parts[-1][0] != "user":
        parts.append(("user", "Continue."))
    return [
        AssistantMessage(content=text) if role == "assistant" else UserMessage(content=text)
        for role, text in parts
    ]
