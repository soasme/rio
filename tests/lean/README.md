# Lean behavior models

Run `lake build` in this directory. CI builds all three targets.

The Lean files model decisions in the coding runtime. They are independent
specifications, not proofs extracted from Python. Filesystem contents, hashes,
provider responses, and extension callbacks are supplied as model inputs.
Python tests remain necessary to check that implementation behavior matches.

| Lean file | Python behavior represented |
| --- | --- |
| `RioAgent.lean` | Agent state merge, validation, retries, action execution, event order, cancellation, termination |
| `RioCoding.lean` | Initial coding state and plan gate; file write preflight and cache budget; edit validation; journal checkpoints; command routing; thinking cycle |
| `RioCodingLifecycle.lean` | Prepared session adoption; one-run guard; model and thinking selection; prompt expansion precedence; extension input hooks |

The models deliberately omit transport, OAuth, filesystem and JSONL I/O,
rendering, CLI/TUI interaction, local inference, catalog loading, and extension
registration. The journal model receives an already resolved branch path; it
does not validate or traverse parent links. `validateEdit` receives occurrence
and overlap results instead of searching text. These behaviors are covered by
Python tests, but do not yet have Lean specifications. Future models should
include a source mapping and a check that their decisions match Python.
