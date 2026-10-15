# เปิดบอทบน demo ในหน้าต่างแยก (ปิดหน้าต่าง = หยุดบอท, ไม้ที่เปิดอยู่ยังมี SL/TP ฝั่ง server)
#   .\start_bot.ps1
#   .\start_bot.ps1 -Strategies xau_momo -Profile demo_1k
param(
    [string]$Strategies = "",
    [string]$Profile = ""
)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$args = @("-m", "bot", "live")
if ($Strategies) { $args += @("--strategies", $Strategies) }
if ($Profile) { $args += @("--profile", $Profile) }
Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $root -WindowStyle Minimized
Write-Host "บอทเริ่มทำงานในหน้าต่างแยกแล้ว — ดูสถานะด้วย: .venv\Scripts\python -m bot status"
