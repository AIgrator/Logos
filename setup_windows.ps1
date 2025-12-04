# PowerShell script for setting up the Gemini AI Assistant environment on Windows.
#
# This script automates the following steps:
# 1. Checks if Python is installed and accessible via the system's PATH.
# 2. Checks if the 'uv' package manager is installed. If not, it installs it.
# 3. Creates a virtual environment and installs all project dependencies using 'uv'.

# --- Script Configuration ---
# Stop script execution on any error
$ErrorActionPreference = "Stop"

# --- Colors for output ---
$Green = "Green"
$Yellow = "Yellow"
$Red = "Red"
$NC = "Gray" # PowerShell's default color

# Get the absolute path of the directory where the script is located
$scriptRoot = $PSScriptRoot | Resolve-Path
$pythonExe = Join-Path -Path $scriptRoot -ChildPath ".venv\Scripts\python.exe"
$mainPy = Join-Path -Path $scriptRoot -ChildPath "main.py"
$setupChoicesFile = Join-Path -Path $scriptRoot -ChildPath "setup_choices.json"

# --- Start of Script ---
Write-Host "--- Starting setup script for Gemini AI Assistant on Windows ---" -ForegroundColor $Green

# --- 1. Check and install uv ---
Write-Host "`nStep 1: Checking for 'uv' package manager..." -ForegroundColor $Yellow
$uvExists = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvExists) {
    Write-Host "'uv' not found. Installing it now..."
    try {
        # Download and execute the official installer script
        irm https://astral.sh/uv/install.ps1 | iex
        Write-Host "'uv' installed successfully." -ForegroundColor $Green
        
        # The installer adds uv to the user's PATH, but it's not available in the current session.
        # We add it manually for this session to proceed without restarting the terminal.
        $uvPath = "$([Environment]::GetFolderPath('UserProfile'))\.uv\bin"
        $env:PATH += ";$uvPath"
        Write-Host "Temporarily added 'uv' to PATH for this session." -ForegroundColor $Yellow
    }
    catch {
        Write-Host "Error: Failed to install 'uv'." -ForegroundColor $Red
        Write-Host $_.Exception.Message -ForegroundColor $Red
        exit 1
    }
}
else {
    Write-Host "'uv' is already installed. Skipping installation." -ForegroundColor $Green
}

# --- 2. Install Python and project dependencies ---
Write-Host "`nStep 2: Installing Python (if needed) and project dependencies..." -ForegroundColor $Yellow
if (-not (Test-Path "pyproject.toml")) {
    Write-Host "Error: pyproject.toml not found in the current directory." -ForegroundColor $Red
    Write-Host "Please run this script from the root directory of the project." -ForegroundColor $Red
    exit 1
}
else {
    Write-Host "'pyproject.toml' found. Creating virtual environment and installing packages..."
    Write-Host "This may take a few moments as 'uv' might need to download a Python version..."
    try {
        # Use uv to create venv and install packages from pyproject.toml
        # 'uv' will automatically download and install Python 3.12.11 if it's not found.
        uv sync --python 3.12.11
        Write-Host "Virtual environment created and dependencies installed successfully." -ForegroundColor $Green
    }
    catch {
        Write-Host "Error: Failed to sync dependencies with 'uv'." -ForegroundColor $Red
        Write-Host $_.Exception.Message -ForegroundColor $Red
        exit 1
    }
}

# --- 3. Run Python Setup Wizard ---
Write-Host "`nStep 3: Launching initial setup wizard..." -ForegroundColor $Yellow

# Check if the Python executable exists after uv sync
if (-not (Test-Path $pythonExe)) {
    Write-Host "Error: Python executable not found at '$pythonExe'. Setup cannot proceed." -ForegroundColor $Red
    exit 1
}

Write-Host "Launching the GUI setup wizard. Please complete the steps in the new window."
$setupProcess = Start-Process -FilePath $pythonExe -ArgumentList "`"$mainPy`" --run-setup" -Wait -PassThru
$exitCode = $setupProcess.ExitCode

if ($exitCode -eq 0) {
    Write-Host "Setup wizard completed successfully." -ForegroundColor $Green
} else {
    Write-Host "Setup wizard was cancelled by the user. Skipping further integration." -ForegroundColor $Yellow
    exit 0
}

# --- 4. Process Setup Choices and Finalize Installation ---
Write-Host "`nStep 4: Applying final setup choices..." -ForegroundColor $Yellow
$createGlobalCommand = $false
$addContextMenu = $false
$createDesktopShortcut = $false

if (Test-Path $setupChoicesFile) {
    try {
        $choices = Get-Content $setupChoicesFile | ConvertFrom-Json
        $createGlobalCommand = $choices.create_global_command
        $addContextMenu = $choices.add_context_menu
        $createDesktopShortcut = $choices.create_desktop_shortcut
        Write-Host "Setup choices loaded from '$setupChoicesFile'." -ForegroundColor $Green
    }
    catch {
        Write-Host "Error: Failed to read setup choices from '$setupChoicesFile'. Defaulting to no additional setup." -ForegroundColor $Red
        Write-Host $_.Exception.Message -ForegroundColor $Red
    }
} else {
    Write-Host "Warning: Setup choices file '$setupChoicesFile' not found. Defaulting to no additional setup." -ForegroundColor $Yellow
}

