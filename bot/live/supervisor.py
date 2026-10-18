"""
supervisor.py — คุมบอททุกตัวในโปรเซสเดียว (ใช้บน Linux/Docker แทน start_bot.ps1 / restart_bots.ps1 · Windows ก็ใช้ได้)

    python -m bot supervise

  · เปิดบอทที่เปิดใช้อยู่ (main + m5_* ที่ไม่ได้ถูกปิดในหน้าเว็บ) ตัวละ 1 โปรเซส
  · บอทตาย → เปิดใหม่เอง (รอ 5 → 10 → … ≤ 60 วิ ถ้าตายซ้ำเร็วๆ)
  · state/bot_settings.json เปลี่ยน (กดบันทึกในหน้า /settings หรือกดปิด/เปิดบอท):
        แค่รายชื่อบอทที่ปิดเปลี่ยน → หยุด/เปิดเฉพาะตัวนั้น
        ค่าอื่นเปลี่ยน (TF / lot / เป้า / ขยาย SL) → รีสตาร์ทบอททดสอบทั้งหมด (main ไม่แตะ)
  · ได้ SIGTERM (docker stop) → ปิดบอททุกตัวอย่างสุภาพ
"""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .. import config as C
from .. import settings as S

log = logging.getLogger("supervisor")
POLL = 1.0


@dataclass
class Child:
    name: str
    proc: subprocess.Popen | None = None
    started: float = 0.0
    backoff: float = 5.0
    next_start: float = 0.0
    restarts: int = field(default=0)


class Supervisor:
    def __init__(self) -> None:
        self.children = {n: Child(n) for n in C.LIVE_INSTANCES}
        self.settings = S.load()
        self.mtime = self._mtime()
        self.running = True

    @staticmethod
    def _mtime() -> float:
        try:
            return S.PATH.stat().st_mtime
        except OSError:
            return 0.0

    def enabled(self, name: str) -> bool:
        return name == "main" or name not in self.settings["disabled_bots"]

    def start(self, c: Child) -> None:
        c.proc = subprocess.Popen([sys.executable, "-m", "bot", "live", "--instance", c.name], cwd=str(C.ROOT),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        c.started = time.time()
        log.info("start %s pid=%s", c.name, c.proc.pid)

    def stop(self, c: Child, why: str) -> None:
        if c.proc is not None and c.proc.poll() is None:
            log.info("stop %s (%s)", c.name, why)
            c.proc.terminate()
            try:
                c.proc.wait(15)
            except subprocess.TimeoutExpired:
                c.proc.kill()
        c.proc = None
        c.next_start = 0.0
        c.backoff = 5.0

    def check_settings(self) -> None:
        m = self._mtime()
        if m == self.mtime:
            return
        self.mtime = m
        new = S.load()
        old, self.settings = self.settings, new
        others_changed = {k: v for k, v in old.items() if k != "disabled_bots"} != \
                         {k: v for k, v in new.items() if k != "disabled_bots"}
        for c in self.children.values():
            if c.name == "main" or c.proc is None:
                continue                                  # main ไม่อ่านค่าจากหน้าเว็บ · ตัวที่ไม่ได้รันไม่ต้องหยุด
            if not self.enabled(c.name):
                self.stop(c, "ปิดในหน้าเว็บ")
            elif others_changed:
                self.stop(c, "ค่าตั้งเปลี่ยน")           # รอบถัดไปเปิดใหม่ด้วยค่าใหม่
        log.info("settings changed: %s", new)

    def tick(self) -> None:
        now = time.time()
        for c in self.children.values():
            if not self.enabled(c.name):
                continue
            if c.proc is not None and c.proc.poll() is not None:
                code = c.proc.returncode
                c.proc = None
                c.restarts += 1
                # ตายเร็ว (< 1 นาที) = มีปัญหา → รอนานขึ้นเรื่อยๆ ไม่เปิดถี่จนรก log
                c.backoff = min(c.backoff * 2, 60.0) if now - c.started < 60 else 5.0
                c.next_start = now + c.backoff
                log.warning("%s exited code=%s → เปิดใหม่ใน %.0f วิ (ครั้งที่ %d)", c.name, code, c.backoff, c.restarts)
            if c.proc is None and now >= c.next_start:
                self.start(c)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        log.info("supervisor: %s", [n for n in self.children if self.enabled(n)])
        try:
            while self.running:
                self.check_settings()
                self.tick()
                time.sleep(POLL)
        except KeyboardInterrupt:
            pass
        for c in self.children.values():
            self.stop(c, "supervisor หยุด")
