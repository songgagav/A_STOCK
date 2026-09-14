# 一键启动 A_stock_rotation 后台栈:
#   Redis(6379) / Prometheus(9090) / Alertmanager(9093) / 告警 webhook(9111)
#   /metrics(9101) / Celery worker / Grafana(3000) / 估值哨兵守护 / Web 看板(8000)
#
# 用法: powershell -ExecutionPolicy Bypass -File ops/start_obs_stack.ps1
# 幂等: 所有步骤按进程/端口判重, 重复执行不会起第二个; 已在跑的只报 OK。
#
# 历史修复 (2026-09-14):
#   - Redis 原本传 @() 空参数 -> Start-Process 参数校验直接报错, Redis 从未启动,
#     而它是 Celery 的 broker (127.0.0.1:6379), 于是看板的"全量数据库更新"一直不可用。
#   - Prometheus 原本传相对路径 --config.file=prometheus.yml, 但 -WorkingDirectory 是项目根,
#     该文件不在那里 -> 起不来 (9090 从未监听)。改为绝对路径 + rule_files 相对配置目录解析。
#   - 补齐告警链: Prometheus -> Alertmanager(9093) -> ops/alert_hook.py(9111) -> logs/alerts.log。
#   - 补齐 Web 看板(8000), pidfile 复用 logs\dashboard.pid (与 run_services/daemon 同一份)。
$ErrorActionPreference = 'Continue'
$proj = Split-Path -Parent $PSScriptRoot
$obs  = Join-Path $proj '..\obs-stack'          # 与 A_stock_rotation 同级的二进制目录
$py   = 'C:\Users\13372\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe'
$logDir = Join-Path $proj 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Get-PyHit($match) {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match $match }
}

function Ensure-Proc($match, $script, $desc) {
  $hit = Get-PyHit $match
  if ($hit) { Write-Host "OK   $desc 已在运行 (pid=$($hit.ProcessId -join ','))"; return }
  Write-Host "START $desc ..."
  Start-Process -FilePath $py -ArgumentList $script -WorkingDirectory $proj -WindowStyle Hidden
}

# 同 Ensure-Proc, 但把 stdout/stderr 落到 logs\<logName> (常驻守护需要留证据/排错)
function Ensure-ProcLog($match, $script, $desc, $logName) {
  $hit = Get-PyHit $match
  if ($hit) { Write-Host "OK   $desc 已在运行 (pid=$($hit.ProcessId -join ','))"; return }
  $out = Join-Path $logDir $logName
  $err = $out -replace '\.log$', '.err.log'
  Write-Host "START $desc ... -> logs\$logName"
  Start-Process -FilePath $py -ArgumentList $script -WorkingDirectory $proj -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err
}

# 启动外部可执行文件。$argsList 可为 $null/@() (不能把空数组传给 -ArgumentList, 会报参数校验错);
# $workDir 用于让 Redis dump.rdb / Prometheus tsdb 等相对路径落到各自目录, 不污染项目根。
function Ensure-Exe($exePath, $argsList, $desc, $workDir) {
  if (-not (Test-Path $exePath)) { Write-Host "WARN $desc 未安装: $exePath, 跳过"; return }
  $name = [IO.Path]::GetFileNameWithoutExtension($exePath)
  $hit = Get-Process -Name $name -ErrorAction SilentlyContinue
  if ($hit) { Write-Host "OK   $desc 已在运行 (pid=$($hit.Id -join ','))"; return }
  $sp = @{ FilePath = $exePath; WindowStyle = 'Hidden' }
  if ($workDir) { $sp['WorkingDirectory'] = $workDir }
  $a = @($argsList | Where-Object { $_ -ne $null -and "$_" -ne '' })
  if ($a.Count -gt 0) { $sp['ArgumentList'] = $a }
  Write-Host "START $desc ..."
  Start-Process @sp
}

function Test-Port($port) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $ar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
    $ok = $ar.AsyncWaitHandle.WaitOne(800)
    if ($ok) { $c.EndConnect($ar) }
    $c.Close()
    return $ok
  } catch { return $false }
}

Write-Host '== A_stock 后台栈启动检查 =='

# 1) Redis (6379) —— Celery broker/backend, 看板"全量数据库更新"依赖它
$redisDir = Join-Path $obs 'redis'
Ensure-Exe (Join-Path $redisDir 'redis-server.exe') @('redis.windows.conf') 'Redis(6379)' $redisDir

