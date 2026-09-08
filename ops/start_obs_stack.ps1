# 一键启动 A_stock_rotation 观测栈 (Redis / Prometheus / Grafana / /metrics / Celery worker)
# 用法: powershell -ExecutionPolicy Bypass -File ops/start_obs_stack.ps1
$ErrorActionPreference = 'Continue'
$proj = Split-Path -Parent $PSScriptRoot
$obs  = Join-Path $proj '..\obs-stack'          # 与 A_stock_rotation 同级的二进制目录
$py   = 'C:\Users\13372\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe'

function Ensure-Proc($match, $script, $desc) {
  $hit = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match $match }
  if ($hit) { Write-Host "OK   $desc 已在运行 (pid=$($hit.ProcessId -join ','))"; return }
  Write-Host "START $desc ..."
  Start-Process -FilePath $py -ArgumentList $script -WorkingDirectory $proj -WindowStyle Hidden
}

function Ensure-Exe($exePath, $argsList, $desc) {
  $name = [IO.Path]::GetFileNameWithoutExtension($exePath)
  $hit = Get-Process -Name $name -ErrorAction SilentlyContinue
  if ($hit) { Write-Host "OK   $desc 已在运行 (pid=$($hit.Id -join ','))"; return }
  Write-Host "START $desc ..."
  Start-Process -FilePath $exePath -ArgumentList $argsList -WindowStyle Hidden
}

Write-Host '== A_stock 观测栈启动检查 =='
# 1) Redis (Windows 移植 5.0.14)
Ensure-Exe (Join-Path $obs 'redis\redis-server.exe') @() 'Redis(6379)'
# 2) Prometheus
Ensure-Exe (Join-Path $obs 'prom\prometheus-2.53.2.windows-amd64\prometheus.exe') @('--config.file=prometheus.yml','--storage.tsdb.path=prom/data') 'Prometheus(9090)'
# 3) /metrics (python)
Ensure-Proc 'metrics_server' 'src/metrics_server.py --port 9101' 'Metrics(9101)'
# 4) Celery worker
Ensure-Proc 'celery.*tasks_db|tasks_db.*worker' 'src/tasks_db.py --worker' 'Celery worker' 
if (-not (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'celery' })) {
  Start-Process -FilePath $py -ArgumentList '-m','celery','-A','src.tasks_db','worker','--pool=solo','-l','warning','--without-gossip','--without-mingle','--without-heartbeat' -WorkingDirectory $proj -WindowStyle Hidden
  Write-Host 'START Celery worker (solo) ...'
}
# 5) Grafana
$gd = Join-Path $obs 'grafana\grafana-v11.1.0'
$gExe = Join-Path $gd 'bin\grafana-server.exe'
if (Test-Path $gExe) { Ensure-Exe $gExe @('--homepath',$gd,'server') 'Grafana(3000)' } else { Write-Host 'WARN grafana 未安装, 跳过' }

Start-Sleep -Seconds 4
Write-Host '== 端口检查 =='
foreach($port in 6379,9090,9101,3000){
  $ok = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue
  Write-Host ("  port {0}: {1}" -f $port, $(if($ok.TcpTestSucceeded){'OPEN'}else{'closed'}))
}
Write-Host '完成. Grafana: http://localhost:3000 (admin/admin)  Prometheus: http://localhost:9090'
