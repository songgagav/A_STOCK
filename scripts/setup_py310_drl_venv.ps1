<#
.SYNOPSIS
  创建"同时具备 h5i_db 与 torch"的干净 Python 3.10 环境（解决 P0-DRLDEP，(c) 路线）。

.BACKGROUND 为什么必须单独建这个环境
  DRL 训练需要 `h5i_db` **与** `torch` 在**同一个解释器**里：
    · `.venv314`（Python 3.14）有 torch，但 `h5i_db` 的原生扩展**仅支持 CPython 3.10**
      （README:171），故装不进去；
    · 机器上原有的 3.10 是 TRAE Agent 自带的**共享工具解释器**，而且
      **没有 `venv` 模块、也没有 `ensurepip`**，无法用它建隔离环境、也不能往里装 torch
      （会污染共享工具链）。
  ⇒ 结论：需要**下载一个独立的 Python 3.10**，在它之上建 venv 并两面都装齐。
  这正是登记册 P0-DRLDEP 的 (c) 路线。

.KEY FACTS（实测）
  · Windows 上 Python 3.10 的**最后一个二进制安装包是 3.10.11**；之后的 3.10.x 只有源码发布。
  · 安装包 SHA256 已固定在 $InstallerSha256，下载后**必须校验**（并校验 Authenticode 签名）。
  · 实测结果：该 venv 下 `drl_degrade.probe_runtime()` 返回 ok=True（四项全 True）；
    全量测试 682 passed / 1 skipped（对比 .venv314 的 664/19 —— 因为本环境有 h5i_db，
    原先 18 个"h5i-db 不可用, 跳过"的 test_integration_f68 用例真的跑起来了）。

.USAGE
  pwsh -File scripts/setup_py310_drl_venv.ps1                 # 全流程
  pwsh -File scripts/setup_py310_drl_venv.ps1 -SkipDownload    # 已装好基础解释器时
#>
[CmdletBinding()]
param(
  [string]$BasePythonDir = "$env:LOCALAPPDATA\Programs\Python\Python310",
  [string]$VenvDir       = "",
  [switch]$SkipDownload,
  [switch]$SkipTorch
)

$ErrorActionPreference = 'Stop'
$PyVersion       = '3.10.11'
$InstallerName   = "python-$PyVersion-amd64.exe"
$InstallerUrl    = "https://www.python.org/ftp/python/$PyVersion/$InstallerName"
# 固定校验和: 2026-09-20 实测所得。上游若重新打包会不一致 —— 那正是我们希望**响亮失败**的时刻。
$InstallerSha256 = 'D8DEDE5005564B408BA50317108B765ED9C3C510342A598F9FD42681CBE0648B'

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $VenvDir) { $VenvDir = Join-Path $RepoRoot '.venv310' }

function Say($m) { Write-Host "[setup310] $m" }

# ---------------------------------------------------------------- 1. 基础解释器
$baseExe = Join-Path $BasePythonDir 'python.exe'
if (-not (Test-Path $baseExe)) {
  if ($SkipDownload) { throw "基础解释器不存在且指定了 -SkipDownload: $baseExe" }
  $tmp = Join-Path $env:TEMP $InstallerName
  if (-not (Test-Path $tmp)) {
    Say "下载 $InstallerUrl"
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $tmp -UseBasicParsing -TimeoutSec 600
  }
  $got = (Get-FileHash $tmp -Algorithm SHA256).Hash
  if ($got -ne $InstallerSha256) {
    throw "安装包 SHA256 不符 —— 拒绝安装。期望 $InstallerSha256，实际 $got"
  }
  Say "SHA256 校验通过"
  $sig = Get-AuthenticodeSignature $tmp
  if ($sig.Status -ne 'Valid') { throw "Authenticode 签名无效: $($sig.Status)" }
  Say "Authenticode 签名有效: $($sig.SignerCertificate.Subject)"
  Say "静默安装到 $BasePythonDir（不改 PATH、不注册 launcher）"
  $a = @('/quiet','InstallAllUsers=0',"TargetDir=$BasePythonDir",'Include_launcher=0',
          'Include_test=0','AssociateFiles=0','Shortcuts=0','PrependPath=0',
          'Include_doc=0','Include_pip=1','Include_tcltk=1','SimpleInstall=1')
  $p = Start-Process -FilePath $tmp -ArgumentList $a -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "安装器退出码 $($p.ExitCode)" }
}
$ver = (& $baseExe -c "import sys;print('.'.join(map(str,sys.version_info[:3])))")
Say "基础解释器: $baseExe (Python $ver)"
if ($ver -ne $PyVersion) { Say "警告: 期望 $PyVersion，实际 $ver —— 仍继续，但请核对" }

# venv 支持是硬前提（原共享工具解释器就是缺这个）
$hasVenv = (& $baseExe -c "import importlib.util as u;print(bool(u.find_spec('venv')))")
if ($hasVenv.Trim() -ne 'True') { throw "该解释器没有 venv 模块，无法创建隔离环境" }

# ---------------------------------------------------------------- 2. venv
if (Test-Path $VenvDir) { Say "venv 已存在，先删除: $VenvDir"; Remove-Item $VenvDir -Recurse -Force }
Say "创建 venv: $VenvDir"
& $baseExe -m venv $VenvDir
$py = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv 创建失败" }
& $py -m pip install --upgrade pip --quiet

# ---------------------------------------------------------------- 3. 依赖
Say "安装核心依赖 requirements_314.txt"
& $py -m pip install -r (Join-Path $RepoRoot 'requirements_314.txt') --quiet
Say "安装 duckdb / polars / pytest（db.py 与 vnpy_backtest.py 的顶层依赖）"
& $py -m pip install duckdb polars pytest --quiet
Say "安装 h5i-db（3.10-only 原生扩展）"
& $py -m pip install 'h5i-db>=0.1.6' --quiet
if (-not $SkipTorch) {
  # 必须走 pytorch 的 CPU 索引: 默认 PyPI 的 torch 会拉 CUDA 版(约 2.5GB)
  Say "安装 torch (CPU wheel, 约 200MB)"
  & $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet
  Say "安装 gymnasium + stable-baselines3"
  & $py -m pip install gymnasium stable-baselines3 --quiet
}

# ---------------------------------------------------------------- 4. 验收
Say "验收: 四项依赖必须在**同一解释器**里齐备"
& $py -c @"
import importlib.util as u, sys
need = ('h5i_db','torch','gymnasium','stable_baselines3')
got = {m: bool(u.find_spec(m)) for m in need}
print('  python:', '.'.join(map(str, sys.version_info[:3])))
for k, v in got.items(): print(f'  {k} = {v}')
missing = [k for k, v in got.items() if not v]
sys.exit(1 if missing else 0)
"@
if ($LASTEXITCODE -ne 0) { throw "仍有依赖缺失 —— 见上面的矩阵" }
Say "完成。请把 run_daily/daemon 指向: $py"
Say "验证方式: & '$py' -c \"import h5i_db, torch; print('ok')\""
