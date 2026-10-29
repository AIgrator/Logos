---
name: skill-creator
description: Create or update a reusable Agent Skill (SKILL.md) when the user asks to remember a workflow, make a procedure repeatable, or fix a skill that misbehaved.
---

# Skill creator

Turn a workflow the user wants to keep into a portable `SKILL.md`, or repair an existing skill.

## When to use

The user asks to remember a procedure, make it repeatable, create a skill, or fix a skill that produced the wrong behavior.

## Collect

Ask only what is missing:

- what should trigger the skill (tasks, phrases, file types);
- the steps and the tools they use;
- hard constraints and things to avoid;
- one concrete example of a good result.

## Where to save

- Project-specific workflow: `<project>/.logos/skills/<slug>/SKILL.md`.
- Cross-project workflow: the `skills` folder inside the user profile (`~/.logos/skills/<slug>/SKILL.md`). Resolve it to an absolute path before calling file tools.
- A project or user skill with the same name overrides a built-in one; tell the user when that happens.
- Slug: lowercase letters, digits and hyphens; at most 64 characters.

## Write

Frontmatter:

```yaml
---
name: <slug>
description: What the skill does and when to use it. Put the trigger tasks in the first ~300 characters.
---
```

Body:

- one sentence of purpose;
- numbered steps in the imperative mood, naming the real tools;
- quality checks and known pitfalls;
- stop conditions and what to report to the user.

Keep the file under about 100 lines. Put long reference material in `references/` and reusable code in `scripts/` inside the skill folder.

## Rules

- Stop when the requested skill works: `SKILL.md` plus only the scripts the workflow needs, and one smoke run. Do not add `tests/`, extra references, or improvements the user did not ask for; mention them as suggestions instead.
- No secrets, tokens, passwords or personal absolute paths; use `<project>` placeholders.
- Do not encode destructive commands; require confirmation for delete or overwrite steps.
- Read an existing skill before updating it and keep the parts that work.
- After writing, read the file back, check the frontmatter parses, list the target folder for name clashes, and tell the user the path plus an example request that triggers it.
