# PowerShell Script to Remove 'Open with Logos' from Windows Context Menu

# Registry path for the context menu entry
$appRegPath = "HKCU:\Software\Classes\Directory\shell\Logos"

# Check if the registry key exists
if (Test-Path $appRegPath) {
    # Remove the registry key and all its subkeys
    Remove-Item -Path $appRegPath -Recurse -Force
    Write-Host "Successfully removed 'Open with Logos' from the context menu."
} else {
    Write-Host "'Open with Logos' context menu entry not found. Nothing to remove."
}
