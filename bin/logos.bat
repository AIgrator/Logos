@echo off
REM Launches the Logos AI Assistant from the repository venv

REM Check for help flag
IF /I "%1" == "-h" GOTO :HELP
IF /I "%1" == "--help" GOTO :HELP

REM Launch the Python application, passing all arguments
"%~dp0\..\.venv\Scripts\python.exe" "%~dp0\..\main.py" "%*"
GOTO :EOF

:HELP
ECHO Usage: logos [optional_path_to_folder]
ECHO
ECHO Launches the Logos AI Assistant.
ECHO If an optional folder path is provided, the assistant will start in that directory.
ECHO
ECHO Example: logos C:\Users\YourUser\Documents\MyProject
ECHO
ECHO Options:
ECHO   -h, --help    Show this help message and exit.
GOTO :EOF
