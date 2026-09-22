# Trust project resources

Project resources can include instructions and extensions from the repository
being worked on. Extensions execute Python, so Rio does not trust project
resources automatically.

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
