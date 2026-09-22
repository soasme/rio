# Run your first task

This tutorial runs Rio against a small task in a project directory.

## Install Rio

```bash
uv tool install rio
```

## Log in to a provider

Choose a provider supported by your configuration, then save its credentials:

```bash
rio login anthropic
```

Use `rio login PROVIDER --method METHOD` when the provider offers more than
one authentication method.

## Run a task

Change to the project Rio should work in and describe the outcome:

```bash
cd my-project
rio run "Add a health-check endpoint and tests."
```

Rio prints progress and a session ID when it finishes. Keep that ID if the
task needs a follow-up.

## Run a task from a file

For a longer request, put the instructions in a Markdown file:

```markdown
# Task

Add a health-check endpoint and tests.
Run the test suite before finishing.
```

Then pass the file to `rio run`:

```bash
rio run task.md
```

Rio reads a sole Markdown-file argument as the task text.

## Follow a task file with an additional instruction

Ask Rio to follow an existing task file and add a one-off instruction in the
same request:

```bash
rio run "Follow task.md, run the tests, and finish the implementation."
```

Because this invocation contains more than the file name, Rio receives it as
task text and reads `task.md` as part of the work.
