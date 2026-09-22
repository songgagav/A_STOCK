# ============================================================
# install_stockdb_service.ps1 -- 用 NSSM 把厂商行情引擎 stockdb.exe 注册为 Windows 服务
#
# 为什么需要它（2026-09-22 的真实事故，见登记册 P0-DATASRC-STOCKDB）
# ---------------------------------------------------------------
# stockdb.exe 是**生产摄入主路径的硬依赖**（SDK 直连，127.0.0.1:7899），
# 但它此前只能**人工双击启动**。2026-09-22 它没跑：11:31 起 h5i 拿不到数据，
# 盘中引擎静默停摆、下午既无行情也无撮合；而下游症状是"今天没有新数据"，
# 与"今天是节假日"**无法区分** —— 这正是本仓反复吃亏的那类静默失效。
# daemon 是 NSSM 托管的，stockdb 却靠人记得双击，两者可靠性差了一个量级。
# 本脚本把它补成服务，然后交给 `ops/service_control.ps1` 与 daemon **一起**起停。
#
# 信任状态（务必知情）
# -------------------
# 同 `install_daemon_service.ps1`：`nssm.exe` 的 Authenticode = **NotSigned**
# （NSSM 2.24 / 2014 年）。故这里同样校验 **SHA256** 并**显式要求**
# `-IAcceptUnsignedNssm` —— 哈希只证明"同一来源未被替换"，不能替代代码签名。
# 本机实测 sha256 = 472232CA...B6481C（win32 版；win64 见下）。
#
# 与 daemon 的关系
# ----------------
# **不建立 SCM 静态依赖**（`nssm set AStockDaemon DependOnService AStockStockdb`）：
# daemon 在启动时会**主动探活** stockdb，若探不到就退出码 4 拒绝启动
# （见 ops/start_daemon.ps1）。建立静态依赖会让 SCM **等待**依赖项，
# 而 daemon 又断言依赖项必须已就绪 —— 两者叠加会变成启动死锁。
# 取而代之：由 `service_control.ps1` 保证**启动顺序**（先 stockdb 后 daemon），
# 并由 daemon 的运行期巡检发现"起来之后又掉了"。
#
# 用法
# ----
#   pwsh -File ops\install_stockdb_service.ps1 -IAcceptUnsignedNssm -Start
#   pwsh -File ops\install_stockdb_service.ps1 -Uninstall
# ============================================================
[CmdletBinding()]
param(
  [string]$ServiceName = 'AStockStockdb',
  [string]$DisplayName = 'A-STOCK 行情引擎 (stockdb.exe)',
  # 工作目录必须与 exe 同目录: stockdb.conf 里 work_dir/pidfile 都是 **相对路径**
  # （./data、./data1、./log.txt、./lgdb.pid）。换了工作目录它就读不到库。
  [string]$StockdbRoot = 'E:\A_stockDB',
  [switch]$Uninstall,
  [switch]$Start,
  [switch]$IAcceptUnsignedNssm
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn2($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }

# nssm 沿用 daemon 安装脚本的位置（工作区根的 _tools）
$ToolsDir = Join-Path (Split-Path -Parent $RepoRoot) '_tools'
$NssmExe = Join-Path $ToolsDir 'nssm-2.24\win64\nssm.exe'
if (-not (Test-Path $NssmExe)) { $NssmExe = Join-Path $ToolsDir 'nssm-2.24\win32\nssm.exe' }
$Exe = Join-Path $StockdbRoot 'stockdb.exe'
$LogDir = Join-Path $RepoRoot 'logs'

function Run($file, $argv) {
  $out = & $file @argv 2>&1
  return $out
}

Step "================ 开始 (Service=$ServiceName Uninstall=$Uninstall Start=$Start) ================"

# ---- 前置 ----
Step '前置检查'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Bad '需要管理员权限'; exit 5 }
Ok '管理员权限'
if (-not (Test-Path $NssmExe)) { Bad "nssm.exe 不存在: $NssmExe（请先跑 install_daemon_service.ps1 下载）"; exit 9 }
$eh = (Get-FileHash $NssmExe -Algorithm SHA256).Hash
Ok "nssm.exe sha256 = $eh"
$sig = (Get-AuthenticodeSignature $NssmExe).Status
if ($sig -ne 'Valid') {
  Warn2 "nssm.exe 未签名（Authenticode=$sig）；哈希只证明来源未被替换，不能替代代码签名"
  if (-not $IAcceptUnsignedNssm) { Bad '需显式 -IAcceptUnsignedNssm'; exit 7 }
  Ok '已显式接受未签名二进制'
}
if (-not (Test-Path $Exe)) { Bad "stockdb.exe 不存在: $Exe"; exit 9 }
Ok "stockdb.exe: $Exe ($([math]::Round((Get-Item $Exe).Length/1MB,2)) MB)"
if (-not (Test-Path (Join-Path $StockdbRoot 'stockdb.conf'))) {
  Bad "stockdb.conf 不存在于 $StockdbRoot —— 工作目录错了就读不到库"; exit 9
}
Ok 'stockdb.conf 就位（work_dir/pidfile 为相对路径 ⇒ AppDirectory 必须是此处）'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

