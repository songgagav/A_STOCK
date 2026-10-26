<#
.SYNOPSIS
  用 NSSM 把每日行情数据自动补全守护注册为 Windows 服务。

.DESCRIPTION
  该服务只运行 src/data_update_daemon.py，负责 StockDB→H5i 主链和经过门禁的
  Baostock 回补；不运行选股、DRL、PaperBook 或 broker 逻辑。

  安装/卸载需要管理员 PowerShell。默认只安装，不自动启动；使用 -Start 才启动。
#>
[CmdletBinding()]
param(
  [string]$ServiceName = 'AStockDataUpdate',
  [string]$DisplayName = 'A-STOCK 日线数据自动补全',
  [string]$StockdbRoot = 'E:\A_stockDB',
  [string]$Interpreter = '310',
  [switch]$Uninstall,
  [switch]$Start,
  [switch]$IAcceptUnsignedNssm
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
$ToolsDir = Join-Path (Split-Path -Parent $RepoRoot) '_tools'
$NssmExe = Join-Path $ToolsDir 'nssm-2.24\win64\nssm.exe'
$NSSM_ZIP_SHA256 = '727D1E42275C605E0F04ABA98095C38A8E1E46DEF453CDFFCE42869428AA6743'
$NSSM_EXE_SHA256 = 'F689EE9AF94B00E9E3F0BB072B34CAAF207F32DCB4F5782FC9CA351DF9A06C97'
$NSSM_URL = 'https://nssm.cc/release/nssm-2.24.zip'
$StepLog = Join-Path $LogDir 'install_data_update_steps.log'

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Step([string]$Message) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Add-Content -Path $StepLog -Value $line -Encoding UTF8
  Write-Host $line
}

function Run([string]$Exe, [string[]]$ArgList) {
  Step ("执行: {0} {1}" -f (Split-Path $Exe -Leaf), ($ArgList -join ' '))
  $output = & $Exe @ArgList 2>&1 | Out-String
  $code = $LASTEXITCODE
  if ($output.Trim()) { Step ("输出: " + (($output.Trim() -replace "`r?`n", ' | ') -replace "`0", '')) }
  Step ("退出码: {0}" -f $code)
  return $code
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
Step "用户=$($identity.Name) 管理员=$isAdmin Service=$ServiceName Uninstall=$Uninstall Start=$Start"
if (-not $isAdmin) { throw '需要管理员权限' }

if (-not (Test-Path $NssmExe)) {
  $zip = Join-Path $ToolsDir 'nssm-2.24.zip'
  New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
  if (-not (Test-Path $zip)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Step "下载 $NSSM_URL"
    Invoke-WebRequest -Uri $NSSM_URL -OutFile $zip -UseBasicParsing -TimeoutSec 120
  }
  $zipHash = (Get-FileHash $zip -Algorithm SHA256).Hash
  if ($zipHash -ne $NSSM_ZIP_SHA256) { throw "NSSM zip SHA256 不符: $zipHash" }
  Expand-Archive -Path $zip -DestinationPath $ToolsDir -Force
}

if (-not (Test-Path $NssmExe)) { throw "NSSM 不存在: $NssmExe" }
$exeHash = (Get-FileHash $NssmExe -Algorithm SHA256).Hash
if ($exeHash -ne $NSSM_EXE_SHA256) { throw "NSSM exe SHA256 不符: $exeHash" }
$signature = Get-AuthenticodeSignature $NssmExe
Step "NSSM SHA256=$exeHash Authenticode=$($signature.Status)"
if ($signature.Status -ne 'Valid' -and -not $IAcceptUnsignedNssm) {
  throw 'NSSM 未签名；需要显式 -IAcceptUnsignedNssm'
}

if ($Uninstall) {
  if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Run $NssmExe @('stop', $ServiceName) | Out-Null
    Start-Sleep -Seconds 2
    Run $NssmExe @('remove', $ServiceName, 'confirm') | Out-Null
  }
  if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "服务仍存在: $ServiceName"
  }
  Step "已卸载 $ServiceName"
  exit 0
}

$py = Join-Path $RepoRoot ".venv$Interpreter\Scripts\python.exe"
$entry = Join-Path $RepoRoot 'src\data_update_daemon.py'
if (-not (Test-Path $py)) { throw "解释器不存在: $py" }
if (-not (Test-Path $entry)) { throw "入口不存在: $entry" }

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Run $NssmExe @('stop', $ServiceName) | Out-Null
  Start-Sleep -Seconds 2
  Run $NssmExe @('remove', $ServiceName, 'confirm') | Out-Null
  Start-Sleep -Seconds 2
}

Run $NssmExe @('install', $ServiceName, $py, $entry) | Out-Null
$pairs = @(
  @('AppDirectory', $RepoRoot),
  @('DisplayName', $DisplayName),
  @('Description', 'A股日线数据自动补全: StockDB主链 + 双缺口Baostock回补'),
  @('Start', 'SERVICE_AUTO_START'),
  @('AppEnvironmentExtra', "STOCKDB_ROOT=$StockdbRoot", 'PYTHONIOENCODING=utf-8', 'PYTHONUTF8=1'),
  @('AppStdout', (Join-Path $LogDir 'data_update_service.out.log')),
  @('AppStderr', (Join-Path $LogDir 'data_update_service.err.log')),
  @('AppRotateFiles', '1'),
  @('AppRotateBytes', '10485760'),
  @('AppExit', 'Default', 'Restart'),
  @('AppRestartDelay', '5000'),
  @('AppStopMethodConsole', '15000'),
  @('AppStopMethodWindow', '5000')
)
foreach ($pair in $pairs) {
  Run $NssmExe (@('set', $ServiceName) + $pair) | Out-Null
}

Step "已安装 $ServiceName; 入口=$entry; 目录=$RepoRoot"
if ($Start) {
  Run 'sc.exe' @('start', $ServiceName) | Out-Null
  Start-Sleep -Seconds 3
  $state = (Get-Service -Name $ServiceName).Status
  Step "服务状态=$state"
}
