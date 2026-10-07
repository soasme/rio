# Coding CLI messages

The human transcript uses a bullet (`•`) for each new entry, with a blank line
between entries. Results follow with `└` and an indented body. Continuations align
beneath the message or result. All content is literal text, including brackets.
These presentation principles apply to the durable CLI as well as the earlier
TUI. `--output json` continues to emit the committed event records unchanged.

## Durable cells

Render accepted note text and complete Python source, including PEP 723 metadata,
from `patch_accepted`. Entries contain the code or note itself: no `Cell` label,
number, kind, predecessor, or control metadata. IDs remain internal to ordering
and output tracking. Show only new versions in allocation order, not every cell
in the State snapshot. Removing a cell from current context does not erase its
transcript history. Rejected or uncommitted cells are not shown.

Execution authorization does not add a separate entry. Output is visible as it
commits, before execution finishes. Stdout follows `└` directly; stderr adds a
`stderr:` label. Completion shows the outcome without a cell label or number.

```text
• Check the calculation.

• # /// script
  # dependencies = []
  # ///
  print(1 + 1)
  └ 2
  └ success (exit 0)

• The calculation returned 2.

• Success: The calculation returned 2.
```

Control notes display their text. A conclusion note is a request, not an accepted
run ending. Only `run_ended` reports final success or failure. Patch rejection
reports `Retry: <reason>`; model-round bookkeeping does not create transcript entries.

## Output and replay

Execution snapshots contain cumulative output. Print only the newly committed
suffix of each stdout/stderr stream. Partial lines remain contiguous across
chunks. If another entry or stream interrupts a partial line, close it and start
the resumed output with a fresh `└` (and `stderr:` for stderr). Completion always shows an outcome, including silent
success, nonzero exit status, cancellation, and `UnknownExecution` with its reason.

Limit displayed stdout and stderr together to 8,000 characters per execution,
with one explicit `[output truncated]` notice. Also show the notice if durable
storage truncated the output. This display limit does not change Session records.
Cell source and note text retain their line breaks and indentation.

Resume replays the committed transcript in journal order, then displays new
records. Rendering does not launch code or request the model. Repeated cumulative
snapshots do not duplicate output. A terminal session displays its history and
ending without running work again.

## CLI scope

The current CLI is an append-only plain-text stream; the terminal supplies wrapping
and scrollback. It has no TUI history panel, state sidebar, active status row,
resize reflow, or entry eviction. The earlier TUI timer and compact read/write/edit
renderers do not apply: the durable runtime executes independent Python cells,
not separately reported tool invocations. Failures use explicit text labels;
plain output requires no ANSI colors or terminal controls.

Tests cover committed content visibility, hidden cell metadata, literal multiline
content, streamed output, interleaved notes, truncation, silent and failed
executions, and replay in both human and JSON modes. Lean models the committed-cell
projection and cumulative-output suffix rule separately from the Python tests.
