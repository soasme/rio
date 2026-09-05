# rio skills and prompt templates

Skills provide reusable task knowledge. Prompt templates save prompts that users invoke by name. Neither is part of the model's context by default: rio loads only each skill's name, description, and path into the skill instructions, and the model reads the full file when its description matches the task.

## Skills

A skill follows the Agent Skills structure:

```text
<skills-dir>/<skill-name>/SKILL.md
```

rio loads user and project skills in increasing precedence:

1. `~/.rio/skills/`
2. `~/.agents/skills/`
3. `<cwd>/.rio/skills/`
4. `<cwd>/.agents/skills/`

rio's own product knowledge is regular packaged documentation, not a built-in skill, so it does not appear in the user's skill list or compete with user skill names.

A higher-precedence skill with the same name overrides the lower one. Use `/skill:<name>` for explicit invocation.

A skill with `disable-model-invocation: true` in its `SKILL.md` frontmatter is excluded from the skill instructions entirely, so the model cannot invoke it on its own. The skill stays loaded and remains available through explicit `/skill:<name>` invocation.

## Prompt templates

Templates load from user and project `.rio/prompts/` and `.agents/prompts/` directories. They are prompt shortcuts, not background knowledge, and support Pi-compatible argument placeholders such as `$1`, `$@`, `$ARGUMENTS`, defaults, and slices. Legacy `{{ arguments }}` and `{{ args }}` placeholders remain supported.

Use a skill for reference know-how and a template for a frequently repeated prompt. Reloading resources (`rio_coding.session.CodingSession.reload`) re-discovers both from disk and rebuilds the skill instructions. Because those instructions are only ever sent as the *current* step's system prompt -- there is no transcript they were already baked into -- a reload takes effect on the very next step, not just on new sessions.

When modifying rio's resource system, read `src/rio_coding/skills.py`, `src/rio_coding/prompt_templates.py`, and `src/rio_coding/resources.py`, then test discovery, precedence, diagnostics, prompt formatting, and reload behavior.
