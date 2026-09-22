<#
.SYNOPSIS
  启动/停止/查询 AStockDaemon 服务，并清理探测遗留的垃圾服务（需**管理员**）。
  [2026-09-22] 同时托管**行情引擎** AStockStockdb（stockdb.exe），两者一起起停。

.BACKGROUND 为什么不直接用 `nssm start`
  上一次提权安装时**卡在 `nssm start`** 上：服务已注册（注册表参数全部正确、路径 Test-Path=True），
  但 `daemon_service.out.log`/`.err.log` **从未生成** ⇒ 应用根本没被拉起过，
  服务停在 STOPPED / WIN32_EXIT_CODE=1077(从未启动)。而同一命令**手工前台跑完全正常**。
  `nssm start` 的语义是"启动并等待服务进入 RUNNING"，应用没起来时它会一直等。
  故此处改用 `sc.exe start`（立即返回）+ **轮询**判定，把"等待"变成可观测、可超时的检查。

.BACKGROUND 为什么要和 stockdb 一起管（P0-DATASRC-STOCKDB）
  stockdb.exe 是生产摄入主路径的硬依赖，但此前只能**人工双击启动**。
  2026-09-22 它没跑 ⇒ 11:31 起盘中引擎静默停摆、下午无行情无撮合，而下游症状
  与"今天是节假日"**无法区分**。daemon 由 NSSM 托管、stockdb 靠人记得双击，
  两者可靠性差一个量级 —— 故并入同一控制面，并保证**启动顺序**（先引擎后守护）:
  daemon 启动时会主动探活 stockdb，探不到就 exit 4 拒绝启动；
  先起 stockdb 才能让这条闸门通过，而不是让守护带着错误码反复重启。

  注: 两者**不建立 SCM 静态依赖**。静态依赖会让 SCM"等待"依赖项，而 daemon 又
  断言依赖项必须已就绪 —— 叠加起来是启动死锁。顺序由本脚本保证。

.用法（管理员）
  pwsh -File ops\service_control.ps1 -Action start      # 先 stockdb 后 daemon
  pwsh -File ops\service_control.ps1 -Action stop       # 先 daemon 后 stockdb
  pwsh -File ops\service_control.ps1 -Action status
  pwsh -File ops\service_control.ps1 -Action start -SkipStockdb   # 只管 daemon
