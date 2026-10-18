"""
mt5_rpyc_server.py — รันใน Python ฝั่ง Wine ของ container MT5 (deploy/mt5-rpyc-service.sh เรียก)
เปิด RPyC classic server ที่ :8001 ให้บอทต่อเข้ามา (bot/mt5api.py)

Wine 11 (staging): ตัวสุ่มเลขของ Windows ใช้ไม่ได้ใน Python 32 บิต
  → os.urandom โยน OSError 0x8009001d → numpy (และ MetaTrader5 ที่ import numpy) import ไม่ได้
  → ใส่ตัวสุ่มสำรอง (xorshift จากเวลา) เฉพาะตอนที่ของ Windows พัง — numpy ใช้แค่ตั้ง seed เริ่มต้น ไม่เกี่ยวกับความปลอดภัย
  ต้องแทนก่อน import อะไรที่ผูก os.urandom ไว้ (random, numpy) และต้องรันด้วย PYTHONHASHSEED=0
"""
import os
import time

_real_urandom = os.urandom
_state = [(time.time_ns() ^ (os.getpid() << 20)) | 1]


def _urandom(n: int) -> bytes:
    try:
        return _real_urandom(n)
    except OSError:
        x, out = _state[0], bytearray()
        for _ in range(n):
            x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
            x ^= x >> 7
            x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
            out.append(x & 0xFF)
        _state[0] = x
        return bytes(out)


os.urandom = _urandom

import MetaTrader5  # noqa: E402,F401  — พังตรงนี้ = ให้ s6 เปิดใหม่ และเห็น error ใน log ทันที
from rpyc import SlaveService  # noqa: E402
from rpyc.utils.server import ThreadedServer  # noqa: E402

print("rpyc classic server :8001 (MetaTrader5 %s)" % MetaTrader5.__version__, flush=True)
ThreadedServer(SlaveService, hostname="0.0.0.0", port=8001, reuse_addr=True).start()
