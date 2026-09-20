<#
.SYNOPSIS
  用**指定解释器**启动生产 daemon（默认 .venv310），并**先做环境前置检查**再启动。

.BACKGROUND 为什么需要这个包装脚本
  `daemon.py` 里是 `PY = sys.executable`（或 `TRAE_PYTHON` 环境变量）——
  也就是说"用哪个解释器"完全由**启动 daemon 的那条命令**决定。
  因此"从 .venv314 切到 .venv310"= 换一条启动命令；"回退"= 换回去。
  把它固化成一个带参数、带前置检查的脚本，切换与回退都只需改一个 `-Interpreter`。

  前置检查（启动前打印，不通过就**拒绝启动**）:
    · `drl_degrade.probe_runtime()` —— h5i_db / torch / gymnasium / stable_baselines3
      是否在**同一个**解释器里齐备（这是 P0-DRLDEP 的核心判据）；
    · 明确打印将使用哪个解释器, 避免"以为切了其实没切"。

.DAILY ROUTINE
  daemon.py 是长驻进程: **请从真实终端或计划任务启动**，不要经由一次性的工具调用
  （工具调用被取消时，长驻子进程会一起被终止 —— 这是本项目已登记的注意事项）。

.USAGE
  # 默认用 .venv310（具备 h5i_db + torch）
  pwsh -File ops/start_daemon.ps1
  # 回退到旧解释器（5 秒内完成；.venv314 保持原样未动）
  pwsh -File ops/start_daemon.ps1 -Interpreter 314
  # 只做前置检查、不真正启动
  pwsh -File ops/start_daemon.ps1 -CheckOnly
#>
[CmdletBinding()]
param(
  [ValidateSet('310', '314')]
  [string]$Interpreter = '310',
  # [2026-09-20 P2-LAKEROOT] 行情湖根。free_stockdb_sync._lake_root() 读 STOCKDB_ROOT,
  # 未设置时回退 <repo>/data/stockdb —— 而该路径**不存在**, 真实湖在 E:\A_stockDB。
  # 后果是**静默**的(scanned=0, 看起来只是今天没数据), 故在此显式钉住并打印校验结果。
  [string]$StockdbRoot = '',
  # [2026-09-21 引擎闸门] SDK 直连(stockdb.exe)已成为新的生产摄入主路径。它是个
  # **要人手双击启动**的常驻服务 —— 机器重启/被关掉时, run_daily 会静默摄入 0 行,
  # 看起来又只是"今天没数据"。故此处当闸门用: 探针不通过即拒绝启动(exit 4)。
  [switch]$AllowStaleEngine,
  [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

$map = @{
  '310' = @{ Dir = '.venv310'; Note = '具备 h5i_db + torch（推荐；DRL 训练可跑）' }
  '314' = @{ Dir = '.venv314'; Note = '旧环境（无 h5i_db ⇒ fusion 每次都会降级, DRL 会被 L3 门禁拦下）' }
}
$pick = $map[$Interpreter]
$py = Join-Path $RepoRoot "$($pick.Dir)\Scripts\python.exe"

Write-Host "=================================================================="
Write-Host "生产 daemon 启动（解释器 .venv$Interpreter）"
Write-Host "=================================================================="
Write-Host "  python : $py"
Write-Host "  说明   : $($pick.Note)"

if (-not (Test-Path $py)) {
  throw "解释器不存在: $py" + $(if ($Interpreter -eq '310') { "`n   提示: 先运行 scripts/setup_py310_drl_venv.ps1 创建 .venv310" } else { "" })
}

# ---- 前置检查: 必须在同一解释器里齐备（P0-DRLDEP 核心判据）----
Write-Host "`n[前置检查] drl_degrade.probe_runtime()"
$probe = & $py -c @"
import importlib.util as u, sys, json
need = ('h5i_db','torch','gymnasium','stable_baselines3')
got = {m: bool(u.find_spec(m)) for m in need}
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])),
                  'deps': got, 'missing': [k for k,v in got.items() if not v]}))