# ---- 卸载 ----
if ($Uninstall) {
  Step "卸载服务 $ServiceName"
  if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Run $NssmExe @('stop', $ServiceName) | Out-Null
    Start-Sleep -Seconds 3
    Run $NssmExe @('remove', $ServiceName, 'confirm') | Out-Null
    Start-Sleep -Seconds 2
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { Bad '服务仍存在'; exit 8 }
    Ok "已卸载 $ServiceName"
  } else { Step '  服务不存在' }
  Step '================ 结束 ================'
  exit 0
}

# ---- 清掉手工实例（避免与服务实例抢 7899 与 leveldb 锁）----
Step '停止手工启动的 stockdb 实例（避免双实例抢端口与 leveldb 锁）'
$killed = 0
Get-Process -Name 'stockdb' -ErrorAction SilentlyContinue | ForEach-Object {
  Warn2 "  结束手工实例 pid=$($_.Id)"
  Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
  $killed++
}
if ($killed -eq 0) { Ok '无手工实例' } else { Start-Sleep -Seconds 3; Ok "已结束 $killed 个" }

# ---- 安装 ----
Step "安装服务 $ServiceName"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Step '  服务已存在 -> 先移除（幂等）'
  Run $NssmExe @('stop', $ServiceName) | Out-Null
  Start-Sleep -Seconds 2
  Run $NssmExe @('remove', $ServiceName, 'confirm') | Out-Null
  Start-Sleep -Seconds 2
}
Run $NssmExe @('install', $ServiceName, $Exe) | Out-Null
$pairs = @(
  @('AppDirectory', $StockdbRoot),
  @('DisplayName', $DisplayName),
  @('Description', '厂商行情引擎(stockdb.exe @127.0.0.1:7899): 生产摄入主路径的硬依赖; 由 NSSM 托管, 与 AStockDaemon 一起起停'),
  @('Start', 'SERVICE_AUTO_START'),
  # 退出即重启: 它是常驻服务, 正常情况不该退出。5 秒延迟避免崩溃风暴。
  @('AppExit', 'Default', 'Restart'),
  @('AppRestartDelay', '5000'),
  # 崩溃风暴保护: 1 小时内重启超过 20 次则**放弃**并置为停止
  # （不设它的话 flapping 会掩盖真故障, 与本仓"响亮失败"的纪律相反）
  @('AppThrottle', '180000'),
  @('AppStdout', (Join-Path $LogDir 'stockdb_service.out.log')),
  @('AppStderr', (Join-Path $LogDir 'stockdb_service.err.log')),
  @('AppRotateFiles', '1'),
  @('AppRotateBytes', '10485760'),
  @('AppStopMethodConsole', '15000'),
  @('AppStopMethodWindow', '5000')
)
foreach ($p in $pairs) { Run $NssmExe @(,@('set', $ServiceName) + $p) | Out-Null }
Ok "已注册 $ServiceName (AppDirectory=$StockdbRoot, 自动启动, 退出重启)"

$svc = Get-Service -Name $ServiceName
if ($svc.StartType -ne 'Automatic') { Bad "StartType=$($svc.StartType) 期望 Automatic"; exit 8 }
Ok "StartType=Automatic"

# ---- 启动并验证「服务形态下真的能用」----
if ($Start) {
  Step '启动服务并验证'
  Run $NssmExe @('start', $ServiceName) | Out-Null
  $ok = $false
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    $c = Get-NetTCPConnection -LocalPort 7899 -State Listen -ErrorAction SilentlyContinue
    if ($c) { $ok = $true; break }
  }
  if (-not $ok) { Bad '服务已启动但 7899 未监听 —— 查 logs\stockdb_service.err.log'; exit 10 }
  Ok '7899 已在监听'
  # 关键: 用**服务身份**跑真探针。端口开了不等于 SDK 能取数
  # （P1-ENGINEDEP: 引擎不在时摄入返回 0 行, 与"今天没数据"无法区分）。
  $py = Join-Path $RepoRoot '.venv310\Scripts\python.exe'
  if (-not (Test-Path $py)) { $py = Join-Path $RepoRoot '.venv314\Scripts\python.exe' }
  $probeOut = (& $py (Join-Path $RepoRoot 'src\engine_bars_sync.py') --probe 2>&1 | Out-String)
  $brace = $probeOut.IndexOf('{')
  $probe = $null
  if ($brace -ge 0) { try { $probe = $probeOut.Substring($brace) | ConvertFrom-Json } catch { } }
  if ($null -eq $probe -or -not $probe.ok) {
    Bad "服务在跑但探针失败：$(if($probe){$probe.error}else{'探针无输出'})"
    exit 10
  }
  Ok "探针通过: 数据到 $($probe.day)（$($probe.trading_days) 个交易日）"
  if ($probe.freshness -and -not $probe.freshness.ok) {
    Warn2 "数据新鲜度: $($probe.freshness.error)"
    Warn2 "  这**不阻塞服务托管**（服务起来正是为了让更新器有机会补数），但下游会拒绝带陈旧数据启动"
  }
}

Step '================ 结束 ================'
Write-Host "`n后续:" -ForegroundColor Cyan
Write-Host "  一起起停: pwsh -File ops\service_control.ps1 -Action start|stop|status"
Write-Host "  单独控制: nssm start|stop|restart $ServiceName"
Write-Host "  日志:     logs\stockdb_service.out.log / stockdb_service.err.log"
Write-Host "  引擎自身: $StockdbRoot\log.txt"
