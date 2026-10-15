# Start one bot instance on DEMO in a separate minimized window.
# Closing that window stops the bot; open positions keep their server-side SL/TP.
#   .\start_bot.ps1                     # main (xau_momo, M15)
#   .\start_bot.ps1 -Instance lab_m5    # scalp lab M5
#   .\start_bot.ps1 -Instance all       # every instance in bot/config.py LIVE_INSTANCES
param(
    [string]$Instance = "main"
)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$names = @($Instance)
if ($Instance -eq "all") { $names = @("main", "lab_m5", "lab_m1") }
foreach ($n in $names) {
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "bot live --instance $n(\s|$)" }
    if ($running) { Write-Host "[$n] already running (pid $($running[0].ProcessId)) - skipped"; continue }
    $p = Start-Process -FilePath $py -ArgumentList @("-m", "bot", "live", "--instance", $n) -WorkingDirectory $root -WindowStyle Minimized -PassThru
    Write-Host "[$n] started (pid $($p.Id))"
}
Write-Host "Status: .venv\Scripts\python -m bot status   |   Monitor: .\monitor.ps1"
