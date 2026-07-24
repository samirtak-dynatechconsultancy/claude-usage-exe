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
#define MyAppVersion    "1.9.8"
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
Source: "run_usage.vbs";                 DestDir: "{app}"; Flags: ignoreversion
Source: "run_team_activity.vbs";         DestDir: "{app}"; Flags: ignoreversion
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
Name: "{group}\Run team activity now"; Filename: "{app}\{#MyAppExeName}"; Parameters: "team-activity"; WorkingDir: "{app}"
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

; v1.8.2: schedule the daily Claude Desktop usage upload. This runs in the
; ELEVATED installer context (deliberately NOT runasoriginaluser) so it has the
; rights to register a RunLevel-Highest task. That task then runs elevated as
; the install user, so VSS can read the cookie even while Claude Desktop is open.
; NOTE: this only works for users who are LOCAL ADMINS -- a standard user's task
; can't elevate, so usage stays empty for them (see RELEASE_NOTES_v1_8_2).
; skipifsilent: SYSTEM/Intune installs would register the task as SYSTEM, which
; can't decrypt the per-user cookie, so we skip it in that path.
; No skipifsilent: silent/Intune installs must register the task too (it runs
; as BUILTIN\Users -> the logged-on user, via --fleet). SYSTEM can register it.
Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:GetUsageParams}"; \
    Flags: runhidden; \
    StatusMsg: "Scheduling Claude Desktop usage upload..."

; Push once now so the dashboard shows data immediately (elevated, hidden via
; the wscript launcher so no console flashes).
Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_usage.vbs"""; \
    Flags: runhidden nowait skipifsilent; \
    StatusMsg: "Uploading current Claude Desktop usage..."

; Schedule the DAILY Team Activity collection (claude.ai per-user admin
; analytics). Registers as BUILTIN\Users (--fleet) so it works for both
; attended and silent/Intune installs. The daily time comes from the Team
; Activity wizard page (GetTeamActivityParams; /TEAMTIME= for silent installs,
; default 08:00). The task no-ops until analytics_orgs is set in config.json,
; so registering it now is harmless.
Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:GetTeamActivityParams}"; \
    Flags: runhidden; \
    StatusMsg: "Scheduling daily Team Activity collection..."

; Collect Team Activity once now so the dashboard shows data immediately after
; install (instead of waiting for the 08:00 task). Hidden via the wscript
; launcher; no-ops if analytics_orgs is empty. skipifsilent keeps SYSTEM/Intune
; installs from running it before the user has entered cookies.
Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_team_activity.vbs"""; \
    Flags: runhidden nowait skipifsilent; \
    StatusMsg: "Collecting Team Activity now..."

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

