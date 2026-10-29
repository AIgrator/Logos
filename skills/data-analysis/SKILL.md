---
name: data-analysis
description: Profile and analyze local CSV, TSV or JSON data files and report findings. Use for spreadsheet exports, logs, survey data, and quick statistics.
---

# Data analysis

Profile a local dataset, answer the actual question with numbers, and keep the work reproducible.

## Workflow

1. Locate the files with `list_directory` or `find_files`. Confirm paths, sizes and delimiters; read the first rows before loading anything.
2. Check which runtime is available (`python --version`, `uv run python --version`, `node --version`). Use the first one that works; do not install packages.
3. Profile before answering: row and column counts, types, missing values, duplicates, ranges, top categories.
4. Run the smallest analysis that answers the question. Print intermediate results so the user can follow.
5. Verify: recompute key totals a second way (filtered count vs sum, sample rows) and check units.

## Rules

- Standard library only (`csv`, `json`, `statistics`, `collections`, `datetime`). Ask before installing anything or uploading data anywhere.
- Keep raw files untouched. Write a cleaned or result file only when the user asked for one, and never overwrite the input.
- For large files, stream or chunk; do not print megabytes of output.
- Report exact numbers with units and the time range they cover. Never estimate silently.
- Separate facts from hypotheses. If the data cannot answer the question, say what is missing.

## Report

- One-line answer.
- A compact markdown table for the key breakdowns.
- Short findings list, then caveats (missing data, ambiguous columns, timezone).
- The exact commands or snippets used, so the result can be reproduced.
