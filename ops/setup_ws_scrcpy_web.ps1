param(
    [string]$InstallDir = "C:\Users\sdw\ws-scrcpy-web",
    [string]$ConfigPath = "C:\ProgramData\WsScrcpyWeb\config.json",
    [int]$WebPort = 8000,
    [string]$TaskName = "ws-scrcpy-web",
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$currentDir = Join-Path $InstallDir "current"
$runBat = Join-Path $InstallDir "run.bat"
$startCmd = Join-Path $currentDir "start.cmd"
$configDir = Split-Path -Parent $ConfigPath

if (-not (Test-Path $startCmd)) {
    throw "ws-scrcpy-web start script not found: $startCmd"
}

if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

$config = [ordered]@{
    installMode = $null
    autoUpdate = $true
    updateCheckIntervalMinutes = 60
    channel = "stable"
    githubOwner = "bilbospocketses"
    serviceFirstRunSeen = $false
    webPort = $WebPort
    firstRunComplete = $true
}

# Use ASCII because ws-scrcpy-web parses this file with JSON.parse() and fails
# on UTF-8 BOM written by some Windows PowerShell versions.
$config | ConvertTo-Json -Compress | Set-Content -Path $ConfigPath -Encoding ASCII

$runLines = @(
    "@echo off",
    "setlocal",
    "",
    "set `"INSTALL_DIR=$InstallDir`"",
    "set `"CURRENT_DIR=%INSTALL_DIR%\current`"",
    "set `"CONFIG_PATH=$ConfigPath`"",
    "set `"PORT=$WebPort`"",
    "set `"WS_SCRCPY_CONFIG=%CONFIG_PATH%`"",
    "",
    "cd /d `"%CURRENT_DIR%`"",
    "call start.cmd >> `"%INSTALL_DIR%\out.log`" 2>>&1"
)
$runLines | Set-Content -Path $runBat -Encoding ASCII

$action = New-ScheduledTaskAction -Execute "cmd" -Argument "/c `"$runBat`""
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Set-ScheduledTask -TaskName $TaskName -Action $action | Out-Null
} else {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Description "Start ws-scrcpy-web at boot" | Out-Null
}

Write-Output "run.bat written: $runBat"
Write-Output "config written: $ConfigPath"
Write-Output "scheduled task action: cmd /c `"$runBat`""

if ($Start) {
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = "cmd.exe /c `"$runBat`""
    } | Out-Null
    Write-Output "ws-scrcpy-web launch requested via WMI"
}
