# Start the bot on DEMO in a separate minimized window.
# Closing that window stops the bot; open positions keep their server-side SL/TP.
#   .\start_bot.ps1
#   .\start_bot.ps1 -Strategies xau_momo -Profile demo_1k
param(
    [string]$Strategies = "",
    [string]$Profile = ""
)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$botArgs = @("-m", "bot", "live")
if ($Strategies) { $botArgs += @("--strategies", $Strategies) }
if ($Profile) { $botArgs += @("--profile", $Profile) }
$p = Start-Process -FilePath $py -ArgumentList $botArgs -WorkingDirectory $root -WindowStyle Minimized -PassThru
Write-Host "Bot started (pid $($p.Id)). Check with: .venv\Scripts\python -m bot status"
