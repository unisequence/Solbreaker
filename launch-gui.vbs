Option Explicit
Dim shell, files, root, command, argument, code
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(root & "\launch-gui.ps1")
For Each argument In WScript.Arguments
    command = command & " " & Quote(argument)
Next
code = shell.Run(command, 0, True)
If code <> 0 Then
    MsgBox "Solbreaker could not start. See the launcher logs in " & root & "\runtime", vbExclamation, "Solbreaker"
End If
WScript.Quit code

Function Quote(value)
    ' Windows paths cannot contain quotes. Escape trailing slashes before the closing quote.
    If InStr(value, Chr(34)) > 0 Then Err.Raise 5, , "Invalid launcher argument"
    Dim position
    position = Len(value)
    Do While position > 0
        If Mid(value, position, 1) <> "\" Then Exit Do
        position = position - 1
    Loop
    Quote = Chr(34) & value & String(Len(value) - position, "\") & Chr(34)
End Function
