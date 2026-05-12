' 完全脱离 SSH 会话启动 executor
' WScript.Shell.Run 的第3个参数 False = 不等待子进程
Dim oShell
Set oShell = CreateObject("WScript.Shell")
oShell.Run "cmd.exe /c C:\Users\sdw\mhxy_executor\start_executor.cmd", 0, False
Set oShell = Nothing
