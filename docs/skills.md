# Skills

Settings → **Skills** shows the Agent Skills that Logos found for the active project and for the current user. A skill is a folder with a `SKILL.md` file — the open [Agent Skills](https://agentskills.io) format used by Claude Code, Codex, Cursor and Qoder. Skills written for those tools work in Logos without changes; there is nothing to convert, import or install through a marketplace.

Skills are used by **Terminal** and **Browser** only. Chat and Vision ignore them.

## What the agent sees

Only the `name` and `description` of each skill go into the system prompt (`### Available Skills ###` in the agent prompt), under a 6,000-character budget. When a task matches a description, the model calls the `use_skill` tool with that name and receives:

- the full text of `SKILL.md`;
- the absolute path of the skill directory;
- the list of auxiliary files (`scripts/`, `references/`, `assets/`, …), which the model reads on demand with `read_files`.

This is progressive disclosure: large reference files cost tokens only when a task actually needs them.

## Where Logos looks

Both scopes are scanned; the project is scanned first, then the user home. Inside a scope the first root that contains a given skill name wins, so a project skill overrides the user skill with the same name.

| Scope | Roots (in priority order) |
|---|---|
| Project | `<project>/.logos/skills`, `<project>/.agents/skills`, `<project>/.cursor/skills`, `<project>/.claude/skills`, `<project>/.codex/skills`, `<project>/.qoder/skills` |
| User | `~/.logos/skills`, `~/.agents/skills`, `~/.cursor/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.qoder/skills` |

The **Project Folder** and **User Folder** buttons open (and create) the Logos-native `.logos/skills` folder; **Reload** rescans everything after you copy a skill in. A project skill applies only to that project; a user skill is available in every project.

## Format

```
my-skill/
  SKILL.md
  references/guide.md
  scripts/run.py
```

`SKILL.md` starts with YAML frontmatter:

```markdown
---
name: my-skill
description: What the skill does and when the agent should use it. This is the only text shown in the prompt, so name the trigger tasks explicitly.
---

Step-by-step instructions the agent follows after loading the skill.
```

- `name` — lowercase letters, digits and hyphens; at most 64 characters. It is normalised from the value or, when absent, from the folder name.
- `description` — at most 1,024 characters. When absent, the first paragraph of the file is used. Keep the essential "what and when" in the first ~300 characters, which is what the prompt shows.
- `license`, `compatibility`, `metadata` and `allowed-tools` are accepted and ignored; skills written for other agents keep working.
- The `SKILL.md` body is capped at 64 KB per load and the auxiliary file list at 40 entries.

Other YAML frontmatter fields that do not fit the small built-in parser are skipped rather than rejecting the skill. A folder without a readable `SKILL.md` is ignored.

## Notes

- Skill folders are read in place; Logos never copies or rewrites them.
- Secrets that match the application's redaction rules are masked when the body is loaded.
- Skill instructions are followed like any other model instructions. Page text and skill files installed from untrusted sources cannot override the browser safety rules or the user's goal.
- A skill can point the model to scripts, but running them still requires the normal shell tools and the usual safety checks.
