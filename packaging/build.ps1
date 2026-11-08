<#
Builds JARVIS.exe into dist\JARVIS.

  powershell -ExecutionPolicy Bypass -File packaging\build.ps1            # test, build
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install   # ...and install for this user
  -SkipTests   build without running the test suite first
  -SkipGpu     don't install/bundle the NVIDIA GPU speech libraries (CPU speech recognition only)
  -Android     also (re)build the Jarvis Wake phone app first (packaging\build_android.ps1)
  -Installer   also build dist\JarvisSetup-<version>.exe (Inno Setup 6) and dist\update.json for update checks
               Code signing (optional): set JARVIS_SIGN_CERT (a .pfx file) and JARVIS_SIGN_PASSWORD

Run it from the project's virtual environment (.venv), which has all requirements installed.
#>
param([switch]$Install, [switch]$SkipTests, [switch]$SkipGpu, [switch]$Installer, [switch]$Android)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

Step "Checking Python"
python -c "import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is needed (activate .venv first)." }

Step "Installing build requirements"
python -m pip install --disable-pip-version-check -q -r requirements.txt "pyinstaller>=6.6,<7"
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# NVIDIA GPU: add the CUDA libraries so speech recognition runs on the GPU (large-v3-turbo).
if (-not $SkipGpu -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Step "NVIDIA GPU found: installing the GPU speech libraries (about 1 GB download, only the first time)"
    python -m pip install --disable-pip-version-check --progress-bar on -r requirements-gpu.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "GPU libraries could not be installed; speech recognition will use the CPU." -ForegroundColor Yellow }
}

if (-not $SkipTests) {
    Step "Running the tests"
    # each build gets its own temporary folder: two builds at once (or an admin and a normal window) can't collide
    $testTemp = Join-Path $env:TEMP "jarvis-build-tests-$PID"
    $junit = Join-Path $env:TEMP "jarvis-build-junit-$PID.xml"
    python -m pytest -q -p no:cacheprovider --basetemp "$testTemp" --junitxml "$junit"
    $testsExit = $LASTEXITCODE
    Remove-Item -Recurse -Force $testTemp -ErrorAction SilentlyContinue
    if ($testsExit -ne 0) {
        # Every test may have passed while the Python process still exited with an error code (a crash while shutting down).
        # The junit report says which it was: a real failure stops the build, a bad exit after a clean run only warns.
        $bad = -1
        try {
            [xml]$report = Get-Content -Raw $junit
            $suite = @($report.testsuites.testsuite)[0]
            $bad = [int]$suite.failures + [int]$suite.errors
            $ran = [int]$suite.tests
        } catch { }
        if ($bad -eq 0 -and $ran -gt 1000) {
            Write-Host "All $ran tests passed, but Python exited with code $testsExit after them (a crash at shutdown, not a test failure). Continuing." -ForegroundColor Yellow
        } else {
            throw "Tests failed (exit code $testsExit, $bad failing), so nothing was built. Use -SkipTests to build anyway."
        }
    }
    Remove-Item -Force $junit -ErrorAction SilentlyContinue
}

if ($Android) {
    Step "Building the Jarvis Wake phone app"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_android.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Host "The phone app wasn't built (see above); Jarvis is built without it." -ForegroundColor Yellow }
}

Step "Preparing the icon and version details"
python packaging\make_icon.py | Out-Null
python packaging\make_version_info.py | Out-Null

Step "Building (this takes a few minutes)"
python -m PyInstaller packaging\jarvis.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Step "Checking the build"
$cli = Join-Path $root "dist\JARVIS\jarvis-cli.exe"
& $cli --version
if ($LASTEXITCODE -ne 0) { throw "The built program does not start." }
& $cli --tools | Select-Object -First 1
$size = (Get-ChildItem -Recurse (Join-Path $root "dist\JARVIS") | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Built dist\JARVIS ({0:N0} MB)" -f $size) -ForegroundColor Green

function Sign-File($path) {
    if (-not $env:JARVIS_SIGN_CERT) { return }
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { Write-Host "signtool.exe not found (Windows SDK); not signed: $path" -ForegroundColor Yellow; return }
    & $signtool.FullName sign /f $env:JARVIS_SIGN_CERT /p $env:JARVIS_SIGN_PASSWORD /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $path
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $path" }
}

if ($Installer) {
    Step "Building the installer (Inno Setup)"
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 is not installed: winget install JRSoftware.InnoSetup" }
    $version = (& $cli --version).Split(" ")[-1]
    Sign-File (Join-Path $root "dist\JARVIS\JARVIS.exe")
    Sign-File (Join-Path $root "dist\JARVIS\jarvis-cli.exe")
    & $iscc "/DAppVersion=$version" (Join-Path $PSScriptRoot "jarvis.iss") | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
    $setup = Join-Path $root "dist\JarvisSetup-$version.exe"
    Sign-File $setup
    $hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    $manifest = [ordered]@{ version = $version; url = "https://REPLACE-WITH-YOUR-DOWNLOAD-URL/JarvisSetup-$version.exe";
                            sha256 = $hash; notes = "Jarvis $version";
                            signed = [bool]$env:JARVIS_SIGN_CERT }  # signed releases: unsigned downloads are refused
    $manifest | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $root "dist\update.json")
    Write-Host "Installer: $setup" -ForegroundColor Green
    Write-Host "Update manifest: dist\update.json (set its url to where you upload the installer)"
}

if ($Install) { & (Join-Path $PSScriptRoot "install.ps1") }
else { Write-Host "Install it with:  powershell -ExecutionPolicy Bypass -File packaging\install.ps1" }
