# File Search Guide (`@` mentions)

Typing `@` in the message input opens a file/folder suggestion popup for the
active project. Selecting a result inserts the path into the message and
attaches it to the request.

Search is **fuzzy path matching**, not glob/wildcard matching. Patterns such as
`*.py`, `?`, `[0-9]`, or `[!...]` are not expanded; those characters are treated
as ordinary characters and will usually produce no useful matches.

---

## Index

[`src/indexer.py`](../src/indexer.py) builds and maintains the search index.

- **Root:** the active project directory (`ProjectManager` root), not the Logos
  installation directory. Switching projects creates a new `Indexer`.
- **Contents:** relative paths using `/` as the separator. Directories are
  stored with a trailing `/`, and are sorted before files, alphabetically.
- **Initial build:** runs in a background thread when the indexer starts.
- **Updates:** `watchdog` observes the project tree; changes trigger a debounced
  rebuild (0.5 s). Automatic rebuilds are gated to Terminal mode via
  `is_active_cb`; the initial index and explicit rebuilds still run.
- **Ignore rules:**
  - hardcoded directories: `.git`, `.venv`, `venv`, `node_modules`,
    `__pycache__`, `.logos`, `screenshots`, `logs`, `cache`;
  - every `.gitignore` pattern found in the tree, including directory patterns
    such as `build/`.
- **Refresh:** when a rebuild finishes, an active search is re-run so the popup
  reflects new files.

The index contains paths only, not file contents. Full-text search is a separate
agent tool (`search_file_content`), not the `@` popup.

---

## Matching behavior

[`Indexer.search()`](../src/indexer.py) uses RapidFuzz:

```python
process.extract(
    search_term,
    search_space,
    processor=lambda choice: choice[len(base_path):],
    limit=200,
    score_cutoff=50,
)
```

- Results are ranked by fuzzy similarity, capped at **200** matches with a
  minimum score of **50**.
- The query is matched against the full relative path, so both file names and
  parent folders influence the score.
- An empty query (just `@`) returns the top-level entries of the current folder:
  directories first, then files, alphabetically.
- Matching is case-sensitive. `IND` does not match `indexer.py`; type the path
  with the same casing as on disk if a result is missing.

### Folders as filters

Typing a slash turns the text before it into a folder path:

```
@src/ind
```

The UI treats `src/` as the current folder and inserts it into the query. The
popup shows paths under that prefix; selecting a folder (`src/`) navigates into
it (the query becomes `@src/`), while selecting a file inserts the full path.

Implementation note: the UI emits both the query and a computed `base_path`, and
`Indexer.search()` supports a `base_path` argument, but the controller slot
(`AppController.search_files`) accepts only the query. In the current build the
folder prefix therefore narrows results through fuzzy scoring of the full path,
not through a separate prefix filter.

---

## Interaction

- `@` must start a word: it is recognized at the beginning of the input or after
  whitespace.
- Up/Down move the selection; Tab or Enter selects; Esc closes the popup.
- A file selection inserts `@relative/path ` and emits the path for attachment.
- A folder selection navigates into it. If the folder path is already fully
  typed, selecting it attaches the folder instead.
- The popup loads results in chunks of 30 as you scroll.
- Clicking outside or moving the caret out of the mention closes the popup.

Selecting a path does not read the file into the prompt by itself; it attaches
the path through the same attachment pipeline as paste/drag-and-drop.

---

## Practical queries

| Goal | Query |
| --- | --- |
| Find a file by name fragment | `indexer` |
| Find a file by full name | `main.py` |
| Narrow to a folder | `src/com` |
| Browse project root | `@` (empty query) |
| Browse a folder | `src/` |

Do not use `*.py` or other shell globs. To find Python files, type part of the
file name or browse the folder; the index already contains every non-ignored
file.
