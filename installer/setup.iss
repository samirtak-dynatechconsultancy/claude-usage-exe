; setup.iss — Inno Setup script for the Claude Code Usage Collector.
;
; Build steps:
;   1. powershell -File build_exe.ps1     ; produces dist\ClaudeUsageCollector.exe
;   2. ISCC.exe setup.iss                  ; produces Output\ClaudeUsageCollector-Setup.exe
;
; Distribution: end-users (or IT via GPO/PDQ/SCCM) run the produced setup.exe.
;
; Silent install for fleet rollouts:
;   ClaudeUsageCollector-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES ^
;     /SERVERURL=https://your-app.vercel.app ^
;     /TOKEN=your-shared-ingest-token
;
; Interactive install: the wizard asks for both values on a custom page,
; pre-filled with command-line values if provided.

#define MyAppName       "Claude Code Usage Collector"
#define MyAppVersion    "1.8.1"
#define MyAppPublisher  "Internal"
#define MyAppExeName    "ClaudeUsageCollector.exe"
#define MyTrayExeName   "ClaudeUsageTray.exe"
#define TaskName        "ClaudeCodeUsageCollector"
#define TaskIntervalMin 15
#define TrayRunKey      "ClaudeUsageCollectorTray"
#define CollectorRunKey "ClaudeUsageCollectorDaemon"

; ── Team-default config baked into the installer ─────────────────────────────
; These pre-fill the wizard fields so end users don't have to type the
; server URL / token. They are still overridable via /SERVERURL= and /TOKEN=
; command-line flags (silent install path) or by editing the wizard fields
; before clicking Next.
;
; ⚠ INGEST_TOKEN security note: anyone who downloads this installer can
;    extract the embedded token (`strings ClaudeUsageCollector-Setup-*.exe`
;    or unpack with InnoUnp). Use a long random token, not a guessable one,
;    and treat it like a public-but-rate-limited shared secret.
#define DefaultServerUrl    "https://claude-usage-dashboard-uhfn.vercel.app"
#define DefaultIngestToken  "dynatechconsultancy"