#>
[CmdletBinding()]
param(
  [ValidateSet('start', 'stop', 'status', 'restart', 'heal', 'diag')]
  [string]$Action = 'status',
  [string]$ServiceName = 'AStockDaemon',
  # 行情引擎服务名（由 ops/install_stockdb_service.ps1 注册）
  [string]$StockdbService = 'AStockStockdb',
  # 只管 daemon、不碰行情引擎（调试或引擎单独维护时用）
  [switch]$SkipStockdb,
  # 引擎端点：start 时轮询它就绪（与 daemon 的启动闸门同一端口）
  [int]$EnginePort = 7899,
  [string[]]$StrayServices = @('__probe_svc__'),
  [string]$DiagDump = '',
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

# ---- 行情引擎（stockdb.exe）辅助：**必须定义在用到它的分支之前** ----
# PowerShell 的函数**不提升**（脚本按行执行），而 `status` 分支在文件前部就 exit 了。
# 实测把函数留在文件后部时, `status` 会报 `EngineListening is not recognized`
# —— 于是"引擎不在监听"这一行永远显示为空, 恰好把本功能想暴露的故障又藏了回去。
function EngineListening([int]$port) {
  return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function EnsureStockdbRunning() {
  if ($SkipStockdb) { Step '[行情引擎] 已指定 -SkipStockdb，不管理'; return $true }
  if (-not (Get-Service -Name $StockdbService -ErrorAction SilentlyContinue)) {
    Step "[行情引擎] 服务 $StockdbService 不存在 —— 请先跑 ops\install_stockdb_service.ps1"
    Step '            （本次继续启动 daemon；daemon 自身的引擎闸门会判定是否放行）'
    return $false
  }
  $st = (Get-Service -Name $StockdbService).Status
  if ($st -ne 'Running') {
    Step "[行情引擎] $StockdbService 状态=$st -> sc.exe start（立即返回，稍后轮询）"
    & sc.exe start $StockdbService | Out-Null
  } else {
    Step "[行情引擎] $StockdbService 已在运行"
  }
  # 判定必须**同时**满足"服务 Running"与"端口在监听"。
  # 实测(NSSM + 控制台型应用): 服务先经 StartPending, 约 **18 秒**才转 RUNNING,
  # 而端口在转 RUNNING **之前**就已监听 —— 只等端口会在服务仍 StartPending 时
  # 就宣布就绪; 只等服务状态又会在端口还没起时就往下走。两者都要。
  $t = 0
  while ($t -lt 90) {
    $s2 = (Get-Service -Name $StockdbService).Status
    $listen = EngineListening -port $EnginePort
    if ($s2 -eq 'Running' -and $listen) {
      Step "  [OK] 服务 Running 且 $EnginePort 已监听（+${t}s）"
      return $true
    }
    Start-Sleep -Seconds 2; $t += 2
  }
  $sFinal = (Get-Service -Name $StockdbService).Status
  Step "  [FAIL] +${t}s 未就绪: 服务状态=$sFinal $EnginePort 监听=$(EngineListening -port $EnginePort)"
  Step '         查 logs\stockdb_service.err.log 与 E:\A_stockDB\log.txt'
  return $false
}

function StopStockdbService() {
  if ($SkipStockdb) { return }
  if (-not (Get-Service -Name $StockdbService -ErrorAction SilentlyContinue)) { return }
  Step "[行情引擎] 停止 $StockdbService"
  & sc.exe stop $StockdbService | Out-Null
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 2; $t += 2
    if ((Get-Service -Name $StockdbService).Status -eq 'Stopped') { break }
  }
  Step "  停止后状态: $((Get-Service -Name $StockdbService).Status)"
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
  # 行情引擎同屏报出 —— 它是本控制面托管的第二个服务。只看 daemon 会漏掉
  # 2026-09-22 那类故障: daemon 一切正常, 引擎却不在监听。
  if (Get-Service -Name $StockdbService -ErrorAction SilentlyContinue) {
    $sg = Get-Service -Name $StockdbService
    $listening = EngineListening -port $EnginePort
    Step "行情引擎 ${StockdbService}: 状态=$($sg.Status) StartType=$($sg.StartType) ${EnginePort} 监听=$listening"
    if ($sg.Status -ne 'Running' -or -not $listening) {
      Step '  [WARN] 引擎不在监听 => 摄入为 0 行, 且症状与"今天没数据"无法区分 (P0-DATASRC-STOCKDB)'
    }
  } else {
    Step "行情引擎 ${StockdbService}: 服务不存在（未托管）—— 跑 ops\install_stockdb_service.ps1 注册"
  }
  exit 0
}

# ---- 诊断: 提权才能读到 LocalSystem 进程的命令行 (非提权下 CommandLine 为空) ----
# 用途: 查清 daemon 名下不断累积的子进程**到底是什么** —— 进程泄漏只报数量没用,
# 必须知道是哪个组件在漏, 才能决定"补模块"还是"关掉它"。
if ($Action -eq 'diag') {
  $dump = if ($DiagDump) { $DiagDump } else { Join-Path $LogDir 'service_diag.txt' }
  $lines = New-Object System.Collections.Generic.List[string]
  function W([string]$s) { $lines.Add($s); Write-Host $s }

  W ("诊断时间: " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
  W ("服务 $ServiceName 状态: " + (Get-Service -Name $ServiceName).Status)

  $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
  $svcHost = (Get-CimInstance Win32_Process -Filter "Name='nssm.exe'" -ErrorAction SilentlyContinue |
              Select-Object -First 1).ProcessId
  W ("nssm 宿主 pid: " + $svcHost)

  # daemon 进程(带命令行)
  W "`n--- daemon.py 进程 ---"
  $daemons = @($all | Where-Object { $_.CommandLine -and $_.CommandLine -match 'daemon\.py' })
  foreach ($d in $daemons) {
    W ("  pid={0,-7} ppid={1,-7} 创建={2}" -f $d.ProcessId, $d.ParentProcessId,
       $(if ($d.CreationDate) { $d.CreationDate.ToString('MM-dd HH:mm:ss') } else { '?' }))
    W ("      {0}" -f $d.CommandLine)
  }
  $dpid = ($daemons | Sort-Object CreationDate | Select-Object -Last 1).ProcessId
  W ("取最新 daemon pid = " + $dpid)

  # daemon 的子进程: 这是泄漏的现场
  W "`n--- daemon 子进程按命令行归类 ---"
  $kids = @($all | Where-Object { $_.ParentProcessId -eq $dpid })
  W ("  子进程总数 = " + $kids.Count)
  $groups = $kids | Group-Object {
    $cl = $_.CommandLine
    if (-not $cl) { return '<命令行不可读>' }
    $cl
  } | Sort-Object Count -Descending
  foreach ($g in $groups) {
    W ("  {0,3} 个 x {1}" -f $g.Count, $(if ($g.Name.Length -gt 150) { $g.Name.Substring(0, 150) } else { $g.Name }))
  }

  # 逐个子进程明细(含创建时间, 判断泄漏节奏)
  W "`n--- daemon 子进程明细(按创建时间) ---"
  foreach ($k in ($kids | Sort-Object CreationDate)) {
    $mem = try { [math]::Round((Get-Process -Id $k.ProcessId -ErrorAction Stop).WorkingSet64 / 1MB, 1) } catch { 0 }
    W ("  {0}  pid={1,-7} ppid={2,-7} {3,7} MB  {4}" -f
       $(if ($k.CreationDate) { $k.CreationDate.ToString('HH:mm:ss') } else { '?' }),
       $k.ProcessId, $k.ParentProcessId, $mem,
       $(if ($k.CommandLine -and $k.CommandLine.Length -gt 110) { $k.CommandLine.Substring(0, 110) } else { $k.CommandLine }))
  }

  # 全机 python 概览
  W "`n--- 全机 python 进程概览 ---"
  $pys = @($all | Where-Object { $_.Name -match 'python' })
  W ("  总数 = " + $pys.Count)
  $byParent = $pys | Group-Object ParentProcessId | Sort-Object Count -Descending | Select-Object -First 10
  foreach ($g in $byParent) {
    $par = $all | Where-Object { $_.ProcessId -eq [int]$g.Name } | Select-Object -First 1
    W ("  父 pid={0,-7} ({1}) -> {2} 个子进程" -f $g.Name, $(if ($par) { $par.Name } else { '已退出' }), $g.Count)
  }

  # 端口占用(观测栈冲突现场)
  W "`n--- 关键端口占用 ---"
  foreach ($port in 3000, 9090, 9093, 9101, 6379, 5555, 8123, 8000) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) {
      $pr = $all | Where-Object { $_.ProcessId -eq $c.OwningProcess } | Select-Object -First 1
      W ("  {0,-6} 被 pid={1,-7} ({2}) 占用 创建={3}" -f $port, $c.OwningProcess,
         $(if ($pr) { $pr.Name } else { '?' }),
         $(if ($pr -and $pr.CreationDate) { $pr.CreationDate.ToString('MM-dd HH:mm:ss') } else { '?' }))
    } else { W ("  {0,-6} 空闲" -f $port) }
  }

  [System.IO.File]::WriteAllLines($dump, $lines, (New-Object System.Text.UTF8Encoding($true)))
  Write-Host "`n已写出: $dump"
  exit 0
}

