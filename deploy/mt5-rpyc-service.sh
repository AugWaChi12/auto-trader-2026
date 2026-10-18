#!/usr/bin/with-contenv bash
# s6 service ใน container MT5 (เมานท์เป็น /custom-services.d/mt5-rpyc):
# เปิด RPyC classic server ด้วย Python ฝั่ง Wine ที่ port 8001 ให้บอทต่อเข้ามา (bot/mt5api.py)
#
# ทำไมไม่ใช้ของ image: start.sh เรียก "python3 -m mt5linux" ซึ่ง mt5linux 1.1.1 เขียนด้วย f-string แบบ Python 3.12
# แต่ image มี Python 3.11 → SyntaxError แล้ว port 8001 ไม่เปิด (เจอจริง 18 ต.ค. 2026)
PYDIR="/config/.wine/drive_c/Program Files (x86)/Python39-32"
# รอ start.sh ติดตั้ง Python + MetaTrader5 + rpyc ใน Wine ให้เสร็จก่อน (ครั้งแรก ~5 นาที)
until [ -d "$PYDIR/Lib/site-packages/rpyc" ] && [ -d "$PYDIR/Lib/site-packages/MetaTrader5" ]; do sleep 10; done
sleep 20
cd /config || exit 1
exec s6-setuidgid abc env HOME=/config WINEPREFIX=/config/.wine WINEDEBUG=-all DISPLAY=:1 \
    wine "$PYDIR/python.exe" -m rpyc.cli.rpyc_classic --host 0.0.0.0 -p 8001
