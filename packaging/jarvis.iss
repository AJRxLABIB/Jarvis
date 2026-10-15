; Inno Setup script: a normal Windows installer for Jarvis (per-user, no administrator rights).
; Built by: powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Installer
; (needs Inno Setup 6: https://jrsoftware.org/isinfo.php, or "winget install JRSoftware.InnoSetup")

#ifndef AppVersion
  #define AppVersion "3.25.0"
#endif

[Setup]
AppId={{6C1B7A3E-2F4D-4B7A-9C51-3A9E7D2F1B60}
AppName=Jarvis Assistant
AppVersion={#AppVersion}
AppVerName=Jarvis Assistant {#AppVersion}
AppPublisher=Jarvis Assistant
DefaultDirName={localappdata}\Programs\Jarvis
DefaultGroupName=Jarvis
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=JarvisSetup-{#AppVersion}
SetupIconFile=jarvis.ico
UninstallDisplayIcon={app}\JARVIS.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Jarvis when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Jarvis"; Filename: "{app}\JARVIS.exe"
Name: "{autodesktop}\Jarvis"; Filename: "{app}\JARVIS.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JarvisAssistant"; ValueData: """{app}\JARVIS.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\JARVIS.exe"; Description: "Start Jarvis now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; your settings, memories and models in %LOCALAPPDATA%\JarvisAssistant are kept
Type: filesandordirs; Name: "{app}"
