"""
awake.py — กันเครื่อง Windows เข้า sleep ขณะบอท/monitor ทำงาน (ไม่แก้การตั้งค่าระบบ)

เหตุที่ต้องมี: 2026-10-16 และ 17 เครื่อง sleep ตอนดึก → บอทดับ ไม้ main ไม่ได้ปิดตอนตี 3 สองคืนติด
ใช้ SetThreadExecutionState แบบเดียวกับโปรแกรมดูวิดีโอ: มีผลเฉพาะตอนโปรเซสนี้ยังรันอยู่ ปิดบอทแล้วกลับเป็นปกติเอง
(ปิดฝาโน้ตบุ๊ก / กดปุ่ม sleep เองยังหลับได้ตามปกติ)
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def keep_awake(display: bool = False) -> bool:
    """เรียกจากเธรดหลักที่รันตลอด · display=True = กันจอดับด้วย (หน้า monitor ที่เปิดโชว์)"""
    if sys.platform != "win32":
        return False
    import ctypes
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)
    ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    log.info("keep awake (display=%s): %s", display, "on" if ok else "failed")
    return ok
