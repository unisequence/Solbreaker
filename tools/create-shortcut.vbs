Option Explicit

Dim shell, files, root, python, code
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(files.GetParentFolderName(WScript.ScriptFullName))
python = root & "\runtime\python\pythonw.exe"
If Not files.FileExists(python) Then python = root & "\.venv-gui\Scripts\pythonw.exe"
If Not files.FileExists(python) Then
    MsgBox "Open launch-gui.vbs once before creating a shortcut.", vbExclamation, "Solbreaker"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
code = shell.Run(Chr(34) & python & Chr(34) & " -m solbreaker.windows_shortcut", 0, True)
If code = 0 Then
    MsgBox "Solbreaker shortcut created on your desktop.", vbInformation, "Solbreaker"
Else
    MsgBox "Solbreaker could not create the desktop shortcut.", vbExclamation, "Solbreaker"
End If
WScript.Quit code
