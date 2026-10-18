<#
  Optional: run Jarvis's scheduled automations without the window.

  Nothing in Jarvis runs this for you. It does exactly what you ask and nothing else:

    .\packaging\background.ps1 -Install     registers ONE per-user task "JarvisBackground" that starts
                                            `run.py --background` when YOU sign in (no administrator needed)
    .\packaging\background.ps1 -Status      shows whether the task exists and whether the host is running
    .\packaging\background.ps1 -Uninstall   removes the task (and asks a running host to stop)

  Background mode never shows dialogs: any step that needs your approval is refused there. Opening the Jarvis window
  asks the background host to stop and takes over. This script has NOT been run on a real Windows PC by its author;
  read it first and try -Status before -Install.
#>
param([switch]$Install, [switch]$Uninstall, [switch]$Status)

$ErrorActionPreference = "Stop"
$TaskName = "JarvisBackground"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"   # pythonw: no console window
$StateFile = Join-Path $Root "data\background.json"

function Show-State {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-Host "Scheduled task '$TaskName': installed ($($task.State))" } else { Write-Host "Scheduled task '$TaskName': not installed" }
    if (Test-Path $StateFile) {
        $s = Get-Content $StateFile -Raw | ConvertFrom-Json
        Write-Host "Host state: $($s.status), pid $($s.pid), last heartbeat $($s.heartbeat)"
    } else { Write-Host "Host state: never started" }
}

if (-not ($Install -or $Uninstall -or $Status)) { Get-Help $PSCommandPath | Out-String | Write-Host; exit 0 }

if ($Status) { Show-State; exit 0 }

if ($Uninstall) {
    $control = Join-Path $Root "data\background.control"
    if (Test-Path $StateFile) { '{"command": "stop"}' | Set-Content -Path $control -Encoding UTF8 }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the scheduled task '$TaskName'."
    } else { Write-Host "The scheduled task '$TaskName' was not installed." }
    exit 0
}

if ($Install) {
    if (-not (Test-Path $Python)) { throw "Not found: $Python. Create the .venv first (see SETUP.md)." }
    $action = New-ScheduledTaskAction -Execute $Python -Argument "run.py --background" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited `
        -Description "Jarvis scheduled automations without the window (installed by you with background.ps1)" | Out-Null
    Write-Host "Installed '$TaskName' for $env:USERNAME: it starts when you sign in. Remove it with -Uninstall."
}
