<#
.SYNOPSIS
  启动/停止/查询 AStockDaemon 服务，并清理探测遗留的垃圾服务（需**管理员**）。

.BACKGROUND 为什么不直接用 `nssm start`
  上一次提权安装时**卡在 `nssm start`** 上：服务已注册（注册表参数全部正确、路径 Test-Path=True），
  但 `daemon_service.out.log`/`.err.log` **从未生成** ⇒ 应用根本没被拉起过，
  服务停在 STOPPED / WIN32_EXIT_CODE=1077(从未启动)。而同一命令**手工前台跑完全正常**。
  `nssm start` 的语义是"启动并等待服务进入 RUNNING"，应用没起来时它会一直等。
  故此处改用 `sc.exe start`（立即返回）+ **轮询**判定，把"等待"变成可观测、可超时的检查。

.用法（管理员）
  pwsh -File ops\service_control.ps1 -Action start
  pwsh -File ops\service_control.ps1 -Action stop
  pwsh -File ops\service_control.ps1 -Action status
#>
[CmdletBinding()]
param(
  [ValidateSet('start', 'stop', 'status', 'restart', 'heal')]
  [string]$Action = 'status',
  [string]$ServiceName = 'AStockDaemon',
  [string[]]$StrayServices = @('__probe_svc__'),
  [int]$TimeoutSec = 90
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$StepLog = Join-Path $LogDir 'service_control.log'

function Step([string]$m) {
  $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
  Add-Content -Path $StepLog -Value $line -Encoding UTF8
  Write-Host $line
}
function DaemonProcs() {
  @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'python' } | Where-Object {
      $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
      $cl -and ($cl -match 'daemon\.py')
    })
}

Step "================ Action=$Action Service=$ServiceName ================"
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
             [Security.Principal.WindowsBuiltInRole]::Administrator)
Step "用户=$($id.Name) 管理员=$isAdmin"
if (-not $isAdmin) { Step '[FAIL] 需要管理员权限'; exit 5 }

# ---- 清理探测遗留的垃圾服务（自己造的垃圾必须自己收）----
foreach ($s in $StrayServices) {
  if (Get-Service -Name $s -ErrorAction SilentlyContinue) {
    Step "清理遗留服务 $s"
    & sc.exe stop $s  | Out-Null
    Start-Sleep -Seconds 2
    & sc.exe delete $s | Out-Null
    Start-Sleep -Seconds 2
    if (Get-Service -Name $s -ErrorAction SilentlyContinue) { Step "  [WARN] $s 仍存在" }
    else { Step "  已删除 $s" }
  }
}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) { Step "[FAIL] 服务 $ServiceName 不存在"; exit 9 }
Step "当前状态: $($svc.Status)"

if ($Action -eq 'status') {
  Step "daemon 进程: $((DaemonProcs).Id -join ', ')"
  exit 0
}

if ($Action -eq 'stop') {
  Step '停止服务'
  & sc.exe stop $ServiceName | Out-Null
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 3; $t += 3
    if ((Get-Service -Name $ServiceName).Status -eq 'Stopped') { break }
  }
  Step "停止后状态: $((Get-Service -Name $ServiceName).Status)"
  Step "残留 daemon 进程: $((DaemonProcs).Id -join ', ')"
  exit 0
}

if ($Action -eq 'restart') {
  Step '重启: 先停'
  & sc.exe stop $ServiceName | Out-Null
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 3; $t += 3
    if ((Get-Service -Name $ServiceName).Status -eq 'Stopped') { break }
  }
  Step "  停止后状态: $((Get-Service -Name $ServiceName).Status)"
}

# ---- 自愈验证: 杀掉 daemon 进程, 看 NSSM 是否按 AppExit=Restart 自动拉起 ----
# 这是服务化**真正的收益**(相对手工进程), 故必须实测, 不能"配了就假设生效"。
if ($Action -eq 'heal') {
  $before = DaemonProcs
  if ($before.Count -eq 0) { Step '[FAIL] 当前没有 daemon 进程, 无法做自愈验证'; exit 9 }
  $victims = $before.Id
  Step "自愈验证: 结束当前 daemon 进程 $($victims -join ', ')（模拟崩溃）"
  $victims | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 4
  Step "  杀掉后残留: $((DaemonProcs).Id -join ', ')"
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 3; $t += 3
    $now = DaemonProcs
    Step "  +{0,3}s 进程={1}" -f $t, $(if ($now.Count) { $now.Id -join ',' } else { '无' })
    if ($now.Count -gt 0) { break }
  }
  $after = DaemonProcs
  if ($after.Count -gt 0) {
    Step "  [OK] NSSM 已自动拉起: $($after.Id -join ', ')"
    Step '--- daemon.log 尾部（应出现新的"守护进程启动"）---'
    Get-Content (Join-Path $LogDir 'daemon.log') -Tail 4 -Encoding UTF8 -ErrorAction SilentlyContinue |
      ForEach-Object { Step "    | $_" }
    Step '================ 自愈验证通过 ================'
    exit 0
  }
  Step '================ 自愈验证失败: 未自动拉起 ================'
  exit 1
}

Step '启动服务（sc.exe start，立即返回）'
& sc.exe start $ServiceName | Out-Null
$t = 0; $final = $null
while ($t -lt $TimeoutSec) {
  Start-Sleep -Seconds 3; $t += 3
  $final = (Get-Service -Name $ServiceName).Status
  $procs = DaemonProcs
  Step "  +{0,3}s 状态={1} daemon进程={2}" -f $t, $final, $(if ($procs.Count) { $procs.Id -join ',' } else { '无' })
  if ($final -eq 'Running' -and $procs.Count -gt 0) { break }
}

$svc = Get-Service -Name $ServiceName
$procs = DaemonProcs
Step "最终状态: $($svc.Status)   StartType=$($svc.StartType)   daemon 进程=$($procs.Id -join ', ')"

$q = (& sc.exe queryex $ServiceName 2>&1 | Out-String)
Step ("sc queryex: " + (($q.Trim() -replace "`r?`n", ' | ')))

foreach ($f in 'daemon_service.out.log', 'daemon_service.err.log') {
  $p = Join-Path $LogDir $f
  if (Test-Path $p) {
    Step "--- $f ({0:N0} B) ---" -f (Get-Item $p).Length
    Get-Content $p -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 12 |
      ForEach-Object { Step "    | $_" }
  } else { Step "--- $f 不存在 ---" }
}
Step '--- daemon.log 尾部 ---'
Get-Content (Join-Path $LogDir 'daemon.log') -Tail 5 -Encoding UTF8 -ErrorAction SilentlyContinue |
  ForEach-Object { Step "    | $_" }

if ($svc.Status -eq 'Running' -and $procs.Count -gt 0) { Step '================ 成功 ================'; exit 0 }
Step '================ 未达预期 ================'
exit 1
