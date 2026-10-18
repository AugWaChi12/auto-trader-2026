#!/usr/bin/with-contenv bash
# s6 service ใน container MT5 (เมานท์เป็น /custom-services.d/mt5-rpyc):
# เปิด RPyC classic server ด้วย Python ฝั่ง Wine ที่ port 8001 ให้บอทต่อเข้ามา (bot/mt5api.py)
#
# ทำไมไม่ใช้ของ image (เจอจริง 18 ต.ค. 2026):
#   1) start.sh เรียก "python3 -m mt5linux" แต่ mt5linux 1.1.1 ใช้ f-string แบบ Python 3.12 บน Python 3.11 → SyntaxError
#   2) image ลง numpy 2.x ใน Wine แต่ MetaTrader5 5.0.36 สร้างกับ numpy 1.x (แก้ครั้งเดียวด้วย numpy 1.26 ใน /config แล้ว)
#   3) rpyc 6: "python -m rpyc.cli.rpyc_classic" ไม่เปิด server
#   4) หลัง recreate container บางครั้ง terminal ไม่ถูกเปิด → หน้าจอดำ
#   5) Wine 11 (ใช้แก้ "debugger found" ของ MT5): ตัวสุ่มเลขใน Python 32 บิตพัง → mt5_rpyc_server.py ใส่ตัวสำรอง + PYTHONHASHSEED=0
# log: /config/mt5-rpyc.log
PYDIR="/config/.wine/drive_c/Program Files (x86)/Python39-32"
LOG=/config/mt5-rpyc.log
RUN="s6-setuidgid abc env HOME=/config WINEPREFIX=/config/.wine WINEDEBUG=-all DISPLAY=:1 PYTHONHASHSEED=0"

# รอ start.sh ติดตั้ง Python + MetaTrader5 + rpyc ใน Wine ให้เสร็จก่อน (ครั้งแรก ~5 นาที)
until [ -d "$PYDIR/Lib/site-packages/rpyc" ] && [ -d "$PYDIR/Lib/site-packages/MetaTrader5" ]; do sleep 10; done
sleep 20
cd /config || exit 1

# ปัญหาที่ 6: เปลี่ยนรุ่น Wine (stable 10 → staging 11) แต่ prefix ใน /config ยังเป็นไฟล์รุ่นเก่า
#   → ฝั่ง 32 บิตพัง (SystemFunction036 unimplemented, urandom/crypto ใช้ไม่ได้, MT5 IPC "Hash calculation failed")
#   แก้: wineboot -u ครั้งเดียวต่อรุ่น Wine (จำรุ่นไว้ใน /config/.wine-booted)
WV=$(wine --version 2>/dev/null)
if [ "$(cat /config/.wine-booted 2>/dev/null)" != "$WV" ]; then
  echo "[mt5-rpyc] $(date -Is) updating Wine prefix for $WV" >>"$LOG"
  $RUN wine wineboot -u </dev/null >>"$LOG" 2>&1 && echo "$WV" > /config/.wine-booted
fi

if ! pgrep -f "terminal64.exe" >/dev/null; then
  echo "[mt5-rpyc] $(date -Is) terminal64.exe not running -> starting it" >>"$LOG"
  $RUN wine "/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe" </dev/null >/dev/null 2>&1 &
  sleep 20
fi

cp /custom-files/mt5_rpyc_server.py /config/mt5_rpyc_server.py
chown abc:abc /config/mt5_rpyc_server.py
echo "[mt5-rpyc] $(date -Is) starting RPyC classic server on :8001" >>"$LOG"
exec $RUN wine "$PYDIR/python.exe" -u 'Z:\config\mt5_rpyc_server.py' </dev/null >>"$LOG" 2>&1
