# Self-research for the 9 M5 test bots: refresh M5 data from MT5 -> rerun the walk-forward lab ->
# write new assignments (bot/m5_bots.json) -> restart only the m5_* bots. main is never touched.
#   .\research_bots.ps1              # full cycle (~10-20 min)
#   .\research_bots.ps1 -NoRestart   # research only, look at research\RESULTS_m5.md first
param([switch]$NoRestart)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root
& $py -m bot download --symbols XAUUSD NAS100 --tf M5 --years 1.5
if ($LASTEXITCODE -ne 0) { Write-Host "download failed - is MT5 open?"; exit 1 }
& $py research\m5_lab.py --assign
if ($LASTEXITCODE -ne 0) { Write-Host "research failed"; exit 1 }
if ($NoRestart) { exit 0 }
$names = (& $py -c "from bot import config as C; print(' '.join(n for n in C.LIVE_INSTANCES if n.startswith('m5_')))").Split(" ")
foreach ($n in $names) {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "bot live --instance $n(\s|$)" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
foreach ($n in $names) { & (Join-Path $root "start_bot.ps1") -Instance $n | Out-Null; Write-Host "[$n] restarted" }