; v1.8.2: remove the daily Claude Desktop usage task.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""ClaudeUsageDaily"""; \
    Flags: runhidden; RunOnceId: "removeUsageTask"

; Remove the daily Team Activity task.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""ClaudeTeamActivityDaily"""; \
    Flags: runhidden; RunOnceId: "removeTeamActivityTask"

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
  IntervalUnitPage:   TInputOptionWizardPage;
  IntervalDetailPage: TInputQueryWizardPage;
  TeamActivityPage:   TWizardPage;
  TeamMemo:           TNewMemo;
  TeamTimeEdit:       TNewEdit;

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
  { v1.8.10: clear any stale usage task from an older version (it may have a
    pre-CREATE_NO_WINDOW action that flashed cmd windows). The [Run] section
    re-registers a clean one. Also stop a usage run in progress: killing the
    exe above already ends it (wscript launched it and did not wait). }
  Exec('schtasks.exe', '/Delete /F /TN "ClaudeUsageDaily"', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
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
  TeamActivityLabel: TNewStaticText;
  TeamTimeLabel: TNewStaticText;
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

  { v1.8.3: how often to collect Claude Desktop usage -- unit then number. }
  IntervalUnitPage := CreateInputOptionPage(
    ConfigPage.ID,
    'Usage Collection Schedule',
    'How often should Claude Desktop usage be uploaded?',
    'Pick a time unit here; set the number on the next page. It repeats on this interval.',
    True, False);
  IntervalUnitPage.Add('Minutes');
  IntervalUnitPage.Add('Hours');
  IntervalUnitPage.Add('Days');
  IntervalUnitPage.Add('Weeks');
  IntervalUnitPage.SelectedValueIndex := 2;   { Days by default }

  IntervalDetailPage := CreateInputQueryPage(
    IntervalUnitPage.ID,
    'Usage Collection Schedule',
    'Set the interval',
    'Runs again and again on this interval. The time of day applies only to Days/Weeks.');
  IntervalDetailPage.Add('Run every how many (a whole number):', False);
  IntervalDetailPage.Add('Time of day HH:MM (Days/Weeks only):', False);
  IntervalDetailPage.Values[0] := '1';
  IntervalDetailPage.Values[1] := '18:00';

  { v1.9.0: Team Activity org+cookie pairs. Collected here and written into
    config.json's analytics_orgs so the daily `team-activity` task can fetch
    claude.ai per-user admin analytics. One org per line, pipe-delimited:
        ORG_UUID | Label | Cookie
    The cookie is the FULL Cookie header from a logged-in claude.ai request
    and may itself contain '|'-free but quote/brace-heavy text, so parsing
    splits on only the FIRST TWO pipes and JSON-escapes each field. }
  TeamActivityPage := CreateCustomPage(
    IntervalDetailPage.ID,
    'Team Activity (optional)',
    'Collect claude.ai per-user admin analytics for your organizations');

  { Daily collection time. Team Activity always runs once a day (it collects
    the previous complete day), so only the time of day is configurable here.
    Written into the ClaudeTeamActivityDaily scheduled task by GetTeamActivityParams. }
  TeamTimeLabel := TNewStaticText.Create(TeamActivityPage);
  TeamTimeLabel.Parent := TeamActivityPage.Surface;
  TeamTimeLabel.Left := 0;
  TeamTimeLabel.Top := 2;
  TeamTimeLabel.AutoSize := True;
  TeamTimeLabel.Caption := 'Daily collection time (HH:MM, 24-hour) - runs once a day:';

  TeamTimeEdit := TNewEdit.Create(TeamActivityPage);
  TeamTimeEdit.Parent := TeamActivityPage.Surface;
  TeamTimeEdit.Left := 0;
  TeamTimeEdit.Top := 20;
  TeamTimeEdit.Width := 90;
  TeamTimeEdit.Text := '08:00';

  TeamActivityLabel := TNewStaticText.Create(TeamActivityPage);
  TeamActivityLabel.Parent := TeamActivityPage.Surface;
  TeamActivityLabel.Left := 0;
  TeamActivityLabel.Top := 52;
  TeamActivityLabel.Width := TeamActivityPage.SurfaceWidth;
  TeamActivityLabel.AutoSize := False;
  TeamActivityLabel.Height := 90;
  TeamActivityLabel.WordWrap := True;
  TeamActivityLabel.Caption :=
    'One organization per line: ORG_UUID | Label | Cookie' + #13#10 +
    '- ORG_UUID: the organization UUID (from the /organizations/<UUID>/ URL).' + #13#10 +
    '- Label: any name shown in the dashboard dropdown.' + #13#10 +
    '- Cookie: the FULL Cookie header from a logged-in claude.ai request ' +
    '(DevTools > Network > any request > Request Headers > Cookie; must contain sessionKey=...).' + #13#10 +
    'A long cookie may wrap onto several lines - that is fine. Leave blank to skip; ' +
    'you can add or update orgs later via Start Menu > Edit config.';

  TeamMemo := TNewMemo.Create(TeamActivityPage);
  TeamMemo.Parent := TeamActivityPage.Surface;
  TeamMemo.Left := 0;
  TeamMemo.Top := 148;
  TeamMemo.Width := TeamActivityPage.SurfaceWidth;
  TeamMemo.Height := TeamActivityPage.SurfaceHeight - 148;
  TeamMemo.ScrollBars := ssVertical;
  TeamMemo.WordWrap := True;
end;

{ Escape a string for embedding inside a JSON double-quoted value. Order
  matters: backslashes first, then double quotes. Handles cookies that carry
  quote-heavy sub-values such as the g_state cookie. }
function JsonEsc(s: String): String;
begin
  StringChangeEx(s, '\', '\\', True);
  StringChangeEx(s, '"', '\"', True);
  Result := s;
end;

{ Count occurrences of a character in a string. }
function CountChar(const s: String; c: Char): Integer;
var
  i: Integer;
begin
  Result := 0;
  for i := 1 to Length(s) do
    if s[i] = c then
      Result := Result + 1;
end;

{ Collect the Team Activity memo into one record per organization.

  Users enter one org per line: ORG_UUID | Label | Cookie. But a pasted cookie
  is long and often arrives with hard line breaks, so it can span several
  physical lines. We stitch those back together: a line with >= 2 pipes STARTS
  a new org record; any following line with < 2 pipes is a wrapped continuation
  of the current record's cookie and is appended to it. (Cookies don't contain
  '|', so pipe-count is a safe record delimiter.) Returns the record count. }
function GetTeamRecords(var recs: TArrayOfString): Integer;
var
  i, n: Integer;
  raw: String;
begin
  n := 0;
  SetArrayLength(recs, 0);
  if TeamMemo <> nil then
  begin
    for i := 0 to TeamMemo.Lines.Count - 1 do
    begin
      raw := TeamMemo.Lines[i];
      if Trim(raw) <> '' then
      begin
        if CountChar(raw, '|') >= 2 then
        begin
          n := n + 1;
          SetArrayLength(recs, n);
          recs[n - 1] := Trim(raw);
        end
        else if n > 0 then
          recs[n - 1] := recs[n - 1] + Trim(raw)
        else
        begin
          n := n + 1;
          SetArrayLength(recs, n);
          recs[n - 1] := Trim(raw);   { malformed leading line; flagged later }
        end;
      end;
    end;
  end;
  Result := n;
end;

{ Parse one record (ORG_UUID | Label | Cookie) on the first two pipes, so any
  pipe inside the cookie is preserved. Empty out-params signal a bad record. }
procedure ParseRec(const rec: String; var org, name, cookie: String);
var
  p1, p2: Integer;
  rest: String;
begin
  org := ''; name := ''; cookie := '';
  p1 := Pos('|', rec);
  if p1 = 0 then Exit;
  org := Trim(Copy(rec, 1, p1 - 1));
  rest := Copy(rec, p1 + 1, Length(rec));
  p2 := Pos('|', rest);
  if p2 = 0 then Exit;
  name := Trim(Copy(rest, 1, p2 - 1));
  cookie := Trim(Copy(rest, p2 + 1, Length(rest)));
end;

{ Build the analytics_orgs JSON array body (without the surrounding brackets). }
function BuildAnalyticsOrgs: String;
var
  recs: TArrayOfString;
  i, cnt: Integer;
  org, name, cookie, acc: String;
begin
  acc := '';
  cnt := GetTeamRecords(recs);
  for i := 0 to cnt - 1 do
  begin
    ParseRec(recs[i], org, name, cookie);
    if (org <> '') and (cookie <> '') then
    begin
      if acc <> '' then
        acc := acc + ',' + #13#10;
      acc := acc + '    {"org": "' + JsonEsc(org) +
        '", "org_name": "' + JsonEsc(name) +
        '", "cookie": "' + JsonEsc(cookie) + '"}';
    end;
  end;
  Result := acc;
end;

procedure WriteConfigJson;
var
  configPath:        String;
  contents:          String;
  serverUrl:         String;
  ingestTok:         String;
  uploadContentStr:  String;
  analyticsBody:     String;
  analyticsBlock:    String;
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
  analyticsBody := BuildAnalyticsOrgs;
  if analyticsBody = '' then
    analyticsBlock := '  "analytics_orgs": []'
  else
    analyticsBlock := '  "analytics_orgs": [' + #13#10 +
                      analyticsBody + #13#10 +
                      '  ]';

  configPath := ExpandConstant('{app}\config.json');
  contents :=
    '{' + #13#10 +
    '  "server_url":     "' + serverUrl + '",' + #13#10 +
    '  "ingest_token":   "' + ingestTok + '",' + #13#10 +
    '  "upload_content": ' + uploadContentStr + ',' + #13#10 +
    '  "projects_dirs":  null,' + #13#10 +
    '  "analytics_days_back": 1,' + #13#10 +
    analyticsBlock + #13#10 +
    '}' + #13#10;
  SaveStringToFile(configPath, contents, False);
end;

{ Build the collector argument string for the usage-task registration, from
  the schedule pages. Called by the Run entry via a code: constant. }
function GetUsageParams(Param: String): String;
var
  unitStr, num, atTime: String;
begin
  if IntervalUnitPage = nil then
  begin
    Result := 'usage --install-task --every 1 --unit days --at 18:00 --fleet';
    Exit;
  end;
  case IntervalUnitPage.SelectedValueIndex of
    0: unitStr := 'minutes';
    1: unitStr := 'hours';
    3: unitStr := 'weeks';
  else
    unitStr := 'days';
  end;
  num := Trim(IntervalDetailPage.Values[0]);
  if num = '' then num := '1';
  atTime := Trim(IntervalDetailPage.Values[1]);
  if atTime = '' then atTime := '18:00';
  Result := 'usage --install-task --every ' + num +
            ' --unit ' + unitStr + ' --at ' + atTime + ' --fleet';
end;

{ Build the collector argument string for the DAILY team-activity task from the
  Team Activity wizard page's time field. /TEAMTIME= overrides for silent
  installs; falls back to 08:00. Team Activity is always daily. }
function GetTeamActivityParams(Param: String): String;
var
  atTime: String;
begin
  atTime := Trim(GetCmdLineParam('TEAMTIME'));
  if (atTime = '') and (TeamTimeEdit <> nil) then
    atTime := Trim(TeamTimeEdit.Text);
  if atTime = '' then atTime := '08:00';
  Result := 'team-activity --install-task --every 1 --unit days --at '
            + atTime + ' --fleet';
end;

{ True if s is a valid HH:MM 24-hour time. }
function IsValidHHMM(s: String): Boolean;
var
  p, hh, mm: Integer;
begin
  Result := False;
  p := Pos(':', s);
  if p < 2 then Exit;
  hh := StrToIntDef(Copy(s, 1, p - 1), -1);
  mm := StrToIntDef(Copy(s, p + 1, Length(s)), -1);
  Result := (hh >= 0) and (hh <= 23) and (mm >= 0) and (mm <= 59);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  url: String;
  n, teamCount: Integer;
  teamRecs: TArrayOfString;
  org, name, cookie: String;
begin
  Result := True;

  if CurPageID = ConfigPage.ID then
  begin
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

  if (IntervalDetailPage <> nil) and (CurPageID = IntervalDetailPage.ID) then
  begin
    n := StrToIntDef(Trim(IntervalDetailPage.Values[0]), -1);
    if n < 1 then
    begin
      MsgBox('Enter a whole number of 1 or more for the interval.', mbError, MB_OK);
      Result := False; Exit;
    end;
  end;

  { Team Activity: validate each org record parses to ORG_UUID | Label |
    Cookie with a non-empty org + cookie. A wrapped/multi-line cookie is
    stitched back together by GetTeamRecords, so a long paste is fine. Empty
    page is fine (skipped). }
  if (TeamActivityPage <> nil) and (CurPageID = TeamActivityPage.ID) then
  begin
    if not IsValidHHMM(Trim(TeamTimeEdit.Text)) then
    begin
      MsgBox('Enter the daily collection time as HH:MM (24-hour), e.g. 08:00.',
             mbError, MB_OK);
      Result := False; Exit;
    end;
    teamCount := GetTeamRecords(teamRecs);
    for n := 0 to teamCount - 1 do
    begin
      ParseRec(teamRecs[n], org, name, cookie);
      if (org = '') or (cookie = '') then
      begin
        MsgBox('Team Activity organization ' + IntToStr(n + 1) + ' is '
               + 'incomplete. Enter one organization per line as:'#13#10
               + '    ORG_UUID | Label | Cookie'#13#10#13#10
               + 'The cookie may wrap across several lines - that is fine. '
               + 'Or clear the box to skip Team Activity setup.',
               mbError, MB_OK);
        Result := False; Exit;
      end;
    end;
  end;
end;
