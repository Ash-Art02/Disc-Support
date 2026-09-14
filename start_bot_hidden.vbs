Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\run.py""", 0, False
