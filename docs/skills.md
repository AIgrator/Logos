# Skills

Settings → **Skills** shows the Agent Skills that Logos found for the active project, for the current user and for the application itself. A skill is a folder with a `SKILL.md` file — the open [Agent Skills](https://agentskills.io) format used by Claude Code, Codex, Cursor and Qoder. Skills written for those tools work in Logos without changes.

Logos ships a small built-in set, so the agents can use common workflows without any setup. Additional skills can be installed from the built-in catalogue or copied in by hand.

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
| Built-in | the `skills` folder shipped inside the application (read-only, scanned last) |

The **Project Folder** and **User Folder** buttons open (and create) the Logos-native `.logos/skills` folder; **Reload** rescans everything after you copy a skill in. A project skill applies only to that project; a user skill is available in every project.

## Built-in skills

The application bundles four skills and scans them after the project and user folders, so they are always available without setup:

- `web-research` — research a question across several sources and answer with citations.
- `data-analysis` — profile and analyze local CSV, TSV or JSON files.
- `file-organizer` — propose, confirm and apply a folder cleanup plan.
- `skill-creator` — turn a repeated workflow into a new `SKILL.md`.

Built-ins are read-only and update with the application. They are never copied into your folders. A project or user skill with the same name simply overrides the built-in one, which is also how you can customize a bundled workflow.

## Installing from the catalogue

Settings → Skills → **Browse Catalogue...** opens the public [ClawHub](https://clawhub.ai) registry without an API key. The dialog lists the most downloaded skills and searches by name or task.

Before installing, Logos shows the author reference, download count, license and source URL. A skill whose author did not declare a license is marked as such. **Install** downloads the skill archive and unpacks it into `~/.logos/skills/<slug>`, where normal discovery picks it up for the next Terminal or Browser request. Installing again replaces the folder after confirmation.

The catalogue needs an internet connection. Only ClawHub is connected; skills from other registries can still be copied into any of the roots above. Catalogue skills are third-party instructions: install only what you trust, and read the `SKILL.md` if in doubt.

## Try it

A small dataset ships with the repository at `examples/skills-demo/sales.csv` (12 sales rows). The agents resolve relative paths against the active project, so open the repository as the active project (or use the absolute path to the CSV, or copy it into the active project). Then switch to **Terminal** and ask:

> Analyze examples/skills-demo/sales.csv and show revenue by region, the best month and the best product.

Expected result:

- a `Loading skill · data-analysis` card appears while the agent loads the built-in skill;
- total revenue is 29,400;
- regions: East 7,350, North 7,100, South 6,900, West 8,050;
- best month: 2026-02 (16,450); best product: Widget (15,900).

If the skill card does not appear, repeat the request with "Use the data-analysis skill" to verify the loading path directly. The visible card and the numbers are the actual check.

To test the catalogue, open Settings → Skills → **Browse Catalogue...**, search `markdown`, and install `ivangdavila/markdown` (MIT-0). The confirmation shows the source, downloads and license; after installing, the skill appears in the list and lives in `~/.logos/skills/markdown`. To check the license warning instead, search `pdf` and select the top entry: it reports a proprietary license and should not be installed for testing.

To test skill creation, ask the Terminal agent to "create a skill called weekly-report that summarizes ...". The `skill-creator` skill writes `<project>/.logos/skills/weekly-report/SKILL.md`; the new skill appears in Settings and can be loaded on the next request.

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

- Existing skill folders are read in place. Logos writes only when you install a skill from the catalogue.
- Secrets that match the application's redaction rules are masked when the body is loaded.
- Skill instructions are followed like any other model instructions. Page text and skill files installed from untrusted sources cannot override the browser safety rules or the user's goal.
- A skill can point the model to scripts, but running them still requires the normal shell tools and the usual safety checks.
