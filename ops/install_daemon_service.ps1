<#
.SYNOPSIS
  用 NSSM 把 A_stock_rotation 的 daemon 注册为 Windows 服务（需**管理员**）。

.BACKGROUND
  手工 Start-Process 起的 daemon 在本会话内能跨调用存活，但**能否活过会话结束/重启无证据**。
  daemon 的价值在于长期在岗（08:30 盘前 + 19:10 收盘选股），故须由 SCM 托管。

.NSSM 的信任状态（务必知情）
  实测 `nssm.exe` 的 Authenticode = **NotSigned**（NSSM 2.24 / 2014 年, 未签名）。
  本脚本把分发 ZIP 与 EXE 的 SHA256 钉死；哈希不符即拒绝。
  这只能防"同一来源被替换"，**不能**替代代码签名 —— 即"信任 nssm.cc 这个来源"，
  不是"验证了发布者"。

.可观测性（本版重点）
  提权会话里管道重定向不可用，且 Start-Transcript 版本曾**整体卡住、无从定位**。
  故本版**每一步先写日志再执行**，日志为纯文本逐行追加（logs\install_steps.log）——
  若某步卡住，日志最后一行即指向它。

.用法（管理员）
  pwsh -File ops\install_daemon_service.ps1 -IAcceptUnsignedNssm -Start
  pwsh -File ops\install_daemon_service.ps1 -Uninstall
#>
[CmdletBinding()]
param(
  [string]$ServiceName = 'AStockDaemon',
  [string]$DisplayName = 'A-STOCK 量化守护 (daemon.py)',
  [string]$StockdbRoot = 'E:\A_stockDB',
  [string]$Interpreter = '310',
  [switch]$Uninstall,
  [switch]$Start,
  [switch]$IAcceptUnsignedNssm
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$StepLog = Join-Path $LogDir 'install_steps.log'

function Step([string]$m) {
  $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
  Add-Content -Path $StepLog -Value $line -Encoding UTF8
  Write-Host $line
}
function Ok([string]$m)   { Step "    [OK] $m" }
function Bad([string]$m)  { Step "    [FAIL] $m" }
function Warn2([string]$m){ Step "    [WARN] $m" }
function Run([string]$exe, [string[]]$argv) {
  Step ("    执行: {0} {1}" -f (Split-Path $exe -Leaf), ($argv -join ' '))
  $o = & $exe @argv 2>&1 | Out-String
  $code = $LASTEXITCODE
  if ($o.Trim()) { Step ("      输出: " + (($o.Trim() -replace "`r?`n", ' | ') -replace "`0", '')) }
  Step ("      退出码: {0}" -f $code)
  return $code
}

$NSSM_ZIP_SHA256 = '727D1E42275C605E0F04ABA98095C38A8E1E46DEF453CDFFCE42869428AA6743'
$NSSM_EXE_SHA256 = 'F689EE9AF94B00E9E3F0BB072B34CAAF207F32DCB4F5782FC9CA351DF9A06C97'
$NSSM_URL = 'https://nssm.cc/release/nssm-2.24.zip'
$ToolsDir = Join-Path (Split-Path -Parent $RepoRoot) '_tools'
$NssmExe = Join-Path $ToolsDir 'nssm-2.24\win64\nssm.exe'

Step "================ 开始 (Service=$ServiceName Uninstall=$Uninstall Start=$Start) ================"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
             [Security.Principal.WindowsBuiltInRole]::Administrator)
Step "用户=$($id.Name) 管理员=$isAdmin"
if (-not $isAdmin) { Bad '需要管理员权限'; exit 5 }

