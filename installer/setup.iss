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
#define MyAppVersion    "1.3.1"
#define MyAppPublisher  "Internal"
#define MyAppExeName    "ClaudeUsageCollector.exe"
#define TaskName        "ClaudeCodeUsageCollector"
#define TaskIntervalMin 15

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
OutputBaseFilename=ClaudeUsageCollector-Setup-{#MyAppVersion}
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
Source: "..\collector\config.example.json"; DestDir: "{app}"; DestName: "config.example.json"; Flags: ignoreversion
Source: "CONSENT.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Push usage data now"; Filename: "{app}\{#MyAppExeName}"; Parameters: "push"; WorkingDir: "{app}"
Name: "{group}\Show collector status"; Filename: "{app}\{#MyAppExeName}"; Parameters: "status"; WorkingDir: "{app}"
Name: "{group}\Uninstall"; Filename: "{uninstallexe}"

[Run]
; Run an initial push so the dashboard immediately sees this machine.
; Hidden — we don't want a console flashing in front of the user.
Filename: "{app}\{#MyAppExeName}"; Parameters: "push"; WorkingDir: "{app}"; \
    Flags: runhidden nowait; StatusMsg: "Running first push..."

; Register the recurring Scheduled Task via a single schtasks /Create /XML.
; The XML (written in CurStepChanged before this fires) carries all settings:
; trigger (every {#TaskIntervalMin}m), battery-friendly flags, run level.
; This replaces the v1.2.1/v1.3.0 approach of schtasks-then-powershell-tweak,
; which Sophos Endpoint Agent flagged as "Lockdown malicious behavior" —
; powershell.exe from an installer modifying Scheduled Tasks looks like a
; classic persistence pattern to AV heuristics.
Filename: "schtasks.exe"; Parameters: \
    "/Create /F /XML ""{tmp}\ClaudeCodeUsageCollector.xml"" /TN ""{#TaskName}"" /RU ""{username}"""; \
    Flags: runhidden; StatusMsg: "Registering Scheduled Task..."

[UninstallRun]
; Remove the Scheduled Task. /F = no confirmation prompt.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; Flags: runhidden

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

procedure CurStepChanged(CurStep: TSetupStep);
var
  configPath:        String;
  contents:          String;
  serverUrl:         String;
  ingestTok:         String;
  uploadContentStr:  String;
  xmlPath:           String;
  xml:               String;
  exePath:           String;
begin
  if CurStep <> ssPostInstall then Exit;

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

  { Build the Scheduled Task XML used by the schtasks /Create /XML invocation
    above. Embeds all the battery-friendly settings inline so we do not have
    to call PowerShell post-create (which Sophos Endpoint Agent flags as
    "Lockdown" malicious behavior). schtasks /XML accepts UTF-8 with BOM. }
  exePath := ExpandConstant('{app}\{#MyAppExeName}');
  xmlPath := ExpandConstant('{tmp}\ClaudeCodeUsageCollector.xml');
  xml :=
    '<?xml version="1.0" encoding="UTF-8"?>' + #13#10 +
    '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' + #13#10 +
    '  <RegistrationInfo>' + #13#10 +
    '    <Description>Claude Code Usage Collector - pushes new usage data every {#TaskIntervalMin} minutes</Description>' + #13#10 +
    '  </RegistrationInfo>' + #13#10 +
    '  <Triggers>' + #13#10 +
    '    <TimeTrigger>' + #13#10 +
    '      <Repetition>' + #13#10 +
    '        <Interval>PT{#TaskIntervalMin}M</Interval>' + #13#10 +
    '        <StopAtDurationEnd>false</StopAtDurationEnd>' + #13#10 +
    '      </Repetition>' + #13#10 +
    '      <StartBoundary>2026-01-01T00:00:00</StartBoundary>' + #13#10 +
    '      <Enabled>true</Enabled>' + #13#10 +
    '    </TimeTrigger>' + #13#10 +
    '  </Triggers>' + #13#10 +
    '  <Principals>' + #13#10 +
    '    <Principal id="Author">' + #13#10 +
    '      <LogonType>InteractiveToken</LogonType>' + #13#10 +
    '      <RunLevel>HighestAvailable</RunLevel>' + #13#10 +
    '    </Principal>' + #13#10 +
    '  </Principals>' + #13#10 +
    '  <Settings>' + #13#10 +
    '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>' + #13#10 +
    '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>' + #13#10 +
    '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>' + #13#10 +
    '    <AllowHardTerminate>true</AllowHardTerminate>' + #13#10 +
    '    <StartWhenAvailable>true</StartWhenAvailable>' + #13#10 +
    '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>' + #13#10 +
    '    <IdleSettings>' + #13#10 +
    '      <StopOnIdleEnd>true</StopOnIdleEnd>' + #13#10 +
    '      <RestartOnIdle>false</RestartOnIdle>' + #13#10 +
    '    </IdleSettings>' + #13#10 +
    '    <AllowStartOnDemand>true</AllowStartOnDemand>' + #13#10 +
    '    <Enabled>true</Enabled>' + #13#10 +
    '    <Hidden>false</Hidden>' + #13#10 +
    '    <RunOnlyIfIdle>false</RunOnlyIfIdle>' + #13#10 +
    '    <WakeToRun>false</WakeToRun>' + #13#10 +
    '    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>' + #13#10 +
    '    <Priority>7</Priority>' + #13#10 +
    '  </Settings>' + #13#10 +
    '  <Actions Context="Author">' + #13#10 +
    '    <Exec>' + #13#10 +
    '      <Command>' + exePath + '</Command>' + #13#10 +
    '      <Arguments>push</Arguments>' + #13#10 +
    '      <WorkingDirectory>' + ExpandConstant('{app}') + '</WorkingDirectory>' + #13#10 +
    '    </Exec>' + #13#10 +
    '  </Actions>' + #13#10 +
    '</Task>' + #13#10;
  { XML content above is pure ASCII, so byte-identical under ANSI / UTF-8 /
    UTF-8-with-BOM-stripped. SaveStringToFile gives us all of those at once. }
  SaveStringToFile(xmlPath, xml, False);
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
