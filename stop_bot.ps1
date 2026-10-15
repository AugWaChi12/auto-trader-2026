# Stop bot instance(s). Open positions stay protected by their server-side SL/TP.
#   .\stop_bot.ps1 -Instance lab_m1
#   .\stop_bot.ps1 -Instance all          # all bots (monitor keeps running)
#   .\stop_bot.ps1 -Instance all -Monitor # bots + monitor
param(
    [string]$Instance = "all",
    [switch]$Monitor
)
$pattern = if ($Instance -eq "all") { "bot live --instance " } else { "bot live --instance $Instance(\s|$)" }
$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $pattern }
if ($Monitor) { $procs += Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match "bot monitor" } }
foreach ($p in $procs) {
    $name = ($p.CommandLine -replace '.*-m bot ', '').Trim()
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "stopped: $name (pid $($p.ProcessId))"
}
if (-not $procs) { Write-Host "nothing to stop" }
