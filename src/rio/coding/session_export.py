"""Session export helpers for human-readable run views.

A session journal is a sequence of committed notebook steps (see
`rio.coding.session_store`), each carrying the JSON Patch it made. The export
renders a table of steps, the per-step token footprint, and the final notebook
as the model saw it.

Cell outputs are arbitrary command output, so every value is HTML-escaped.
"""

from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import JsonLexer

from rio.coding.agent import render_notebook
from rio.coding.session_store import (
    BranchSummaryEntry,
    CustomEntry,
    LabelEntry,
    LeafEntry,
    ModelChangeEntry,
    ReasoningEntry,
    SessionEntry,
    SessionInfoEntry,
    StateResetEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
    TurnEntry,
    ValidationFailureEntry,
    latest_leaf_id,
    notebook_at_entry,
)
from rio.coding.session_usage import StepUsage, collect_session_usage

__all__ = [
    "SessionExportError",
    "default_session_export_artifact_path",
    "default_session_export_path",
    "export_session_artifact",
    "export_session_html",
    "export_session_jsonl",
    "normalize_export_format",
    "render_session_html",
]

#: Hard cap on how much of one step's patch the export embeds: it carries cell
#: outputs (e.g. a full test-suite log), and this is a single file.
_PATCH_CHARS = 4_000


class SessionExportError(ValueError):
    """Raised when a session cannot be exported."""


def default_session_export_path(session_path: Path) -> Path:
    """Return the default HTML export path for a JSONL session file."""
    return session_path.with_suffix(".html")


def default_session_export_artifact_path(
    session_path: Path,
    *,
    destination_dir: Path,
    format: str = "html",
) -> Path:
    """Return the default user-facing export artifact path."""
    suffix = _export_suffix(format)
    return destination_dir / f"{session_path.stem}{suffix}"


def export_session_jsonl(entries: Sequence[SessionEntry], output_path: Path) -> Path:
    """Write session entries to a JSONL export and return its path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_session_jsonl_text(entries), encoding="utf-8")
    return output_path


def _session_jsonl_text(entries: Sequence[SessionEntry]) -> str:
    """Serialize session entries to JSONL text (one JSON object per line)."""
    lines = [entry.model_dump_json() for entry in entries]
    return "\n".join(lines) + ("\n" if lines else "")


def export_session_html(
    entries: Sequence[SessionEntry],
    output_path: Path,
    *,
    title: str = "Rio Session Export",
    source: str | None = None,
    instructions: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> Path:
    """Write a self-contained HTML session export and return its path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_session_html(
            entries,
            title=title,
            source=source,
            instructions=instructions,
            provider=provider,
            model=model,
        ),
        encoding="utf-8",
    )
    return output_path