[Setup]
AppId={{8A4E4A2C-3B57-4F2A-9C1A-3B7E1A2D0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ClaudeUsageCollector
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=ClaudeUsageCollector-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\ClaudeUsageCollector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\ClaudeUsageTray.exe";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\collector\config.example.json"; DestDir: "{app}"; DestName: "config.example.json"; Flags: ignoreversion
; CONSENT.txt is the last file -- its AfterInstall procedure writes
; config.json. That guarantees config.json exists before [Run] fires the
; daemon. v1.6.0 wrote config.json in CurStepChanged(ssPostInstall) which
; runs AFTER [Run], so the daemon's first launch crashed with
; FileNotFoundError on a fresh install.
Source: "CONSENT.txt"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: WriteConfigJson

[Registry]
; Launch the collector daemon at every login -- for EVERY user on this box.
; HKLM (not HKCU) is the key change from the v1.4 Scheduled Task model:
; each Windows session (RDP or local) inherits this entry on logon and
; spawns its own daemon process under that user's identity. The daemon
; reads %CLIENTNAME% to attribute pushes to the right human even on a
; shared OS user (typical RDP).
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#CollectorRunKey}"; \
    ValueData: """{app}\{#MyAppExeName}"" daemon"; \
    Flags: uninsdeletevalue

; Launch the tray at every login (per-user HKCU since it's a UI thing).
; uninsdeletevalue removes the entry cleanly on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#TrayRunKey}"; \
    ValueData: """{app}\{#MyTrayExeName}"""; \
    Flags: uninsdeletevalue

[Icons]
Name: "{group}\Open log";              Filename: "notepad.exe"; Parameters: """{localappdata}\ClaudeUsageCollector\collector.log"""
Name: "{group}\Run push now";          Filename: "{app}\{#MyAppExeName}"; Parameters: "push"; WorkingDir: "{app}"
Name: "{group}\Show collector status"; Filename: "{app}\{#MyAppExeName}"; Parameters: "status"; WorkingDir: "{app}"
Name: "{group}\Open install folder";   Filename: "{app}"
Name: "{group}\Edit config";           Filename: "notepad.exe"; Parameters: """{app}\config.json"""
Name: "{group}\Launch tray icon";      Filename: "{app}\{#MyTrayExeName}"
Name: "{group}\Uninstall";             Filename: "{uninstallexe}"

[Run]
; v1.5 model: no Scheduled Task. The HKLM Run entry above autostarts the
; collector daemon on every user logon. To cover the upgrade-in-place case
; (someone running v1.4 with an existing Scheduled Task), delete it now.
; /F means "no confirmation", so it silently no-ops if the task isn't there.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; \
    Flags: runhidden skipifsilent; StatusMsg: "Removing legacy Scheduled Task (if any)..."

; Start the daemon NOW for the install user so they don't have to log out
; and back in to see data flowing. runasoriginaluser drops admin priv.
; nowait so the install finishes; the daemon runs forever.
; skipifsilent: MDM/Intune installs run as SYSTEM — the daemon would start
; in Session 0 with no access to the user's .claude/ files and block the
; real user-context instance from launching at logon.
Filename: "{app}\{#MyAppExeName}"; Parameters: "daemon"; WorkingDir: "{app}"; \
    Flags: runasoriginaluser nowait skipifsilent; \
    StatusMsg: "Starting collector daemon..."

; Launch the tray icon NOW so the user sees it immediately without having to
; log out and back in. runasoriginaluser drops admin priv -- the tray runs
; as the actual user, not the elevated installer.
Filename: "{app}\{#MyTrayExeName}"; \
    Flags: runasoriginaluser nowait skipifsilent; \
    StatusMsg: "Starting tray icon..."

[UninstallRun]
; Kill the running daemon + tray processes so Inno Setup can delete their
; .exes without "in use" errors. /F = force, /IM = match by image name.
Filename: "taskkill.exe"; Parameters: "/F /IM ""{#MyAppExeName}"""; \
    Flags: runhidden; RunOnceId: "killCollectorDaemon"
Filename: "taskkill.exe"; Parameters: "/F /IM ""{#MyTrayExeName}"""; \
    Flags: runhidden; RunOnceId: "killTrayApp"

; Belt-and-suspenders: kill any legacy Scheduled Task left over from v1.4
; installs. /F = no confirmation. Silently no-ops if absent.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; \
    Flags: runhidden; RunOnceId: "removeLegacySchedTask"

[UninstallDelete]
; The exe stops writing here at uninstall, but state.json / collector.log
; live under %LOCALAPPDATA% and may contain useful diagnostic info — leave
; them in place. The user can delete the folder manually.
Type: files; Name: "{app}\config.json"

[Code]
{ ──────────────────────────────────────────────────────────────────────────── }
{ Custom wizard page: SERVER_URL + INGEST_TOKEN entry                          }
{ ──────────────────────────────────────────────────────────────────────────── }
var
  ConfigPage:  TInputQueryWizardPage;
  ConsentPage: TOutputMsgMemoWizardPage;
  PrivacyPage: TInputOptionWizardPage;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ErrorCode: Integer;
begin
  { v1.6+: in-place upgrades land here. The currently-running collector
    daemon + tray hold their .exe handles open, which would cause Inno
    Setup's file copy step to fail with "in use" prompts or restart-
    required dialogs. taskkill them first; /F means SIGKILL-equivalent,
    and the new install's [Run] section restarts both. }
  Exec('taskkill.exe', '/F /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Exec('taskkill.exe', '/F /IM "{#MyTrayExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Result := '';
end;

function GetCmdLineParam(const Name: String): String;
var
  i: Integer;
  prefix: String;
begin
  Result := '';
  prefix := '/' + Name + '=';
  for i := 1 to ParamCount do
    if Pos(LowerCase(prefix), LowerCase(ParamStr(i))) = 1 then
    begin
      Result := Copy(ParamStr(i), Length(prefix) + 1, MaxInt);
      Exit;
    end;
end;

procedure InitializeWizard;
var
  ConsentText: TArrayOfString;
begin
  { Load the consent file shipped in [Files]; if missing, fall back to inline.}
  if not LoadStringsFromFile(ExpandConstant('{tmp}\CONSENT.txt'), ConsentText) then
  begin
    SetArrayLength(ConsentText, 5);
    ConsentText[0] := 'This software uploads metadata AND the full text of your';
    ConsentText[1] := 'Claude Code conversations to your team''s dashboard server.';
    ConsentText[2] := '';
    ConsentText[3] := 'Do not paste secrets, customer PII, or other sensitive data';
    ConsentText[4] := 'into Claude Code prompts on this machine.';
  end;

  ConsentPage := CreateOutputMsgMemoPage(
    wpWelcome,
    'Data Collection Notice',
    'Please read carefully before continuing',
    'By installing this collector you agree to upload your Claude Code usage data to your team''s dashboard server. ' +
    'This includes conversation content — not just token counts.',
    ''
  );
  { Memo text is set after page creation. }

  { Privacy / data scope page: choose between full content and metadata only.
    Sits between the consent screen and the server-connection page so the
    user makes the choice before being asked for the team secret. }
  PrivacyPage := CreateInputOptionPage(
    ConsentPage.ID,
    'Data Scope',
    'Choose how much data this machine sends',
    'Token counts and project names are always sent (charts and costs need them). ' +
    'Choose whether to also send the actual text of your conversations.',
    True,   { Exclusive   = True  => radio buttons }
    False   { ListBox     = False => standard radio look, not a listbox }
  );
  PrivacyPage.Add('Full conversation content (recommended for team audits, lets the dashboard show what was discussed)');
  PrivacyPage.Add('Metadata only (token counts, models, project names — no prompt or response text leaves this machine)');

  { Pre-fill from /CONTENT= silent-install flag if present; default to full. }
  if LowerCase(Trim(GetCmdLineParam('CONTENT'))) = 'metadata' then
    PrivacyPage.SelectedValueIndex := 1
  else
    PrivacyPage.SelectedValueIndex := 0;

  ConfigPage := CreateInputQueryPage(
    PrivacyPage.ID,
    'Server Connection',
    'Where should this machine send its data?',
    'Get these values from whoever set up the dashboard. The server URL is the Vercel deployment; the ingest token is a shared team secret.'
  );
  ConfigPage.Add('Server URL (e.g. https://your-app.vercel.app):', False);
  ConfigPage.Add('Ingest token:', True);

  { Pre-fill priority:
      1. /SERVERURL= or /TOKEN= command-line flags (silent install)
      2. Team-default baked into the installer (#define above)
    Both still editable by the user in the wizard before clicking Next. }
  ConfigPage.Values[0] := GetCmdLineParam('SERVERURL');
  if ConfigPage.Values[0] = '' then
    ConfigPage.Values[0] := '{#DefaultServerUrl}';
  ConfigPage.Values[1] := GetCmdLineParam('TOKEN');
  if ConfigPage.Values[1] = '' then
    ConfigPage.Values[1] := '{#DefaultIngestToken}';
end;

procedure WriteConfigJson;
var
  configPath:        String;
  contents:          String;
  serverUrl:         String;
  ingestTok:         String;
  uploadContentStr:  String;
begin
  { Called from the Files AfterInstall hook on CONSENT.txt (the last file).
    Runs AFTER all Files entries are copied but BEFORE the Run section
    fires the daemon -- which is exactly the slot we need so the daemon
    finds a populated config on its first launch.

    v1.6.0 did this in CurStepChanged(ssPostInstall) which fires AFTER
    the Run section, so the daemon's first launch crashed with
    FileNotFoundError on a fresh install. }

  serverUrl := Trim(ConfigPage.Values[0]);
  ingestTok := Trim(ConfigPage.Values[1]);

  { PrivacyPage.SelectedValueIndex: 0 = full content, 1 = metadata only.
    Translate to a JSON boolean. }
  if PrivacyPage.SelectedValueIndex = 1 then
    uploadContentStr := 'false'
  else
    uploadContentStr := 'true';

  { Write config.json into the install dir. Use ASCII JSON so even non-ASCII
    hostnames don't trip the collector's json.loads on Python < 3.6. }
  configPath := ExpandConstant('{app}\config.json');
  contents :=
    '{' + #13#10 +
    '  "server_url":     "' + serverUrl + '",' + #13#10 +
    '  "ingest_token":   "' + ingestTok + '",' + #13#10 +
    '  "upload_content": ' + uploadContentStr + ',' + #13#10 +
    '  "projects_dirs":  null' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(configPath, contents, False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  url: String;
begin
  Result := True;
  if CurPageID <> ConfigPage.ID then Exit;

  url := Trim(ConfigPage.Values[0]);
  if (Pos('http://', LowerCase(url)) <> 1) and (Pos('https://', LowerCase(url)) <> 1) then
  begin
    MsgBox('Server URL must start with http:// or https://', mbError, MB_OK);
    Result := False; Exit;
  end;
  if Trim(ConfigPage.Values[1]) = '' then
  begin
    MsgBox('Ingest token is required.', mbError, MB_OK);
    Result := False; Exit;
  end;
end;
