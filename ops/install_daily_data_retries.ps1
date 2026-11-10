[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PowerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PowerShell)) { throw 'Windows PowerShell 5.1 was not found.' }
$LogPath = Join-Path $RepoRoot 'logs\install_daily_data_retries.log'
New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) -Force | Out-Null
Start-Transcript -Path $LogPath -Append | Out-Null
Write-Output "Installer identity=$([Security.Principal.WindowsIdentity]::GetCurrent().Name) elevated=$([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

function New-DailyTask([string]$Name, [string]$ScriptPath, [datetime[]]$Times, [int]$MaxHours) {
  if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "Task script missing: $ScriptPath" }
  $Action = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" -WorkingDirectory $RepoRoot
  $Triggers = @($Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
  $Principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount -RunLevel Limited
  $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours $MaxHours)
  Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Triggers -Principal $Principal -Settings $Settings -Description 'A-STOCK idempotent daily H5i auxiliary data refresh; writes data receipts only.' -Force | Out-Null
}

$Today = (Get-Date).Date
$FinancialTimes = @((0..4) | ForEach-Object { $Today.AddMinutes(20).AddHours(2 * $_) })
$MarginTimes = @((0..4) | ForEach-Object { $Today.AddHours(6).AddMinutes(30 + 30 * $_) })

try {
  New-DailyTask -Name 'AStockH5iFinancialsSync' -ScriptPath (Join-Path $PSScriptRoot 'run_h5i_financials_sync.ps1') -Times $FinancialTimes -MaxHours 3
  New-DailyTask -Name 'AStockMarginMorningRetry' -ScriptPath (Join-Path $PSScriptRoot 'run_margin_morning_retry.ps1') -Times $MarginTimes -MaxHours 1

  $TaskNames = @('AStockH5iFinancialsSync', 'AStockMarginMorningRetry')
  $TaskSummary = $TaskNames | ForEach-Object {
    $Task = Get-ScheduledTask -TaskName $_ -ErrorAction Stop
    $Info = Get-ScheduledTaskInfo $Task
    [pscustomobject]@{
      TaskName = $Task.TaskName
      State = $Task.State
      Principal = $Task.Principal.UserId
      LogonType = $Task.Principal.LogonType
      RunLevel = $Task.Principal.RunLevel
      Action = $Task.Actions.Execute
      Arguments = $Task.Actions.Arguments
      TriggerCount = $Task.Triggers.Count
      TriggerTimes = @($Task.Triggers | ForEach-Object { $_.StartBoundary })
      NextRunTime = $Info.NextRunTime
      MultipleInstances = $Task.Settings.MultipleInstances
    }
  } | ConvertTo-Json -Depth 4
  Write-Output $TaskSummary
  Stop-Transcript | Out-Null
} catch {
  Write-Error $_
  Stop-Transcript | Out-Null
  exit 1
}