Step '清理上一次残留的提权安装进程（非提权会话杀不掉它们）'
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'powershell|pwsh' } | ForEach-Object {
  $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
  if ($cl -and ($cl -match 'install_daemon_service') -and ($_.Id -ne $PID)) {
    Step "    结束残留 pid=$($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
  }
}
Get-Process -Name nssm -ErrorAction SilentlyContinue | ForEach-Object {
  Step "    结束残留 nssm pid=$($_.Id)"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

if (-not $Uninstall) {
  Step '准备 NSSM（校验哈希）'
  if (-not (Test-Path $NssmExe)) {
    $zip = Join-Path $ToolsDir 'nssm-2.24.zip'
    if (-not (Test-Path $zip)) {
      New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
      [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
      Step "    下载 $NSSM_URL"
      Invoke-WebRequest -Uri $NSSM_URL -OutFile $zip -UseBasicParsing -TimeoutSec 120
    }
    $zh = (Get-FileHash $zip -Algorithm SHA256).Hash
    Step "    zip SHA256=$zh"
    if ($zh -ne $NSSM_ZIP_SHA256) { Bad 'zip 哈希不符'; exit 6 }
    Expand-Archive -Path $zip -DestinationPath $ToolsDir -Force
  }
  $eh = (Get-FileHash $NssmExe -Algorithm SHA256).Hash
  Step "    exe SHA256=$eh"
  if ($eh -ne $NSSM_EXE_SHA256) { Bad 'nssm.exe 哈希不符'; exit 6 }
  Ok 'nssm.exe 哈希相符'
  $sig = Get-AuthenticodeSignature $NssmExe
  Step "    Authenticode=$($sig.Status)"
  if ($sig.Status -ne 'Valid') {
    Warn2 'nssm.exe 未签名；哈希只证明来源未被替换，不能替代代码签名'
    if (-not $IAcceptUnsignedNssm) { Bad '需显式 -IAcceptUnsignedNssm'; exit 7 }
    Ok '已显式接受未签名二进制'
  }
}

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

Step '前置检查'
$py = Join-Path $RepoRoot ".venv$Interpreter\Scripts\python.exe"
if (-not (Test-Path $py)) { Bad "解释器不存在: $py"; exit 9 }
Ok "解释器: $py"
if (-not (Test-Path (Join-Path $RepoRoot 'src\daemon.py'))) { Bad 'src\daemon.py 不存在'; exit 9 }

Step '引擎闸门（与 ops/start_daemon.ps1 同一判据，不绕过）'
$probeOut = (& $py (Join-Path $RepoRoot 'src\engine_bars_sync.py') --probe 2>&1 | Out-String)
$brace = $probeOut.IndexOf('{')
$probe = $null
if ($brace -ge 0) { try { $probe = $probeOut.Substring($brace) | ConvertFrom-Json } catch { } }
if ($null -eq $probe -or -not $probe.ok) {
  Bad "厂商引擎不可用（$(if($probe){$probe.error}else{'探针无输出'})）—— 拒绝装服务"
  exit 4
}
Ok "引擎正常: 数据到 $($probe.day)"
if ($probe.freshness -and -not $probe.freshness.ok) {
  Bad "引擎数据落后: $($probe.freshness.error) —— 拒绝装服务"
  exit 4
}

Step '停止当前手工启动的 daemon（避免两个实例）'
$stopFile = Join-Path $LogDir 'daemon.stop'
Set-Content -Path $stopFile -Value '' -Encoding ASCII
$waited = 0
$alive = @()
while ($waited -lt 30) {
  $alive = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'python' } | Where-Object {
    $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
    $cl -and ($cl -match 'daemon\.py')
  })
  if ($alive.Count -eq 0) { break }
  Start-Sleep -Seconds 2; $waited += 2
}
if ($alive.Count -gt 0) {
  Warn2 "优雅停止超时，强制结束 $($alive.Count) 个"
  $alive | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}
Remove-Item $stopFile -Force -ErrorAction SilentlyContinue
Ok '手工实例已停止'

Step "安装服务 $ServiceName"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Step '  服务已存在 -> 先移除（幂等）'
  Run $NssmExe @('stop', $ServiceName) | Out-Null
  Start-Sleep -Seconds 2
  Run $NssmExe @('remove', $ServiceName, 'confirm') | Out-Null
  Start-Sleep -Seconds 2
}

