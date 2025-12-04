"""
This module contains a comprehensive list of default file and folder patterns
to be ignored when scanning directories. This list is based on common patterns
found in GitHub's .gitignore templates for various languages, frameworks,
and operating systems.
"""

DEFAULT_GLOBAL_IGNORE_PATTERNS = [
    # OS-specific generated files
    '.DS_Store',
    'Thumbs.db',
    'ehthumbs.db',
    'Desktop.ini',
    '.Trash-*//',
    '.localized',

    # Git artifacts
    '.git/',
    '.gitignore',
    '.gitattributes',
    '.gitmodules',
    '.gitkeep',
    '.svn/',
    '.hg/',

    # Python
    '__pycache__/',
    '*.pyc',
    '*.pyo',
    '*.pyd',
    '.venv/',
    'venv/',
    'env/',
    '.python-version',
    '.mypy_cache/',
    '.pytest_cache/',
    'htmlcov/',
    '.coverage',
    '.coverage.*',
    '.nox/',
    '.tox/',
    'dist/',
    'build/',
    '*.egg-info/',
    '.ipynb_checkpoints/',

    # Node.js
    'node_modules/',
    'package-lock.json',
    'yarn.lock',
    'npm-debug.log*',
    'yarn-debug.log*',
    'yarn-error.log*',
    '.pnp/',
    '.pnp.js',

    # Java
    '*.class',
    '*.jar',
    '*.war',
    '*.ear',
    'target/',
    '.gradle/',

    # C/C++
    '*.o',
    '*.obj',
    '*.ko',
    '*.lo',
    '*.la',
    '*.so',
    '*.dll',
    '*.exe',
    '*.out',
    '*.a',
    '*.lib',

    # IDE and editor files
    '.idea/',
    '.vscode/',
    '.sublime-workspace',
    '.project',
    '.settings/',
    '*.swp',
    '*~',
    '.history/',
    '.vs/',

    # Logs and databases
    '*.log',
    '*.temp',
    '*.tmp',
    '*.sqlite',
    '*.db',
    '*.sqlite3-journal',

    # Compression
    '*.zip',
    '*.tar',
    '*.gz',
    '*.rar',
    '*.7z',

    # Misc
    '*.bak',
    '*.swo',
    '*.swn',
]