# 2) Prometheus (9090) —— 配置取项目内 ops\prometheus.yml(其 rule_files 相对配置目录解析)
$promDir = Join-Path $obs 'prom\prometheus-2.53.2.windows-amd64'
$promCfg = Join-Path $proj 'ops\prometheus.yml'
$promData = Join-Path $obs 'prom\data'
Ensure-Exe (Join-Path $promDir 'prometheus.exe') `
  @("--config.file=$promCfg", "--storage.tsdb.path=$promData") 'Prometheus(9090)' $promDir

# 3) Alertmanager (9093) —— 收到告警后推给本地 webhook
$amDir = Join-Path $obs 'am\alertmanager-0.27.0.windows-amd64'
Ensure-Exe (Join-Path $amDir 'alertmanager.exe') `
  @("--config.file=$(Join-Path $proj 'ops\alertmanager.yml')") 'Alertmanager(9093)' $amDir

# 4) 告警 webhook 接收端 (9111) —— 落盘 logs\alerts.log, 设了 DING_WEBHOOK_URL 则转发钉钉
Ensure-ProcLog 'alert_hook' 'ops/alert_hook.py --port 9111' 'AlertHook(9111)' 'alert_hook.log'

# 5) /metrics (9101) —— Prometheus 的抓取目标 (job=astock-db)
Ensure-Proc 'metrics_server' 'src/metrics_server.py --port 9101' 'Metrics(9101)'

# 6) Celery worker (solo pool, Windows 必需)
Ensure-Proc 'celery.*tasks_db|tasks_db.*worker' 'src/tasks_db.py --worker' 'Celery worker'
if (-not (Get-PyHit 'celery')) {
  Start-Process -FilePath $py -ArgumentList '-m','celery','-A','src.tasks_db','worker','--pool=solo','-l','warning','--without-gossip','--without-mingle','--without-heartbeat' -WorkingDirectory $proj -WindowStyle Hidden
  Write-Host 'START Celery worker (solo) ...'
}

# 7) Grafana (3000)
$gd = Join-Path $obs 'grafana\grafana-v11.1.0'
$gExe = Join-Path $gd 'bin\grafana-server.exe'
if (Test-Path $gExe) { Ensure-Exe $gExe @('--homepath',$gd,'server') 'Grafana(3000)' $gd } else { Write-Host 'WARN grafana 未安装, 跳过' }

# 8) 估值覆盖率哨兵守护 (每日 18:30 体检; 报出缺口才补 pe_ttm 补丁)
#    见 docs/patch-retirement-watch.md —— 补丁退役需要连续的每日哨兵证据。
Ensure-ProcLog 'sentinel_daemon' 'src/sentinel_daemon.py' 'Sentinel(估值覆盖率,每日18:30)' 'sentinel_daemon.log'

# 9) Web 看板 (8000) —— pidfile 与 run_services.py / daemon.py 共用 logs\dashboard.pid
Ensure-ProcLog 'dashboard\.py' 'src/dashboard.py --port 8000' 'Dashboard(8000)' 'dashboard.log'

Start-Sleep -Seconds 6
Write-Host ''
Write-Host '== 端口检查 =='
$ports = @(
  @{p=6379; d='Redis'}, @{p=9090; d='Prometheus'}, @{p=9093; d='Alertmanager'},
  @{p=9111; d='AlertHook'}, @{p=9101; d='Metrics'}, @{p=3000; d='Grafana'},
  @{p=8000; d='Dashboard'}
)
foreach ($x in $ports) {
  $ok = Test-Port $x.p
  Write-Host ("  port {0,-6} {1,-14} {2}" -f $x.p, $x.d, $(if ($ok) { 'OPEN' } else { 'closed' }))
}
Write-Host ''
Write-Host '完成:'
Write-Host '  Web 看板     http://localhost:8000'
Write-Host '  Grafana      http://localhost:3000  (admin/admin)'
Write-Host '  Prometheus   http://localhost:9090'
Write-Host '  Alertmanager http://localhost:9093'
Write-Host ''
Write-Host '  日志: logs\dashboard.log / sentinel_daemon.log / alert_hook.log / alerts.log'
Write-Host '  状态: data\sentinel_last.json (哨兵)  logs\dashboard.pid (看板)'
