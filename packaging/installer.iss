; Inno Setup 6 script for PC Control Deck (Windows agent + tray app).
; Built by scripts/build-agent.ps1:  ISCC.exe /DAppVersion=x.y.z installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "PC Control Deck"
#define AppExe "PCControlDeck.exe"

[Setup]
AppId={{6B0C7E2E-3B7A-4C0E-9E7A-5D2C6F1B8A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Admin: registers elevated scheduled tasks (CPU temperature sensors) and LAN-only firewall rules.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=PCControlDeck-Setup-{#AppVersion}
SetupIconFile=..\build\pcdeck.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
es.TaskAutostart=Iniciar con Windows (recomendado)
en.TaskAutostart=Start with Windows (recommended)
es.TaskFirewall=Permitir que los dispositivos se conecten en redes privadas (recomendado)
en.TaskFirewall=Allow devices to connect on private networks (recommended)
es.Launch=Abrir PC Control Deck
en.Launch=Open PC Control Deck
es.GroupOptions=Opciones:
en.GroupOptions=Options:

[Tasks]
Name: "autostart"; Description: "{cm:TaskAutostart}"; GroupDescription: "{cm:GroupOptions}"
Name: "firewall"; Description: "{cm:TaskFirewall}"; GroupDescription: "{cm:GroupOptions}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\PCControlDeck\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "setup-tasks.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Scheduled tasks (elevated app without UAC prompts; autostart task disabled when unchecked)
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup-tasks.ps1"" -Exe ""{app}\{#AppExe}"" -Autostart 1"; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup-tasks.ps1"" -Exe ""{app}\{#AppExe}"" -Autostart 0"; Flags: runhidden waituntilterminated; Tasks: not autostart
; Firewall: inbound only for this program, private profile only (agent WebSocket + mDNS replies)
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden waituntilterminated; Tasks: firewall
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" protocol=TCP localport=8765 profile=private enable=yes"; Flags: runhidden waituntilterminated; Tasks: firewall
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" protocol=UDP localport=5353 profile=private enable=yes"; Flags: runhidden waituntilterminated; Tasks: firewall
Filename: "{app}\{#AppExe}"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent
; Self-update from the app (silent setup with /AUTOUPDATE=1): start the new version again, as the
; signed-in user; the app hands itself to its elevated scheduled task (no UAC prompt).
Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Flags: nowait runasoriginaluser; Check: IsAutoUpdate

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "kill"
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup-tasks.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "tasks"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden waituntilterminated; RunOnceId: "fw"

[Code]
// Started by the app's self-update (PCControlDeck-Setup-x.y.z.exe /VERYSILENT ... /AUTOUPDATE=1).
function IsAutoUpdate: Boolean;
begin
  Result := ExpandConstant('{param:AUTOUPDATE|0}') = '1';
end;

// Stop a running copy before files are replaced on upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
