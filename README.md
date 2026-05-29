# Claude Code Usage Collector (Windows installer)

End-user Windows installer + source for the agent that uploads Claude Code
usage data to the team dashboard.

> **The server lives separately.** This repo contains only the client-side
> collector and its installer. The dashboard server (Vercel + Supabase) is
> in [claude-usage-dashboard](https://github.com/samirtak-dynatechconsultancy/claude-usage-dashboard).
> See [SETUP.md in that repo](https://github.com/samirtak-dynatechconsultancy/claude-usage-dashboard/blob/main/SETUP.md)
> for the full deployment story.

---

## For end users

Grab the latest `.exe` from the
[**Releases**](https://github.com/samirtak-dynatechconsultancy/claude-usage-exe/releases/latest)
page and run it.

The installer:

1. Shows a **consent screen** explaining exactly what gets collected
   (token metadata + full conversation content)
2. Asks for your team's **server URL** + **ingest token** — get these from
   whoever set up the dashboard
3. Installs to `C:\Program Files\ClaudeUsageCollector\`
4. Registers a **Windows Scheduled Task** that runs every 15 minutes in
   the background — uploads anything new since the last push

To stop it: **Add or remove programs** → **Claude Code Usage Collector** →
Uninstall.

Per-machine logs and state live at `%LOCALAPPDATA%\ClaudeUsageCollector\`.

---

## For admins distributing it

### Silent install for fleet rollouts (GPO / PDQ / Intune / SCCM)

```powershell
ClaudeUsageCollector-Setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES `
    /SERVERURL=https://your-app.vercel.app `
    /TOKEN=YOUR_SHARED_INGEST_TOKEN
```

Suppresses every prompt and pre-fills both wizard fields. No user
interaction needed.

### Building the installer yourself

```powershell
cd installer
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" .\setup.iss
```

Outputs `installer\Output\ClaudeUsageCollector-Setup-1.0.0.exe`.

Prerequisites:
- Python 3.8+ with `pyinstaller` (the build script installs it if missing)
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) — install via
  `winget install JRSoftware.InnoSetup` or from the website

Bump the version by editing the `#define MyAppVersion` line at the top of
[installer/setup.iss](installer/setup.iss).

---

## What this collector actually does

Every 15 minutes (or on demand via the Start Menu shortcut), the agent:

1. Walks `~/.claude/projects/**/*.jsonl` (and the Xcode coding-assistant
   directory if present)
2. Compares each file's mtime against
   `%LOCALAPPDATA%\ClaudeUsageCollector\state.json`
3. For new or changed files:
   - Computes SHA-256
   - POSTs `/api/upload-url` to the server → gets a signed Supabase
     Storage upload URL
   - PUTs the raw JSONL bytes to that URL (raw conversation content lives
     in Supabase Storage)
   - Parses the JSONL locally to extract per-turn token metadata
4. POSTs the metadata batch to `/api/ingest`
5. Updates local state so the next push only handles what's new

It's **stdlib-only Python** — no `pip install` step. That keeps the
PyInstaller bundle small (~10 MB) and the attack surface tiny.

## Manual testing without the installer

```powershell
cd collector
copy config.example.json config.json
notepad config.json    # paste server_url + ingest_token
python collector.py status            # show config + state
python collector.py push --dry-run    # list what would upload
python collector.py push              # actually upload
python collector.py reset-state       # forget upload history
```

See [collector/README.md](collector/README.md) for full collector docs and
[installer/README.md](installer/README.md) for installer internals.

## License

MIT — see [LICENSE](LICENSE).
