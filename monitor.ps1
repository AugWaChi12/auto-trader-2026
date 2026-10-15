# Open the read-only monitor (http://127.0.0.1:8765, charts at /charts) in a minimized window.
#   .\monitor.ps1           # overview page
#   .\monitor.ps1 -Charts   # multi-chart page
param([switch]$Charts)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "bot monitor" }
if ($running) {
    $url = "http://127.0.0.1:8765"; if ($Charts) { $url += "/charts" }
    Start-Process $url
    Write-Host "Monitor already running - opened $url"
    exit
}
$botArgs = @("-m", "bot", "monitor")
if ($Charts) { $botArgs += "--charts" }
Start-Process -FilePath $py -ArgumentList $botArgs -WorkingDirectory $root -WindowStyle Minimized
Write-Host "Monitor: http://127.0.0.1:8765  (charts: /charts)"
