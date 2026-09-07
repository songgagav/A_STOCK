param(
    [string]$Day = "2026-08-24",
    [string]$Table = "daily_bars"
)

$segments = @(
    @{ start = "000000"; end = "300000" },
    @{ start = "300000"; end = "600000" },
    @{ start = "600000"; end = "700000" },
    @{ start = "700000"; end = "839000" },
    @{ start = "839000"; end = "870000" },
    @{ start = "870000"; end = "999999" }
)

Write-Output "=== update_db segment run day=$Day table=$Table ==="
for ($i = 0; $i -lt $segments.Count; $i++) {
    $s = $segments[$i].start
    $e = $segments[$i].end
    Write-Output ""
    Write-Output ("--- segment " + ($i+1) + "/" + $segments.Count + ": " + $s + " .. " + $e + " ---")
    python -u update_db.py --day $Day --days 1 --only $Table --start-symbol $s --end-symbol $e
    if ($LASTEXITCODE -ne 0) {
        Write-Output ("segment " + $s + ".." + $e + " failed, continue next")
    }
}
Write-Output ""
Write-Output "=== all segments done ==="