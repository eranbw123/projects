' Hidden launcher for Scheduled Tasks: runs its arguments as one command with
' no console window. wscript.exe is a GUI-subsystem host, so unlike
' `cmd /c start /min` or `powershell -WindowStyle Hidden` nothing ever
' flashes on screen; the child console app gets a hidden console instead.
' Waits for the child and propagates its exit code, so Task Scheduler's
' Last Result and instance policies keep meaning exactly what they meant
' with a visible console action. Registered actions look like:
'   wscript //B "hidden.vbs" "python.exe" "script.py" args...
Dim sh, i, part, cmd
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    part = WScript.Arguments(i)
    If InStr(part, " ") > 0 Then part = Chr(34) & part & Chr(34)
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & part
Next
WScript.Quit sh.Run(cmd, 0, True)