# ---- 行情引擎（stockdb.exe）启停：**先引擎后守护** ----
# （辅助函数 EngineListening / EnsureStockdbRunning / StopStockdbService 已提到
#   文件前部定义 —— PowerShell 函数不提升, 而 status 分支在前部就 exit 了。）

if ($Action -eq 'stop') {
  Step '停止服务（顺序: 先 daemon 后行情引擎）'
  & sc.exe stop $ServiceName | Out-Null
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 3; $t += 3
    if ((Get-Service -Name $ServiceName).Status -eq 'Stopped') { break }
  }
  Step "停止后状态: $((Get-Service -Name $ServiceName).Status)"
  Step "残留 daemon 进程: $((DaemonProcs).Id -join ', ')"
  StopStockdbService
  exit 0
}

if ($Action -eq 'restart') {
  Step '重启: 先停（顺序: 先 daemon 后行情引擎）'
  & sc.exe stop $ServiceName | Out-Null
  $t = 0
  while ($t -lt $TimeoutSec) {
    Start-Sleep -Seconds 3; $t += 3
    if ((Get-Service -Name $ServiceName).Status -eq 'Stopped') { break }
  }
  Step "  停止后状态: $((Get-Service -Name $ServiceName).Status)"
  if (-not $SkipStockdb) {
    StopStockdbService
    EnsureStockdbRunning | Out-Null
  }
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
# 顺序: **先行情引擎, 后守护**。daemon 启动时会探活 stockdb, 探不到就 exit 4
# 拒绝启动（ops/start_daemon.ps1 的引擎闸门）—— 先起引擎才能让那条闸门通过,
# 而不是让守护带着错误码反复重启。
$engineOk = EnsureStockdbRunning
if (-not $engineOk) {
  Step '[WARN] 行情引擎未就绪 —— daemon 很可能因引擎闸门而拒绝启动（exit 4）'
}
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
# 行情引擎一并报出 —— 它是本控制面托管的第二个服务, 状态必须同屏可见
if (-not $SkipStockdb -and (Get-Service -Name $StockdbService -ErrorAction SilentlyContinue)) {
  $sg = Get-Service -Name $StockdbService
  $listening = EngineListening -port $EnginePort
  Step "行情引擎: $StockdbService 状态=$($sg.Status)  $EnginePort 监听=$listening"
  if ($sg.Status -ne 'Running' -or -not $listening) {
    Step '  [WARN] 行情引擎未就绪 —— 摄入将是 0 行, 且症状与"今天没数据"无法区分'
  }
}

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
