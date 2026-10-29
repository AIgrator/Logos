---
name: file-organizer
description: Tidy a folder by proposing a safe move-and-rename plan, confirming it with the user, then applying it. Use when files are scattered across Downloads or Desktop.
---

# File organizer

Reorder a folder with an explicit, reversible plan. Never surprise the user.

## Workflow

1. Confirm the target folder and the goal (by type, by date, by project). If the user did not name a folder, propose one and wait.
2. Inventory with `list_directory` and `find_files`: names, sizes, dates, subfolders. Count the files first.
3. Build a table: current path to destination path, plus a "leave in place" group for hidden files, system files, open documents and anything ambiguous.
4. Ask for explicit confirmation of the plan. Do not move anything on a vague "ok" if the plan changed since it was shown.
5. Apply with `create_directory` and `move_files`. Never delete and never overwrite; rename with a numeric suffix on collision.
6. Report moved and skipped counts and provide the old-to-new list for rollback.

## Grouping defaults

- Documents, Images, Video, Audio, Archives, Installers, Code, Other.
- Keep existing project folders together; do not split a folder that is already organized.
- Files younger than 24 hours may still be in use; list them under "recent, left in place".

## Rules

- Stay inside the confirmed folder. Never touch the rest of the disk.
- Do not rename files inside archives, repositories or application data folders.
- Explain each rule you use and let the user change it before applying.
- If anything fails, stop and report what was already moved instead of continuing blindly.
