# Open the read-only monitor page (http://127.0.0.1:8765) in a minimized window.
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
Start-Process -FilePath $py -ArgumentList @("-m", "bot", "monitor") -WorkingDirectory $root -WindowStyle Minimized
Write-Host "Monitor: http://127.0.0.1:8765"