"@
$p = $probe | ConvertFrom-Json
Write-Host "  python : $($p.python)"
foreach ($k in $p.deps.PSObject.Properties.Name) {
  Write-Host ("  {0,-20} = {1}" -f $k, $p.deps.$k)
}

if ($p.missing.Count -gt 0) {
  if ($Interpreter -eq '310') {
    # .venv310 存在的意义就是"四项齐备"；缺任何一项都说明环境没建好，必须拒绝。
    Write-Host "`n[拒绝启动] .venv310 缺: $($p.missing -join ', ')" -ForegroundColor Red
    Write-Host "  请先运行: pwsh -File scripts/setup_py310_drl_venv.ps1"
    exit 2
  } else {
    # .venv314 缺 h5i_db 是**已知且被接受的**（决策 D 过渡期的回退环境），
    # 故这里是"知情继续"而非拒绝 —— 措辞必须与 310 分支区分开，
    # 否则运维会以为命令失败了（而它其实会照常启动）。
    Write-Host "`n[知情继续] .venv314 缺: $($p.missing -join ', ')" -ForegroundColor Yellow
    Write-Host "  这是**预期**的（该环境本就无 h5i_db）。后果（均已登记）："
    Write-Host "    · fusion 每次都会因『读不到主源』降级到 f_ml（而非因数据不足）;"
    Write-Host "    · DRL 会被决策 D 的 L3 门禁拦下并记 CRITICAL, 当日不产出新信号。"
    Write-Host "  仅当你在执行回退/对比时才应继续。"
  }
} else {
  Write-Host "`n  四项齐备 —— DRL 训练与 target_plan 生成可用。" -ForegroundColor Green
}

# ---- 行情湖根（P2-LAKEROOT）：显式设置 STOCKDB_ROOT，并校验它真的有 kline_parts ----
Write-Host "`n[行情湖] STOCKDB_ROOT"
$root = $StockdbRoot
if (-not $root) { $root = $env:STOCKDB_ROOT }
if (-not $root) {
  # 实测本机真实湖在 E:\A_stockDB（5548 个 kline_parts 分片）；仓库内默认路径不存在。
  $guess = 'E:\A_stockDB'
  if (Test-Path $guess) { $root = $guess }
}
if (-not $root) {
  Write-Host "  [拒绝启动] 未指定行情湖根, 且 E:\A_stockDB 不存在。" -ForegroundColor Red
  Write-Host "  free_stockdb_sync 若拿不到湖, 会**静默**扫到 0 分片(scanned=0), 看起来只是没数据。"
  Write-Host "  请显式指定: pwsh -File ops/start_daemon.ps1 -StockdbRoot <湖根>"
  exit 3
}
$env:STOCKDB_ROOT = $root
$kparts = Join-Path $root 'kline_parts'
$nparts = 0
if (Test-Path $kparts) { $nparts = (Get-ChildItem $kparts -Filter '*.parquet' -ErrorAction SilentlyContinue).Count }
Write-Host "  STOCKDB_ROOT = $root"
Write-Host "  kline_parts  = $kparts  ($nparts 个分片)"
if ($nparts -eq 0) {
  Write-Host "  [拒绝启动] 该湖根下没有 kline_parts 分片 ⇒ 摄入必然是 0 行。" -ForegroundColor Red
  Write-Host "  请先修复上游（free-stockdb 更新器）再启动; 见登记册 P1-DATA-STALE / P2-LAKEROOT。"
  exit 3
}
Write-Host "  (已导出到子进程环境; daemon.py 用 sys.executable 启动 run_daily, 会继承该变量)" -ForegroundColor Green

