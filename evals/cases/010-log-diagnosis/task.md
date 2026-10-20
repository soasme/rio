Run python diagnose.py before editing to reproduce the failure. This scenario tests Rio's handling of large tool output: let the diagnostic command's full stdout reach Rio; do not redirect or trim it before delivery. Rio may truncate the observation; use the output tail or full output log to find the error.

Then fix parser.py. parse_status(lines) should return the value after the last STATUS= prefix, stripping whitespace; ignore other lines and raise ValueError if no status exists. Preserve diagnose.py.
