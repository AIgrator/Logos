# File Search Guide: Using Wildcards

The file search functionality (triggered by `@`) supports powerful wildcard characters (also known as glob patterns) to help you find files and directories more flexibly. Here is a complete list of the supported wildcards:

---

### 1. `*` (Asterisk)

Matches **any number of any characters** (including zero characters). This is the most flexible and commonly used wildcard.

-   **Example:** `*.py`
    -   Finds: `main.py`, `utils.py`, `test.py`
-   **Example:** `*config*`
    -   Finds: `main_config.json`, `config.ini`, `configuration/settings.py`
-   **Example:** `test_*`
    -   Finds: `test_api.py`, `test_users.py`

### 2. `?` (Question Mark)

Matches **exactly one of any character**.

-   **Example:** `log_?.txt`
    -   Finds: `log_1.txt`, `log_A.txt`, `log_9.txt`
    -   **Will not find:** `log_10.txt` (two characters), `log_.txt` (zero characters).

### 3. `[...]` (Square Brackets)

Matches a **single character from the set listed** inside the brackets. You can specify individual characters or a range.

-   **Example (individual characters):** `file[123].txt`
    -   Finds: `file1.txt`, `file2.txt`, `file3.txt`
    -   **Will not find:** `file4.txt`
-   **Example (digit range):** `img_[0-9].jpg`
    -   Finds: `img_0.jpg`, `img_1.jpg`, ..., `img_9.jpg`
-   **Example (letter range):** `[a-c]*.log`
    -   Finds: `a.log`, `b_error.log`, `c_system.log`

### 4. `[!...]` (Brackets with Exclamation Mark)

This is the inverse of the previous rule. It matches a **single character that is NOT listed** after the exclamation mark.

-   **Example:** `file[!0-9].txt`
    -   Finds: `fileA.txt`, `file_B.txt`, `file-final.txt`
    -   **Will not find:** `file1.txt`, `file2.txt`

---

### How to Find a Literal Wildcard Character

If you need to find a file that literally contains a wildcard character in its name, you must wrap it in square brackets.

-   To find `*`, use `[*]`
    -   **Example:** `my_file_[*].txt` will find the file named `my_file_*.txt`
-   To find `?`, use `[?]`
    -   **Example:** `version_[?].py` will find the file named `version_?.py`
-   To find `[`, use `[[]`
    -   **Example:** `data_[[]01].csv` will find the file named `data_[01].csv`