def export_session_artifact(
    entries: Sequence[SessionEntry],
    output_path: Path,
    *,
    title: str = "Rio Session Export",
    source: str | None = None,
    format: str | None = None,
    instructions: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> Path:
    """Write a session export in the requested or inferred format."""
    export_format = normalize_export_format(format or output_path.suffix.removeprefix("."))
    if export_format == "jsonl":
        return export_session_jsonl(entries, output_path)
    return export_session_html(
        entries,
        output_path,
        title=title,
        source=source,
        instructions=instructions,
        provider=provider,
        model=model,
    )


def normalize_export_format(value: str | None) -> str:
    """Normalize a session export format name."""
    normalized = (value or "html").strip().lower().removeprefix(".")
    if normalized in {"htm", "html"}:
        return "html"
    if normalized == "jsonl":
        return "jsonl"
    raise SessionExportError(f"Unsupported export format: {value}")


def _export_suffix(format: str) -> str:
    return ".jsonl" if normalize_export_format(format) == "jsonl" else ".html"


def _jsonl_filename(title: str, source: str | None) -> str:
    """Return the filename embedded in the in-page JSONL download link."""
    if source:
        stem = Path(source).stem
        if stem:
            return f"{stem}.jsonl"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{slug or 'rio-session'}.jsonl"


def render_session_html(
    entries: Sequence[SessionEntry],
    *,
    title: str = "Rio Session Export",
    source: str | None = None,
    instructions: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """Render a completed notebook run as standalone HTML.

    `instructions` are the skill's fixed instructions, which a step entry does
    not journal. They are optional because an export must remain possible when
    the caller cannot reconstruct them; token figures are simply smaller
    without them.
    """
    entry_list = list(entries)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    source_html = f'<p class="source">Source: <code>{_escape(source)}</code></p>' if source else ""
    meta_html = _render_session_meta(entry_list)
    instructions_html = _render_instructions(instructions)
    steps_html = _render_steps_table(entry_list)
    final_context_html = f"<pre>{_escape(_final_notebook_text(entry_list))}</pre>"
    footprint_html = _render_footprint(
        entry_list, instructions=instructions or "", provider=provider, model=model
    )
    jsonl_b64 = base64.b64encode(_session_jsonl_text(entry_list).encode("utf-8")).decode("ascii")
    jsonl_filename = _jsonl_filename(title, source)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  <style>
{_STYLE}
  </style>
</head>
<body>
  <header>
    <p class="eyebrow">rio session export</p>
    <h1>{_escape(title)}</h1>
    <div class="export-meta">
      {source_html}
      <p class="generated">
        Generated: <time datetime="{_attr(generated_at)}">{_escape(generated_at)}</time>
      </p>
      {meta_html}
    </div>
    {instructions_html}
    <a class="jsonl-download" download="{_attr(jsonl_filename)}"
       href="data:application/jsonl;base64,{jsonl_b64}">
      Download JSONL
    </a>
  </header>
  <main>
    <section aria-label="Token footprint">
      <h2>Token footprint</h2>
      {footprint_html}
    </section>
    <section aria-label="Run steps">
      <h2>Steps</h2>
      {steps_html}
    </section>
    <section aria-label="Final notebook">
      <h2>Final notebook</h2>
      {final_context_html}
    </section>
  </main>
</body>
</html>
"""


def _render_session_meta(entries: Sequence[SessionEntry]) -> str:
    """Render session-identifying facts pulled out of the journal, if present."""
    info: SessionInfoEntry | None = None
    model_name: str | None = None
    thinking_level: str | None = None
    step_count = 0
    turn_count = 0
    for entry in entries:
        if isinstance(entry, SessionInfoEntry):
            info = entry
        elif isinstance(entry, ModelChangeEntry):
            model_name = entry.model
        elif isinstance(entry, ThinkingLevelChangeEntry):
            thinking_level = entry.thinking_level
        elif isinstance(entry, StepEntry):
            step_count += 1
        elif isinstance(entry, TurnEntry):
            turn_count += 1

    parts: list[str] = [f"<span>{step_count} step(s)</span>", f"<span>{turn_count} turn(s)</span>"]
    if info is not None:
        if info.title:
            parts.append(f"<span>title <code>{_escape(info.title)}</code></span>")
        if info.cwd:
            parts.append(f"<span>cwd <code>{_escape(info.cwd)}</code></span>")
        if info.skill:
            parts.append(f"<span>skill <code>{_escape(info.skill)}</code></span>")
    if model_name:
        parts.append(f"<span>model <code>{_escape(model_name)}</code></span>")
    if thinking_level:
        parts.append(f"<span>thinking <code>{_escape(thinking_level)}</code></span>")
    return f'<p class="session-meta">{"".join(parts)}</p>'


def _render_instructions(instructions: str | None) -> str:
    """Render the skill's fixed instructions, collapsed since they can be long."""
    if not instructions:
        return ""
    return (
        '<details class="instructions">'
        "<summary>Skill instructions</summary>"
        f"<pre>{_escape(instructions)}</pre>"
        "</details>"
    )


def _render_steps_table(entries: Sequence[SessionEntry]) -> str:
    retries_by_step: dict[int, list[ValidationFailureEntry]] = {}
    reasoning_by_step: dict[int, list[ReasoningEntry]] = {}
    for entry in entries:
        if isinstance(entry, ValidationFailureEntry):
            retries_by_step.setdefault(entry.step, []).append(entry)
        elif isinstance(entry, ReasoningEntry):
            reasoning_by_step.setdefault(entry.step, []).append(entry)

    rows: list[str] = []
    for entry in entries:
        if isinstance(entry, StepEntry):
            rows.extend(_render_retry_rows(retries_by_step.get(entry.step, [])))
            rows.extend(_render_reasoning_rows(reasoning_by_step.get(entry.step, [])))
            rows.append(_render_step_row(entry))
        else:
            note = _render_event_note(entry)
            if note:
                rows.append(note)

    if not rows:
        return '<p class="empty">No steps recorded.</p>'

    return (
        '<table class="steps">'
        "<thead><tr>"
        "<th>#</th><th>Cells run</th><th>Patch</th><th>Reply</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _render_retry_rows(failures: Sequence[ValidationFailureEntry]) -> list[str]:
    return [
        '<tr class="retry"><td colspan="4">'
        f"retry {failure.attempt}: {_escape(failure.error)}"
        "</td></tr>"
        for failure in failures
    ]


def _render_reasoning_rows(reasoning: Sequence[ReasoningEntry]) -> list[str]:
    """Render a step's reasoning above it, collapsed."""
    rows: list[str] = []
    for entry in reasoning:
        truncated = '<p class="truncated-note">(truncated)</p>' if entry.truncated else ""
        rows.append(
            '<tr class="reasoning"><td colspan="4">'
            '<details class="reasoning">'
            "<summary>reasoning</summary>"
            f"<pre>{_escape(entry.reasoning)}</pre>{truncated}"
            "</details>"
            "</td></tr>"
        )
    return rows


def _render_step_row(entry: StepEntry) -> str:
    classes = "terminated" if entry.terminated else ""
    patch = json.dumps(entry.patch, indent=1, ensure_ascii=False)
    patch_html = _render_json_block(patch[:_PATCH_CHARS])
    if len(patch) > _PATCH_CHARS:
        patch_html += '<p class="truncated-note">(truncated)</p>'
    cells = ", ".join(str(index) for index in entry.cells)
    reply = f"<pre>{_escape(entry.reply)}</pre>" if entry.reply is not None else _empty()
    terminated_badge = ' <span class="badge">final</span>' if entry.terminated else ""
    return (
        f'<tr id="step-{entry.step}" class="{classes}">'
        f"<td>{entry.step}{terminated_badge}</td>"
        f"<td>{cells or _empty()}</td>"
        f"<td>{patch_html}</td>"
        f"<td>{reply}</td>"
        "</tr>"
    )


def _render_event_note(entry: SessionEntry) -> str | None:
    """Render a non-step journal entry as a thin note row between steps."""
    text = _event_note_text(entry)
    if text is None:
        return None
    return f'<tr class="event"><td colspan="4">{text}</td></tr>'


def _event_note_text(entry: SessionEntry) -> str | None:
    if isinstance(entry, TurnEntry):
        return f"turn: {_escape(_summarize(entry.observation))}"
    if isinstance(entry, StateResetEntry):
        reason = f": {_escape(entry.reason)}" if entry.reason else ""
        return f"notebook reset{reason}"
    if isinstance(entry, ModelChangeEntry):
        return f"model changed to <code>{_escape(entry.model)}</code>"
    if isinstance(entry, ThinkingLevelChangeEntry):
        return f"thinking level changed to <code>{_escape(entry.thinking_level or 'off')}</code>"
    if isinstance(entry, BranchSummaryEntry):
        return f"branch summary: {_escape(_summarize(entry.summary))}"
    if isinstance(entry, LabelEntry):
        return f"label: <strong>{_escape(entry.label)}</strong>"
    if isinstance(entry, CustomEntry):
        return f"custom[{_escape(entry.namespace)}]: {len(entry.data)} field(s)"
    if isinstance(entry, SessionInfoEntry | LeafEntry | ReasoningEntry):
        # Session metadata is already surfaced in the header; the leaf
        # pointer is plumbing, not session content; reasoning is attached to
        # the step it explains rather than floated between steps.
        return None
    return None


def _render_footprint(
    entries: Sequence[SessionEntry],
    *,
    instructions: str,
    provider: str | None,
    model: str | None,
) -> str:
    """Render the estimated prompt size of every step."""
    usage = collect_session_usage(
        entries, instructions=instructions, provider=provider, model=model
    )
    return _render_usage_table(usage.steps)


def _render_usage_table(steps: Sequence[StepUsage]) -> str:
    if not steps:
        return '<p class="empty">No steps to measure.</p>'
    rows = "".join(
        "<tr>"
        f"<td>{step.number}</td>"
        f"<td>{_escape(', '.join(map(str, step.cells)))}</td>"
        f"<td>{step.instructions_tokens}</td>"
        f"<td>{step.context_tokens}</td>"
        f"<td>{step.tools_tokens}</td>"
        f"<td>{step.total_tokens}</td>"
        "</tr>"
        for step in steps
    )
    return (
        '<table class="usage">'
        "<thead><tr>"
        "<th>#</th><th>Cells run</th><th>Instructions</th><th>Notebook</th>"
        "<th>Step tool</th><th>Total</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _final_notebook_text(entries: Sequence[SessionEntry]) -> str:
    leaf = latest_leaf_id(entries)
    return render_notebook(notebook_at_entry(entries, leaf)) if leaf is not None else ""


def _empty() -> str:
    return '<span class="empty">&mdash;</span>'


def _summarize(text: str, *, limit: int = 110) -> str:
    summary = " ".join(text.split())
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3].rstrip() + "..."


_JSON_LEXER = JsonLexer()
_HIGHLIGHT_FORMATTER = HtmlFormatter(nowrap=True)


def _render_json_block(source: str) -> str:
    """Render JSON text as a syntax-highlighted, self-contained `<pre>` block."""
    try:
        highlighted = highlight(source, _JSON_LEXER, _HIGHLIGHT_FORMATTER)
    except Exception:  # noqa: BLE001 - fall back to plain escaped text
        return f"<pre>{_escape(source)}</pre>"
    return f'<pre class="highlight">{highlighted}</pre>'


def _escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


_STYLE = """\
    :root {
      color-scheme: light;
      --bg: #ffffff;
      --surface: #f6f7f9;
      --surface-2: #eceef1;
      --text: #1b1f24;
      --muted: #5b6472;
      --line: #d9dce1;
      --accent: #2f6fed;
      --danger: #c8452c;
      --mono: "JetBrains Mono", "SFMono-Regular", Consolas, Menlo, monospace;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #14161a;
        --surface: #1b1e24;
        --surface-2: #22262e;
        --text: #e6e8eb;
        --muted: #9aa3b2;
        --line: #2c313a;
        --accent: #6ea3ff;
        --danger: #ff8266;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--mono);
      line-height: 1.5;
    }
    code, pre { font-family: var(--mono); font-size: 0.85em; }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: var(--surface-2);
      border: 1px solid var(--line);
      padding: 8px 10px;
      margin: 0;
    }
    header, main { max-width: 1100px; margin: 0 auto; padding: 24px clamp(16px, 4vw, 36px); }
    h1 { margin: 6px 0 10px; font-size: 22px; }
    h2 {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 0.7rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }
    .eyebrow { color: var(--accent); font-size: 0.75rem; margin: 0; }
    .eyebrow::before { content: "$ "; color: var(--muted); }
    .export-meta { color: var(--muted); font-size: 0.75rem; }
    .export-meta p { margin: 2px 0; }
    .session-meta span { margin-right: 14px; }
    details.instructions {
      margin-top: 12px;
      background: var(--surface);
      border: 1px solid var(--line);
    }
    details.instructions summary, details.reasoning summary {
      padding: 8px 12px;
      cursor: pointer;
      font-size: 0.78rem;
      font-weight: 600;
    }
    details.instructions pre { border: 0; margin: 0 12px 12px; }
    details.reasoning summary { padding: 0; }
    details.reasoning pre { margin-top: 6px; }
    .jsonl-download {
      display: inline-block;
      margin-top: 12px;
      padding: 5px 12px;
      color: var(--accent);
      background: var(--surface);
      border: 1px solid var(--accent);
      border-radius: 6px;
      font-size: 0.76rem;
      text-decoration: none;
    }
    section { margin-bottom: 30px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th, td {
      border: 1px solid var(--line);
      padding: 6px 8px;
      text-align: left;
      vertical-align: top;
    }
    th { background: var(--surface); color: var(--muted); font-weight: 600; }
    tr.retry td { color: var(--danger); background: var(--surface); font-style: italic; }
    tr.event td { color: var(--muted); background: var(--surface); }
    tr.reasoning td { color: var(--muted); background: var(--surface); }
    tr.terminated td { background: var(--surface-2); }
    .badge {
      display: inline-block;
      padding: 0 6px;
      color: var(--accent);
      border: 1px solid var(--accent);
      border-radius: 8px;
      font-size: 0.62rem;
      text-transform: uppercase;
    }
    .truncated-note { margin: 4px 0 0; color: var(--muted); font-size: 0.7rem; }
    .empty { color: var(--muted); font-style: italic; }
    pre.highlight { padding: 6px 8px; }
    .highlight .p { color: var(--muted); }
    .highlight .nt { color: var(--accent); }
    .highlight .s2, .highlight .s1 { color: #2f7a4f; }
    .highlight .mi, .highlight .mf { color: #a05a12; }
    .highlight .kc { color: #a02f6b; font-weight: 500; }
    @media (prefers-color-scheme: dark) {
      .highlight .s2, .highlight .s1 { color: #7fd08a; }
      .highlight .mi, .highlight .mf { color: #e0a95e; }
      .highlight .kc { color: #e58fc0; }
    }
"""
