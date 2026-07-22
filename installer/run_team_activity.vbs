' run_team_activity.vbs - launch the collector's `team-activity` command with
' NO visible window. The scheduled task runs this via wscript (itself
' windowless); wscript then starts ClaudeUsageCollector.exe with window style 0
' (hidden), so no console flashes on each daily collection. The exe sits in the
' same folder as this script.
Option Explicit
Dim fso, sh, dir, exePath
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = dir & "\ClaudeUsageCollector.exe"
' args: command, windowStyle 0 = hidden, bWaitOnReturn False = don't block
sh.Run """" & exePath & """ team-activity", 0, False
