$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv310\Scripts\python.exe'
$Program = Join-Path $RepoRoot 'scripts\h5i_financials_sync.py'
$Log = Join-Path $RepoRoot 'logs\h5i_financials_sync.log'
$Today = Get-Date -Format 'yyyy-MM-dd'
$LastReceipt = Join-Path $RepoRoot 'data\financials_sync\last_run.json'

if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $Program)) {
  throw 'H5i financial sync interpreter or script is missing.'
}
New-Item -ItemType Directory -Path (Split-Path -Parent $Log) -Force | Out-Null

# This updater appends to the shared H5i database. Avoid racing a manually
# started full-universe sync or the margin writer, which has a separate task.
$Writers = @(Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match '(?i)(scripts[\\/]h5i_financials_sync\.py|src[\\/]margin_sync\.py\s+sync)'
})
if ($Writers.Count -gt 0) {
  $Pids = ($Writers | ForEach-Object { $_.ProcessId }) -join ','
  Add-Content -LiteralPath $Log -Encoding UTF8 -Value "[$(Get-Date -Format o)] skipped_busy pids=$Pids as_of=$Today"
  exit 0
}

if (Test-Path -LiteralPath $LastReceipt) {
  try {
    $Last = Get-Content -LiteralPath $LastReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Last.as_of -eq $Today -and $Last.status -in @('available', 'up_to_date')) {
      Add-Content -LiteralPath $Log -Encoding UTF8 -Value "[$(Get-Date -Format o)] already complete for $Today"
      exit 0
    }
  } catch {
    # A corrupt or partial pointer is not trusted; the sync writes a new immutable receipt.
  }
}

# Python providers/progress bars may write harmless diagnostics to stderr and
# still return exit code 0. PowerShell turns native stderr into error records;
# with Stop that aborts before the receipt log is written. Capture both streams
# while Continue is active, then use the process exit code as the result.
$PreviousErrorActionPreference = $ErrorActionPreference
try {
  $ErrorActionPreference = 'Continue'
  $Output = @(& $Python $Program --as-of $Today --workers 6 --minimum-coverage 1.0 2>&1)
  $Code = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $PreviousErrorActionPreference
}
Add-Content -LiteralPath $Log -Encoding UTF8 -Value "[$(Get-Date -Format o)] exit=$Code as_of=$Today"
if ($Output.Count) { Add-Content -LiteralPath $Log -Encoding UTF8 -Value ($Output -join "`n") }
exit $Code

