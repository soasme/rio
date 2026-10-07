# Trust project resources

Project provider extensions execute Python. Rio loads them only after project
trust is approved. Explicit extension paths are treated as selected by the user.

Approve them for a run when you have reviewed the project:

```bash
rio run --approve "Follow the project's contribution guide and fix issue 42."
```

Disable them explicitly when needed:

```bash
rio run --no-approve "Explain this codebase."
```

Project trust is a resource-loading decision, not a sandbox. Review and run
untrusted code using appropriate isolation.
