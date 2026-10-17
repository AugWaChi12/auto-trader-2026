# Restart only the test bots (m5_*) so they pick up new values from state\bot_settings.json. main is never touched.
# Called by the monitor's settings page; can also be run by hand.
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root
$names = (& $py -c "from bot import config as C; print(' '.join(n for n in C.LIVE_INSTANCES if n != 'main'))").Split(" ")
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot live --instance m5_' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
foreach ($n in $names) { & (Join-Path $root "start_bot.ps1") -Instance $n | Out-Null }
