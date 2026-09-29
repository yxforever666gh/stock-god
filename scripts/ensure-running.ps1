param(
    [ValidateSet('Install','Run','Once')][string]$Mode = 'Run'
)
$ErrorActionPreference = 'Stop'
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskName = 'StockGod-0900-EnsureRunning'
$logPath = Join-Path $projectDirectory 'runtime\logs\ensure-running.log'

function Write-CheckLog([string]$message) {
    $directory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    Add-Content -LiteralPath $logPath -Encoding utf8 -Value "$(Get-Date -Format o) $message"
}

function Invoke-Check {
    try {
        $output = & (Join-Path $PSScriptRoot 'release.ps1') -Command ensure | Out-String
        $result = $output | ConvertFrom-Json
        if (-not $result.ready.readiness.ready -or -not $result.ready.pid) {
            throw 'Release command did not return a ready process.'
        }
        Write-CheckLog "action=$($result.action) version=$($result.ready.appVersion) pid=$($result.ready.pid)"
        return $true
    } catch {
        Write-CheckLog "action=failed reason=$($_.Exception.Message)"
        return $false
    }
}

if ($Mode -eq 'Install') {
    $pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Mode Run'
    $action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments -WorkingDirectory $projectDirectory
    $trigger = New-ScheduledTaskTrigger -Daily -At 09:00
    $trigger.EndBoundary = '2099-12-31T23:59:59'
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and (
        $existing.Actions.Execute -ne $action.Execute -or
        $existing.Actions.Arguments -ne $action.Arguments
    )) {
        throw "An unrelated task already uses $taskName."
    }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-CheckLog "action=installed task=$taskName time=09:00"
    Get-ScheduledTask -TaskName $taskName
    return
}

if ($Mode -eq 'Once') {
    if (-not (Invoke-Check)) { exit 1 }
    return
}

$checkedDay = $null
while ($true) {
    $now = Get-Date
    if ($null -eq $checkedDay -or (
        $now.Date -gt $checkedDay -and $now.TimeOfDay -ge [TimeSpan]::FromHours(9)
    )) {
        $checkedDay = $now.Date
        Invoke-Check | Out-Null
    }
    Start-Sleep -Seconds 30
}
