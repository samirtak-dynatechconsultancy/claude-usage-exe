' run_usage.vbs - launch the collector's `usage` command with NO visible window.
' The scheduled task runs this via wscript (itself windowless); wscript then
' starts ClaudeUsageCollector.exe with window style 0 (hidden), so no console
' flashes on each collection. The exe sits in the same folder as this script.
Option Explicit
Dim fso, sh, dir, exePath
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = dir & "\ClaudeUsageCollector.exe"
' args: command, windowStyle 0 = hidden, bWaitOnReturn False = don't block
sh.Run """" & exePath & """ usage", 0, False
