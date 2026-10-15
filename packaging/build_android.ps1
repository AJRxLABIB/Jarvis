<#
Builds the JARVIS Mobile Android app (android\JarvisMobile) into app\remote\assets\JarvisMobile.apk, using only the
official tools: Microsoft Build of OpenJDK 17 and Google's Android SDK command-line tools.

The first run downloads them (about 400 MB) into %LOCALAPPDATA%\JarvisAndroidBuild and accepts the Android SDK
licenses for you (you're running this, so read them at https://developer.android.com/studio/terms if you want).
Later runs reuse them and take a few seconds. No Gradle, no Android Studio.

The app is signed with a key kept in android\signing (made on the first run). Keep that folder: an update of the
app installs over the old one only when it's signed with the same key.

    powershell -ExecutionPolicy Bypass -File packaging\build_android.ps1
#>
param([switch]$Clean)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x faster without the progress bar

$root = Split-Path -Parent $PSScriptRoot
$appName = if ($env:JARVIS_ANDROID_APP) { $env:JARVIS_ANDROID_APP } else { "JarvisMobile" }
$app = Join-Path $root "android\$appName"
$tools = Join-Path $env:LOCALAPPDATA "JarvisAndroidBuild"
$sdk = Join-Path $tools "sdk"
$out = Join-Path $root "app\remote\assets\$appName.apk"
$work = Join-Path $tools "work"
$JdkUrl = "https://aka.ms/download-jdk/microsoft-jdk-17-windows-x64.zip"
$SdkUrl = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
$Platform = "android-34"
$BuildTools = "34.0.0"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($exe)) failed (exit code $LASTEXITCODE)" }
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null
if ($Clean -and (Test-Path $work)) { Remove-Item -Recurse -Force $work }

# ---------------------------------------------------------------- Java 17
$jdk = Get-ChildItem -Directory -Path (Join-Path $tools "jdk") -Filter "jdk-17*" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jdk) {
    Step "Downloading Microsoft Build of OpenJDK 17 (about 190 MB, once)"
    $zip = Join-Path $tools "jdk.zip"
    Invoke-WebRequest -Uri $JdkUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tools "jdk") -Force
    Remove-Item $zip
    $jdk = Get-ChildItem -Directory -Path (Join-Path $tools "jdk") -Filter "jdk-17*" | Select-Object -First 1
}
$env:JAVA_HOME = $jdk.FullName
$env:PATH = "$($jdk.FullName)\bin;$env:PATH"
$javac = Join-Path $jdk.FullName "bin\javac.exe"
$keytool = Join-Path $jdk.FullName "bin\keytool.exe"

# ---------------------------------------------------------------- Android SDK (command-line tools only)
$sdkmanager = Join-Path $sdk "cmdline-tools\latest\bin\sdkmanager.bat"
if (-not (Test-Path $sdkmanager)) {
    Step "Downloading the Android SDK command-line tools (about 150 MB, once)"
    $zip = Join-Path $tools "cmdline-tools.zip"
    Invoke-WebRequest -Uri $SdkUrl -OutFile $zip -UseBasicParsing
    $unpack = Join-Path $tools "cmdline-unpack"
    Expand-Archive -Path $zip -DestinationPath $unpack -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $sdk "cmdline-tools") | Out-Null
    Move-Item -Force (Join-Path $unpack "cmdline-tools") (Join-Path $sdk "cmdline-tools\latest")
    Remove-Item -Recurse -Force $unpack, $zip
}
$bt = Join-Path $sdk "build-tools\$BuildTools"
$androidJar = Join-Path $sdk "platforms\$Platform\android.jar"
if (-not (Test-Path $androidJar) -or -not (Test-Path (Join-Path $bt "aapt2.exe"))) {
    Step "Installing Android platform $Platform and build-tools $BuildTools (once)"
    $yes = ("y`n" * 20)
    $yes | & $sdkmanager "--sdk_root=$sdk" --licenses | Out-Null
    Run $sdkmanager "--sdk_root=$sdk" "platforms;$Platform" "build-tools;$BuildTools"
}
$aapt2 = Join-Path $bt "aapt2.exe"
$d8 = Join-Path $bt "d8.bat"
$zipalign = Join-Path $bt "zipalign.exe"
$apksigner = Join-Path $bt "apksigner.bat"

# ---------------------------------------------------------------- build
Step "Building $appName"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force -Path "$work\classes", "$work\dex" | Out-Null

Run $aapt2 compile --dir (Join-Path $app "res") -o "$work\res.zip"
Run $aapt2 link -o "$work\base.apk" -I $androidJar --manifest (Join-Path $app "AndroidManifest.xml") "$work\res.zip" `
    --min-sdk-version 24 --target-sdk-version 34 --auto-add-overlay

$sources = Get-ChildItem -Recurse -Path (Join-Path $app "src") -Filter *.java | ForEach-Object { $_.FullName }
# android.jar goes on -classpath (not -bootclasspath) so the JDK's own core classes stay available for
# lambda support (LambdaMetafactory); d8 desugars lambdas for Android below (--min-api 24).
Run $javac -encoding UTF-8 -source 8 -target 8 -nowarn -classpath $androidJar -d "$work\classes" @sources

$classes = Get-ChildItem -Recurse -Path "$work\classes" -Filter *.class | ForEach-Object { $_.FullName }
Run $d8 --release --min-api 24 --lib $androidJar --output "$work\dex" @classes

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipFile = [System.IO.Compression.ZipFile]::Open("$work\base.apk", "Update")
try {
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zipFile, "$work\dex\classes.dex", "classes.dex") | Out-Null
} finally { $zipFile.Dispose() }
Run $zipalign -f -p 4 "$work\base.apk" "$work\aligned.apk"

# ---------------------------------------------------------------- sign (your own key, kept in android\signing)
$signing = Join-Path $root "android\signing"
$keystore = Join-Path $signing "jarvis-wake.jks"
$passFile = Join-Path $signing "password.txt"
if (-not (Test-Path $keystore)) {
    Step "Making the app's signing key (once) in android\signing - keep this folder"
    New-Item -ItemType Directory -Force -Path $signing | Out-Null
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "B").Replace("=", "")
    Set-Content -Path $passFile -Value $password -NoNewline -Encoding ASCII
    Run $keytool -genkeypair -keystore $keystore -storepass $password -keypass $password -alias jarviswake `
        -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=Jarvis Wake, O=Jarvis on $env:COMPUTERNAME"
}
Run $apksigner sign --ks $keystore --ks-pass "file:$passFile" --ks-key-alias jarviswake --out "$work\$appName.apk" `
    "$work\aligned.apk"
Run $apksigner verify "$work\$appName.apk"

New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
Copy-Item -Force "$work\$appName.apk" $out
$size = [math]::Round((Get-Item $out).Length / 1KB)
Step "Done: $out ($size KB). Rebuild Jarvis (build.ps1 -Install) so it can offer the app to your phone."
