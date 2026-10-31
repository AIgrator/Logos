@echo off
REM Global launcher installed by Logos Setup.
IF /I "%~1"=="-h" GOTO :HELP
IF /I "%~1"=="--help" GOTO :HELP
"%~dp0Logos.exe" %*
GOTO :EOF

:HELP
ECHO Usage: logos [optional_path_to_folder]
ECHO.
ECHO Launches the Logos AI Assistant. With a folder argument, Logos opens that
ECHO folder as the project; without one it opens the last active project.
ECHO.
ECHO Example: logos C:\Users\YourUser\Documents\MyProject
GOTO :EOF
