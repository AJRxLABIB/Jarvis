<#
  Optional: register Jarvis's background host as a real Windows service ("JarvisBackground").
  Run from an ADMINISTRATOR PowerShell. Nothing in Jarvis runs this for you.

    .\packaging\service.ps1 -Install     asks for your Windows account password (used once to register the service;
                                         it is not stored by Jarvis), installs and starts the service
    .\packaging\service.ps1 -Status      service state + what the host says (data\background.json)
    .\packaging\service.ps1 -Uninstall   stops and removes the service

  Use EITHER this OR the logon task (background.ps1), not both: they would fight over the one-engine lock.
  The service runs as YOUR account (Jarvis's secrets are sealed for it). The account needs "Log on as a service"
  (secpol.msc > Local Policies > User Rights Assignment). NOT run on a real PC by its author: read it first.
#>
param([switch]$Install, [switch]$Uninstall, [switch]$Status)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "jarvis_service.py"
$Name = "JarvisBackground"

if (-not ($Install -or $Uninstall -or $Status)) { Get-Help $PSCommandPath | Out-String | Write-Host; exit 0 }

if ($Status) {
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) { Write-Host "Service '$Name': $($svc.Status) (start type: $($svc.StartType))" } else { Write-Host "Service '$Name': not installed" }
    $state = Join-Path $Root "data\background.json"
    if (Test-Path $state) { $s = Get-Content $state -Raw | ConvertFrom-Json; Write-Host "Host: $($s.status), pid $($s.pid), heartbeat $($s.heartbeat)" } else { Write-Host "Host: never started" }
    exit 0
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this from an administrator PowerShell." }
if (-not (Test-Path $Python)) { throw "Not found: $Python. Create the .venv first (see SETUP.md)." }

if ($Uninstall) {
    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
        & $Python $Script stop 2>$null
        & $Python $Script remove
    } else { Write-Host "Service '$Name' was not installed." }
    exit 0
}

if (Get-ScheduledTask -TaskName "JarvisBackground" -ErrorAction SilentlyContinue) {
    throw "The logon task 'JarvisBackground' is installed. Remove it first: .\packaging\background.ps1 -Uninstall"
}
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Windows account the Jarvis service will run as (yours)"
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($cred.Password))
& $Python $Script --username $cred.UserName --password $plain --startup auto install
$plain = $null
& $Python $Script start
Write-Host "Installed and started '$Name'. Check it with: .\packaging\service.ps1 -Status"