# ---- 厂商行情引擎（SDK 直连 = 新的生产摄入主路径）----
# 为什么是闸门而不是提示: stockdb.exe 需要**人工双击启动**, 一旦它没跑,
# free_stockdb_sync / engine_bars_sync 都会拿不到数据 —— 而症状是"今天没有新数据",
# 与"今天是节假日"无法从下游区分。这正是 P2-LAKEROOT 的同类病症, 故在此拒绝启动。
Write-Host "`n[厂商引擎] stockdb.exe @ 127.0.0.1:7899 (SDK 直连主路径)"
$probeOut = ''
try {
  $probeOut = (& $py (Join-Path $RepoRoot 'src\engine_bars_sync.py') --probe 2>&1 | Out-String)
} catch {
  $probeOut = "probe exception: $_"
}
$probe = $null
$brace = $probeOut.IndexOf('{')
if ($brace -ge 0) {
  try { $probe = $probeOut.Substring($brace) | ConvertFrom-Json } catch { $probe = $null }
}
if ($null -eq $probe) {
  Write-Host "  [拒绝启动] 无法解析引擎探针输出:" -ForegroundColor Red
  Write-Host "  $probeOut"
  Write-Host "  请确认: ① stockdb.exe 已双击启动并保持运行; ② STOCKDB_ROOT 指向真实湖(含 pybao)。"
  exit 4
}
if (-not $probe.ok) {
  Write-Host "  [拒绝启动] 引擎不可用: $($probe.error)" -ForegroundColor Red
  Write-Host "  怎么办: 双击 E:\A_stockDB\stockdb.exe 启动数据库(它是常驻服务, 请保持运行);"
  Write-Host "          首次使用需先双击 数据更新.exe 并等到『同步完成』。"
  Write-Host "          症状识别: 引擎不在时摄入是 0 行, 与『今天没数据』无法区分 —— 故这里直接拒绝。"
  exit 4
}
Write-Host "  引擎正常: 参考股票全历史覆盖到 $($probe.day) (共 $($probe.trading_days) 个交易日)" -ForegroundColor Green

# 新鲜度: 由 Python 侧 `engine_bars_sync.freshness()` 判定 —— 判据是"引擎是否追平
# **今天之前最后一个已收盘的交易日**"。
# [2026-09-21 订正] 初版在此直接拿 `trade_calendar.json` 的 `last` 比, 那是**官方日历的
# 年尾**(实测 20261231), 与"引擎此刻该有多少数据"语义根本不同 —— 它把**每一次正常启动**
# 都误判成落后(实测: 引擎 20260918 被报"落后于 20261231")。故判据移入 Python 并加测试。
$fresh = $probe.freshness
if ($null -eq $fresh) {
  Write-Host "  [告警] 探针未返回 freshness 字段(版本不一致?), 跳过新鲜度比对。" -ForegroundColor Yellow
} elseif ($fresh.ok) {
  Write-Host "  新鲜度: 引擎($($fresh.engine_day)) 已追平最后已收盘交易日($($fresh.expected_day))。" -ForegroundColor Green
} else {
  Write-Host "  [告警] $($fresh.error)" -ForegroundColor Yellow
  if ($fresh.lag_trading_days) { Write-Host "        落后交易日数: $($fresh.lag_trading_days)" }
  if (-not $AllowStaleEngine) {
    Write-Host "  [拒绝启动] 引擎数据落后于最后已收盘交易日。若确认要以此状态启动, 显式加 -AllowStaleEngine。" -ForegroundColor Red
    Write-Host "  说明: 用陈旧数据起服会让台账里出现无法区分于『正常但无新数据』的日子。"
    exit 4
  }
  Write-Host "  [知情继续] 已显式指定 -AllowStaleEngine。" -ForegroundColor Yellow
}

if ($CheckOnly) {
  Write-Host "`n(-CheckOnly: 仅前置检查, 未启动)"
  exit 0
}

Write-Host "`n[启动] daemon.py（长驻; Ctrl+C 结束）"
Write-Host "提示: 回退只需 `pwsh -File ops/start_daemon.ps1 -Interpreter 314`（.venv314 未改动）`n"
Set-Location $RepoRoot
& $py (Join-Path $RepoRoot 'src\daemon.py')
exit $LASTEXITCODE