# Create global launcher if selected
if ($createGlobalCommand) {
    Write-Host "`nCreating a global 'logos' command..."
    $binDir = Join-Path -Path $scriptRoot -ChildPath "bin"
    $launcherPath = Join-Path -Path $binDir -ChildPath "logos.bat"

    # Create bin directory if it doesn't exist
    if (-not (Test-Path $binDir)) {
        New-Item -ItemType Directory -Path $binDir | Out-Null
        Write-Host "Created 'bin' directory."
    }

    # Create the launcher .bat file content
    $launcherContent = @"
@echo off
REM Dynamically launches the Gemini AI Assistant

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
"@
    $launcherContent | Set-Content -Path $launcherPath -Encoding Ascii

    Write-Host "Created 'logos.bat' launcher in 'bin' directory." -ForegroundColor $Green

    # Add bin directory to user's PATH if it's not already there
    Write-Host "Adding 'bin' directory to the user's PATH..."
    $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$binDir*") {
        $newUserPath = "$userPath;$binDir"
        [System.Environment]::SetEnvironmentVariable("PATH", $newUserPath, "User")
        Write-Host "'bin' directory has been added to your PATH." -ForegroundColor $Green
        Write-Host "Please restart your terminal (PowerShell, CMD) for the 'logos' command to be available." -ForegroundColor $Yellow
    } else {
        Write-Host "'bin' directory is already in your PATH. No changes needed." -ForegroundColor $Green
    }
} else {
    Write-Host "Skipping creation of global 'logos' command." -ForegroundColor $Yellow
}

# Add to Windows Context Menu if selected
if ($addContextMenu) {
    Write-Host "`nAdding 'Open with Logos' to Windows context menu..."
    $addContextMenuScript = Join-Path -Path $scriptRoot -ChildPath "context_menu\add_to_windows_context_menu.ps1"
    if (Test-Path $addContextMenuScript) {
        Write-Host "Running context menu integration script..." -ForegroundColor $Yellow
        try {
            # Execute the script to add the context menu item
            powershell -ExecutionPolicy Bypass -File $addContextMenuScript
        }
        catch {
            Write-Host "Error: Failed to add context menu item." -ForegroundColor $Red
            Write-Host $_.Exception.Message -ForegroundColor $Red
        }
    }
    else {
        Write-Host "Error: 'add_to_windows_context_menu.ps1' not found in 'context_menu' directory." -ForegroundColor $Red
    }
} else {
    Write-Host "Skipping context menu integration." -ForegroundColor $Yellow
}

# Create Desktop Shortcut if selected
if ($createDesktopShortcut) {
    Write-Host "`nCreating Desktop Shortcut..."
    $desktopPath = [System.Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path -Path $desktopPath -ChildPath "Logos AI Assistant.lnk"
    
    # Create a shell object to create the shortcut
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    
    # Determine the target path
    if ($createGlobalCommand) {
        # If the global command is created, the .bat file is the most reliable target
        $targetPath = Join-Path -Path $scriptRoot -ChildPath "bin\logos.bat"
    } else {
        # Otherwise, point directly to the python executable
        $targetPath = $pythonExe
    }
    
    $shortcut.TargetPath = $targetPath
    
    # If not using the .bat file, we need to add main.py as an argument
    if (-not $createGlobalCommand) {
        $shortcut.Arguments = "`"$mainPy`""
    }
    
    $shortcut.IconLocation = Join-Path -Path $scriptRoot -ChildPath "assets\ico.ico"
    $shortcut.Description = "Launch the Logos AI Assistant"
    $shortcut.WorkingDirectory = $scriptRoot
    
    $shortcut.Save()
    Write-Host "Desktop shortcut created successfully at '$shortcutPath'." -ForegroundColor $Green
} else {
    Write-Host "Skipping creation of Desktop Shortcut." -ForegroundColor $Yellow
}

# Cleanup: Remove the setup_choices.json file
if (Test-Path $setupChoicesFile) {
    Remove-Item $setupChoicesFile -Force
    Write-Host "`nCleaned up temporary file: '$setupChoicesFile'." -ForegroundColor $Green
}

# --- Final Completion Message ---
Write-Host "`n--- Setup complete! ---" -ForegroundColor $Green
Write-Host "You can now run the application:"
Write-Host "1. From your desktop/start menu (if context menu option was chosen)." -ForegroundColor $Yellow
Write-Host "2. Using the global command 'logos [optional_path_to_folder]' (if global command was chosen, requires terminal restart)." -ForegroundColor $Yellow
Write-Host "3. Manually by activating the environment and running main.py:" -NoNewline -ForegroundColor $NC
Write-Host ".\.venv\Scripts\activate" -ForegroundColor $Yellow
Write-Host "python main.py [optional_path_to_folder]" -ForegroundColor $Yellow
