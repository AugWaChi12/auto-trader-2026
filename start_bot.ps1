# Start bot instance(s) on DEMO as hidden background processes (no console windows).
# Logs: logs\<instance>_YYYYMMDD.log  |  Status: monitor page or `.venv\Scripts\python -m bot status`
#   .\start_bot.ps1                     # main (xau_momo, M15)
#   .\start_bot.ps1 -Instance lab_m5    # one instance
#   .\start_bot.ps1 -Instance all       # every instance in bot/config.py LIVE_INSTANCES
#   .\start_bot.ps1 -Instance all -Visible   # with console windows (debugging)
param(
    [string]$Instance = "main",
    [switch]$Visible
)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$exe = if ($Visible) { $py } else { $pyw }
$names = @($Instance)
if ($Instance -eq "all") { $names = (& $py -c "from bot import config as C; print(' '.join(C.LIVE_INSTANCES))").Split(" ") }
foreach ($n in $names) {
    $running = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match "bot live --instance $n(\s|$)" }
    if ($running) { Write-Host "[$n] already running (pid $($running[0].ProcessId)) - skipped"; continue }
    $p = Start-Process -FilePath $exe -ArgumentList @("-m", "bot", "live", "--instance", $n) -WorkingDirectory $root -WindowStyle Minimized -PassThru
    Write-Host "[$n] started (pid $($p.Id))"
}
Write-Host "Running hidden. Monitor: .\monitor.ps1  |  Stop: .\stop_bot.ps1 -Instance all"
