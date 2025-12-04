# PowerShell Script to Add 'Open with Logos' to Windows Context Menu

# Get the script's own directory to locate the project root
$scriptPath = $MyInvocation.MyCommand.Path
$scriptDir = Split-Path -Parent $scriptPath
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")

# Define absolute paths for the python executable, the main script, and the icon.
# Using absolute paths is crucial for commands executed from the registry.
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$mainPyPath = Join-Path $projectRoot "main.py"
$iconPath = Join-Path $projectRoot "assets\ico.ico"

# Ensure the required files exist before proceeding
if (-not (Test-Path $pythonPath)) {
    Write-Host "Error: Python executable not found at '$pythonPath'. Please run the main setup script first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $mainPyPath)) {
    Write-Host "Error: main.py not found at '$mainPyPath'." -ForegroundColor Red
    exit 1
}

# Registry paths for folder context menu
# HKEY_CURRENT_USER (HKCU) is used to avoid requiring admin rights.
$regPath = "HKCU:\Software\Classes\Directory\shell\Logos\command"
$iconRegPath = "HKCU:\Software\Classes\Directory\shell\Logos"


# Create the registry keys. -Force creates parent keys if they don't exist.
New-Item -Path $regPath -Force | Out-Null

# Set the menu text and icon for the parent key
Set-ItemProperty -Path $iconRegPath -Name "(Default)" -Value "Open with Logos" -Force
if (Test-Path $iconPath) {
    Set-ItemProperty -Path $iconRegPath -Name "Icon" -Value $iconPath -Force
}

# Define the command string.
# Each path is enclosed in its own quotes. "%1" is also quoted to handle paths with spaces.
$command = "`"$pythonPath`" `"$mainPyPath`" `"%1`""

# Set the default value of the 'command' key to our command string
Set-ItemProperty -Path $regPath -Name "(Default)" -Value $command -Force

Write-Host "Successfully added 'Open with Logos' to the folder context menu." -ForegroundColor Green
Write-Host "To remove it, run '.\context_menu\remove_from_windows_context_menu.ps1'"
