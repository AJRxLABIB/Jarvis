<#
Installs the built app for the current user (no administrator rights needed):

  program   %LOCALAPPDATA%\Programs\Jarvis\JARVIS.exe
  data      %LOCALAPPDATA%\JarvisAssistant  (settings, memories, logs, models)
  shortcuts Start Menu and Desktop ("Jarvis")

On the first install, your settings (.env), config files, memories and downloaded models are
copied from this project folder, so the installed app behaves exactly like `python run.py`.
Existing installed data is never overwritten. -NoImport skips that; -NoDesktop skips the desktop shortcut.
#>
param([switch]$NoImport, [switch]$NoDesktop, [switch]$NoStart)
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$source = Join-Path $project "dist\JARVIS"
$programDir = Join-Path $env:LOCALAPPDATA "Programs\Jarvis"
$dataDir = Join-Path $env:LOCALAPPDATA "JarvisAssistant"
$exe = Join-Path $programDir "JARVIS.exe"

if (-not (Test-Path (Join-Path $source "JARVIS.exe"))) { throw "Build it first: packaging\build.ps1" }

# Close every program running from the install folder (JARVIS.exe, jarvis-cli.exe, ...),
# otherwise their files are locked and the copy cannot replace them.
function Test-UsesInstallDir($proc) {
    if ($proc.Name -in @("JARVIS", "jarvis-cli")) { return $true }
    try {
        if ($proc.Path -and $proc.Path.StartsWith($programDir, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        # Any process that loaded a DLL from the install folder also locks it.
        foreach ($m in $proc.Modules) {
            if ($m.FileName -and $m.FileName.StartsWith($programDir, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        }
    } catch {}
    return $false
}
function Get-InstalledProcesses {
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID -and (Test-UsesInstallDir $_) }
}
$running = @(Get-InstalledProcesses)
if ($running.Count -gt 0) {
    Write-Host "Closing the running Jarvis ($($running.Name -join ', '))..."
    $running | ForEach-Object { try { $_.CloseMainWindow() | Out-Null } catch {} }
    Start-Sleep -Seconds 3
    Get-InstalledProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-InstalledProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    $left = @(Get-InstalledProcesses)
    if ($left.Count -gt 0) {
        throw "Jarvis is still running ($($left.Name -join ', ')). Exit it from the tray icon or Task Manager, then run packaging\install.ps1 again."
    }
}

Write-Host "Copying the program to $programDir (this can take a few minutes)..."
New-Item -ItemType Directory -Force -Path $programDir | Out-Null
# /R:2 /W:2 - retry a locked file twice, 2 s apart, instead of robocopy's default of a million retries.
# *.jarvis-old are files set aside by an earlier install because they were still in use.
Get-ChildItem $programDir -Recurse -Filter "*.jarvis-old" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
$log = Join-Path $env:TEMP "jarvis-install-robocopy.log"
function Invoke-Copy {
    robocopy $source $programDir /MIR /R:2 /W:2 /NDL /NJH /NP /XF "*.jarvis-old" /LOG:$log | Out-Null
    return $LASTEXITCODE
}
function Get-LockedFiles {
    Select-String -Path $log -Pattern "ERROR 32 .*Copying File (.+)$" -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Sort-Object -Unique
}
$code = Invoke-Copy
if ($code -ge 8) {
    # Windows lets a file that is in use be renamed, just not overwritten: move each locked file
    # aside (it is deleted on the next install) and copy again.
    $locked = @(Get-LockedFiles)
    foreach ($file in $locked) {
        $target = Join-Path $programDir $file.Substring($source.Length).TrimStart("\")
        Write-Host "In use, setting aside: $target" -ForegroundColor Yellow
        try { Rename-Item $target ("{0}.{1}.jarvis-old" -f (Split-Path $target -Leaf), [DateTime]::Now.Ticks) -ErrorAction Stop }
        catch { Write-Host "  could not rename it: $($_.Exception.Message)" -ForegroundColor Yellow }
    }
    if ($locked.Count -gt 0) { $code = Invoke-Copy }
}
if ($code -ge 8) {
    $failed = Select-String -Path $log -Pattern "ERROR" -ErrorAction SilentlyContinue |
        Select-Object -First 5 | ForEach-Object { $_.Line.Trim() }
    Write-Host ($failed -join "`n") -ForegroundColor Yellow
    throw "Copying failed (robocopy exit code $code). A file is in use - restart the PC (or sign out and in), then run packaging\install.ps1 again. Full log: $log"
}

if (-not $NoImport -and -not (Test-Path (Join-Path $dataDir ".env"))) {
    Write-Host "First install: copying your settings, memories and models from $project"
    New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "data") | Out-Null
    if (Test-Path (Join-Path $project ".env")) { Copy-Item (Join-Path $project ".env") $dataDir }
    if (Test-Path (Join-Path $project "config")) {
        robocopy (Join-Path $project "config") (Join-Path $dataDir "config") *.json /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    $db = Join-Path $project "data\assistant.db"
    if (Test-Path $db) { Copy-Item $db (Join-Path $dataDir "data") }
    $models = Join-Path $project "data\models"
    if (Test-Path $models) {
        robocopy $models (Join-Path $dataDir "data\models") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    $envFile = Join-Path $dataDir ".env"
    if (Test-Path $envFile) {  # a project folder path only makes sense for development; keep it if set
        Write-Host "Settings copied to $envFile"
    }
}

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $exe
    $link.WorkingDirectory = $programDir
    $link.IconLocation = "$exe,0"
    $link.Description = "Jarvis Assistant"
    $link.Save()
}
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Jarvis.lnk"
New-Shortcut $startMenu
if (-not $NoDesktop) { New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Jarvis.lnk") }

# If "Start with Windows" was on for the source version, point it at the installed program.
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Get-ItemProperty -Path $run -Name "JarvisAssistant" -ErrorAction SilentlyContinue) {
    Set-ItemProperty -Path $run -Name "JarvisAssistant" -Value "`"$exe`""
    Write-Host "Start with Windows now starts the installed Jarvis."
}

Write-Host "`nInstalled. Start it from the Start Menu or the desktop: Jarvis" -ForegroundColor Green
Write-Host "Diagnostics: `"$programDir\jarvis-cli.exe`" --doctor"
if (-not $NoStart) { Start-Process $exe }