Run $NssmExe @('install', $ServiceName, $py, (Join-Path $RepoRoot 'src\daemon.py')) | Out-Null
$pairs = @(
  @('AppDirectory', $RepoRoot),
  @('DisplayName', $DisplayName),
  @('Description', 'A股轮动量化守护: 盘前健康检查/盘中引擎/收盘选股; 由 NSSM 托管'),
  @('Start', 'SERVICE_AUTO_START'),
  @('AppEnvironmentExtra', "STOCKDB_ROOT=$StockdbRoot", 'PYTHONIOENCODING=utf-8', 'PYTHONUTF8=1'),
  @('AppStdout', (Join-Path $LogDir 'daemon_service.out.log')),
  @('AppStderr', (Join-Path $LogDir 'daemon_service.err.log')),
  @('AppRotateFiles', '1'),
  @('AppRotateBytes', '10485760'),
  @('AppExit', 'Default', 'Restart'),
  @('AppRestartDelay', '5000'),
  @('AppStopMethodConsole', '15000'),
  @('AppStopMethodWindow', '5000')
)
foreach ($p in $pairs) { Run $NssmExe (@('set', $ServiceName) + $p) | Out-Null }
Ok '服务参数已写入'

Step '回读关键配置'
foreach ($k in @('Application', 'AppDirectory', 'Start', 'AppEnvironmentExtra', 'AppStdout', 'AppExit')) {
  $v = (& $NssmExe get $ServiceName $k 2>&1 | Out-String)
  Step ("    {0,-20} = {1}" -f $k, (($v.Trim() -replace "`r?`n", ' ') -replace "`0", ''))
}

if ($Start) {
  # [2026-09-21 订正] 原用 `nssm start` —— 实测它**会卡住**: 其语义是"启动并等待服务进入
  # RUNNING", 当应用未起来时它会一直等 (实测卡死 3 分钟以上, 且**不留任何输出**,
  # 只能靠比对注册表/日志反推"其实装好了只是没启动")。
  # 改为 `sc.exe start`(立即返回) + **轮询**, 把"等待"变成可观测、可超时的检查。
  Step '启动服务（sc.exe start + 轮询）'
  Run 'sc.exe' @('start', $ServiceName) | Out-Null
  $t = 0
  $alive = @()
  while ($t -lt 90) {
    Start-Sleep -Seconds 3; $t += 3
    $st = (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status
    $alive = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'python' } | Where-Object {
      $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
      $cl -and ($cl -match 'daemon\.py')
    })
    Step "  +{0,3}s 状态={1} daemon进程={2}" -f $t, $st, $(if ($alive.Count) { $alive.Id -join ',' } else { '无' })
    if ($st -eq 'Running' -and $alive.Count -gt 0) { break }
  }
  $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  Step "  最终: 状态=$($svc.Status) StartType=$($svc.StartType) daemon进程=$(if ($alive.Count) { $alive.Id -join ', ' } else { '无' })"
  if ($svc.Status -eq 'Running' -and $alive.Count -gt 0) { Ok '服务已运行' } else { Bad '未达预期(见上)' }
  # NSSM 只有在应用**产生过输出**时才会建这两个文件; 它们存在即证明应用真的被拉起过。
  foreach ($f in 'daemon_service.out.log', 'daemon_service.err.log') {
    $fp = Join-Path $LogDir $f
    if (Test-Path $fp) { Ok "$f 已生成 ({0:N0} B)" -f (Get-Item $fp).Length }
    else { Warn2 "$f 不存在 —— 应用可能从未被拉起" }
  }
  Get-Content (Join-Path $LogDir 'daemon.log') -Tail 3 -Encoding UTF8 -ErrorAction SilentlyContinue |
    ForEach-Object { Step "    log> $_" }
} else {
  Step "未加 -Start：已注册未启动。启动: pwsh -File ops\service_control.ps1 -Action start"
}
Step '================ 结束 ================'
exit 0
