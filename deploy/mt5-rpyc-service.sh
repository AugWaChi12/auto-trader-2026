#!/usr/bin/with-contenv bash
# s6 service ใน container MT5 (เมานท์เป็น /custom-services.d/mt5-rpyc):
# เปิด RPyC classic server ด้วย Python ฝั่ง Wine ที่ port 8001 ให้บอทต่อเข้ามา (bot/mt5api.py)
#
# ทำไมไม่ใช้ของ image (เจอจริง 18 ต.ค. 2026):
#   1) start.sh เรียก "python3 -m mt5linux" แต่ mt5linux 1.1.1 ใช้ f-string แบบ Python 3.12 บน Python 3.11 → SyntaxError
#   2) image ลง numpy 2.x ใน Wine แต่ MetaTrader5 5.0.36 สร้างกับ numpy 1.x → import MetaTrader5 พัง (_ARRAY_API not found)
#   3) rpyc 6: python -m rpyc.cli.rpyc_classic ไม่เปิด server
# log: /config/mt5-rpyc.log
PYDIR="/config/.wine/drive_c/Program Files (x86)/Python39-32"
LOG=/config/mt5-rpyc.log
RUN="s6-setuidgid abc env HOME=/config WINEPREFIX=/config/.wine WINEDEBUG=-all DISPLAY=:1"

# รอ start.sh ติดตั้ง Python + MetaTrader5 + rpyc ใน Wine ให้เสร็จก่อน (ครั้งแรก ~5 นาที)
until [ -d "$PYDIR/Lib/site-packages/rpyc" ] && [ -d "$PYDIR/Lib/site-packages/MetaTrader5" ]; do sleep 10; done
sleep 20
cd /config || exit 1

if ! $RUN wine "$PYDIR/python.exe" -c "import MetaTrader5" </dev/null >>"$LOG" 2>&1; then
  echo "[mt5-rpyc] $(date -Is) MetaTrader5 import failed -> installing numpy<2" >>"$LOG"
  $RUN wine "$PYDIR/python.exe" -m pip install --no-cache-dir --quiet "numpy<2" </dev/null >>"$LOG" 2>&1
fi

echo "[mt5-rpyc] $(date -Is) starting RPyC classic server on :8001" >>"$LOG"
# หมายเหตุ: "python -m rpyc.cli.rpyc_classic" ของ rpyc 6 แค่ import แล้วจบ (exit 0) ไม่เปิด server → เปิด ThreadedServer เอง
exec $RUN wine "$PYDIR/python.exe" -u -c "from rpyc import SlaveService; from rpyc.utils.server import ThreadedServer; print('rpyc classic server :8001', flush=True); ThreadedServer(SlaveService, hostname='0.0.0.0', port=8001, reuse_addr=True).start()" </dev/null >>"$LOG" 2>&1
