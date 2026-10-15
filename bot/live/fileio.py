"""
fileio.py — เขียนไฟล์ state แบบ atomic และทนไฟล์ถูกล็อกบน Windows

ปัญหาที่เจอจริง (logs/lab_m1, 2026-10-15 11:50): os.replace โดน PermissionError (WinError 5)
เพราะโปรแกรมอื่น (antivirus / ตัวทำดัชนี / monitor) เปิดไฟล์ปลายทางอยู่ชั่วขณะ
ถ้าปล่อยให้ error หลุดออกไประหว่าง send() → OrderGuard ไม่ได้จดว่าสัญญาณถูกใช้แล้ว

กติกา: การเขียน state ล้มเหลว **ต้องไม่ทำให้การเทรดสะดุด** — ลองซ้ำ แล้วยอมแพ้ด้วย warning
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write(path: Path, text: str, attempts: int = 5, delay: float = 0.05) -> bool:
    """เขียนไฟล์ชั่วคราวแล้วสลับแทน (ผู้อ่านไม่เห็นไฟล์ครึ่งๆ กลางๆ) — คืน False ถ้าล้มเหลวทุกครั้ง"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    for i in range(attempts):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as e:
            if i == attempts - 1:
                log.warning("เขียน %s ไม่สำเร็จหลังลอง %d ครั้ง: %s (ข้ามไป ไม่กระทบการเทรด)", path.name, attempts, e)
            else:
                time.sleep(delay * (2 ** i))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return False
