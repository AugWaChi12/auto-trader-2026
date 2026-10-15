# Open the read-only monitor (http://127.0.0.1:8765) - runs hidden in the background.
#   .\monitor.ps1           # overview page
#   .\monitor.ps1 -Charts   # multi-chart page
param([switch]$Charts)
$root = $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$url = "http://127.0.0.1:8765"; if ($Charts) { $url += "/charts" }
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match "bot monitor" }
if (-not $running) {
    Start-Process -FilePath $pyw -ArgumentList @("-m", "bot", "monitor", "--no-browser") -WorkingDirectory $root
    Start-Sleep -Seconds 3
}
Start-Process $url
Write-Host "Monitor: $url  (pages: /charts /radar /techniques)"
