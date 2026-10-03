# Lean behavior models

Run `lake build` in this directory. CI builds all four targets.

The Lean files model decisions in the coding runtime. They are independent
specifications, not proofs extracted from Python. Filesystem contents, patch
results, cell outputs, provider responses, and extension callbacks are supplied
as model inputs. Notebook size is total text length rather than a token
estimate.
Python tests remain necessary to check that implementation behavior matches.

| Lean file | Python behavior represented |
| --- | --- |
| `RioAgent.lean` | Notebook loop: which cells run (including after a removed kernel stamp), output merging, kernel stamps, recorded definitions, stale cells, rejecting reads of stale-only names, and placeholders, stop after a failing cell, the size gate on patches, retries, event order, reply termination, cancellation, step limit |
| `RioCoding.lean` | Edit validation; journal replay of resets and step patches, batch append visibility and order, and resume with a new task; command routing; thinking cycle |
| `RioCodingMcp.lean` | `mcp.json` merging: project entries replace global ones only when trusted, `enabled`-only project overrides keep the global entry, invalid entries change nothing; a configured `Authorization` header disables OAuth and a stored token is sent only to the URL it was issued for |
| `RioCodingLifecycle.lean` | Session package isolation across kernel restarts; prepared session adoption; one-run guard; model and thinking selection; blank API-key fallback; prompt expansion precedence; extension input hooks |

The models deliberately omit transport, OAuth, filesystem and JSONL I/O, JSON
Patch application, nbformat validation, kernel process and venv creation,
kernel variables, `%%edit` parsing, output cleaning, rendering,
CLI/TUI interaction, local inference, catalog loading, and extension
registration. The journal model receives an already resolved branch path; it
does not validate or traverse parent links. `validateEdit` receives occurrence
and overlap results instead of searching text. These behaviors are covered by
Python tests, but do not yet have Lean specifications. Future models should
include a source mapping and a check that their decisions match Python.
