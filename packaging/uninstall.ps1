<#
Removes the installed Jarvis: program folder, shortcuts and the Start-with-Windows entry.
Your data (%LOCALAPPDATA%\JarvisAssistant: settings, memories, logs, models) is kept unless you add -RemoveData.
#>
param([switch]$RemoveData)
$ErrorActionPreference = "Stop"
$programDir = Join-Path $env:LOCALAPPDATA "Programs\Jarvis"
$dataDir = Join-Path $env:LOCALAPPDATA "JarvisAssistant"

Get-Process -Name "JARVIS", "jarvis-cli" -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "JarvisAssistant" -ErrorAction SilentlyContinue
foreach ($link in @((Join-Path ([Environment]::GetFolderPath("Programs")) "Jarvis.lnk"),
                    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Jarvis.lnk"))) {
    if (Test-Path $link) { Remove-Item $link }
}
if (Test-Path $programDir) { Remove-Item -Recurse -Force $programDir }
Write-Host "Jarvis was removed."
if ($RemoveData) {
    if (Test-Path $dataDir) { Remove-Item -Recurse -Force $dataDir }
    Write-Host "Your Jarvis data was deleted too."
} else {
    Write-Host "Your settings, memories and models are still in $dataDir (use -RemoveData to delete them)."
}
