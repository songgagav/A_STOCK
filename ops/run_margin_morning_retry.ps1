$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv310\Scripts\python.exe'
$Program = Join-Path $RepoRoot 'src\margin_sync.py'
$Log = Join-Path $RepoRoot 'logs\margin_morning_retry.log'
$ReceiptRoot = Join-Path $RepoRoot 'data\margin_retry_morning\receipts'
$Day = Get-Date -Format 'yyyy-MM-dd'

if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $Program)) {
  throw 'Margin retry interpreter or script is missing.'
}
New-Item -ItemType Directory -Path $ReceiptRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $Log) -Force | Out-Null

# Do not run two H5i writers concurrently; retry on the next 30-minute trigger.
$Writers = @(Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match '(?i)(scripts[\\/]h5i_financials_sync\.py|src[\\/]margin_sync\.py\s+sync)'
})
if ($Writers.Count -gt 0) {
  $Pids = ($Writers | ForEach-Object { $_.ProcessId }) -join ','
  Add-Content -LiteralPath $Log -Encoding UTF8 -Value "[$(Get-Date -Format o)] skipped_busy pids=$Pids trade_day=$Day"
  exit 0
}

$Output = @(& $Python $Program sync --days 2 2>&1)
$Code = $LASTEXITCODE
$Text = $Output -join "`n"
$Result = $null
foreach ($Match in [regex]::Matches($Text, '(?m)^\s*\{')) {
  try {
    $Candidate = $Text.Substring($Match.Index) | ConvertFrom-Json
    if ($Candidate.ok -eq $true -and $Candidate.table -eq 'margin_daily') {
      $Result = $Candidate
      break
    }
  } catch {
    # Native tools can print provider diagnostics before their JSON result.
  }
}
$Missing = $Result.missing_legs
$HasMissingLegs = $false
if ($Missing -is [System.Collections.IDictionary]) {
  $HasMissingLegs = $Missing.Count -gt 0
} elseif ($Missing) {
  $HasMissingLegs = $Missing.PSObject.Properties.Count -gt 0
}
$Status = if ($Code -ne 0 -or -not $Result) { 'blocked' } elseif ($HasMissingLegs) { 'pending' } else { 'available' }
$Now = Get-Date
$RunId = 'margin-' + $Now.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$Receipt = [ordered]@{
  schema_version = 1
  run_id = $RunId
  observed_at = $Now.ToString('o')
  trade_day = $Day
  status = $Status
  source = 'src/margin_sync.py'
  requested_days = 2
  ref_day = $Result.ref_day
  h5i_max_day = if ($Result.h5i_max_day_after) { $Result.h5i_max_day_after } else { $Result.h5i_max_day_before }
  missing_legs = $Missing
  process_exit_code = $Code
  output = $Text
}
$ReceiptPath = Join-Path $ReceiptRoot ($RunId + '.json')
$TempPath = $ReceiptPath + '.tmp'
$Json = $Receipt | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($TempPath, $Json, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $TempPath -Destination $ReceiptPath
Add-Content -LiteralPath $Log -Encoding UTF8 -Value "[$($Now.ToString('o'))] status=$Status run_id=$RunId`n$Text"

$LatestTmp = Join-Path $ReceiptRoot 'last_run.json.tmp'
$Latest = Join-Path $ReceiptRoot 'last_run.json'
[System.IO.File]::WriteAllText($LatestTmp, $Json, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $LatestTmp -Destination $Latest -Force
if ($Code -ne 0) { exit $Code }
exit 0
