"""
gate.py — ตัวกรองที่ผู้จัดการพอร์ต "เรียนรู้" แล้วงานวิจัยยืนยันว่าดีจริง ก่อนใส่ให้บอท

ห่อกลยุทธ์เดิม: สัญญาณเข้าที่เกิดในสภาพตลาด/ช่วงเวลาที่ห้าม → ทิ้ง (สัญญาณออกผ่านเสมอ)
  skip_regimes  : trend / range / mixed — สภาพตลาดจาก efficiency ratio ของ M5 25 แท่ง (ตัวเดียวกับที่ผู้จัดการใช้)
  skip_sessions : asia (00–07 UTC) / europe (07–13) / us (13–24)
ไม่ใช่การ "พักบอท": บอทยังทำงานตลอด แค่กลยุทธ์ถูกปรับให้ไม่เข้าไม้ในจังหวะที่ผลจริง + ทดสอบย้อนหลังบอกว่าเสียเปรียบ

สองโหมด (การทดลองแบบ A/B บนไม้จริง — กันตัวกรองที่ดีแค่ในข้อมูลย้อนหลัง):
  observe : ไม่บล็อก แค่จดว่า "ไม้นี้ฉันจะบล็อก" (state/<บอท>/gate_flags.jsonl) → เทียบผลไม้ที่ถูกจด vs ไม่ถูกจด
  active  : บล็อกจริง — ได้เลื่อนขั้นเมื่อไม้จริงที่ถูกจดในโหมด observe ขาดทุนชัดพอ (research_weekly.gate_evidence)
"""
from __future__ import annotations

import json
import time
from collections import deque

from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, Signal
from .base import Strategy

ER_TREND, ER_RANGE = 0.40, 0.20
ER_BARS = 25                     # M5 25 แท่ง = 24 ช่วง ≈ 2 ชม.
SESSIONS = {"asia": (0, 7), "europe": (7, 13), "us": (13, 24)}


def efficiency(closes: list[float]) -> float:
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return abs(closes[-1] - closes[0]) / path if path else 0.0


def regime_of(er: float) -> str:
    return "trend" if er >= ER_TREND else "range" if er <= ER_RANGE else "mixed"


def session_of(hour_utc: int) -> str:
    return next(k for k, (a, b) in SESSIONS.items() if a <= hour_utc < b)


def signature(skip_regimes: tuple | list, skip_sessions: tuple | list) -> str:
    return ",".join(sorted(skip_regimes)) + "|" + ",".join(sorted(skip_sessions))


class Gate(Strategy):
    FRESH_SEC = 3600        # จดเฉพาะแท่งสด (แท่งย้อนหลังตอนวอร์มอัปหลังรีสตาร์ทไม่จดซ้ำ) · 0 = จดทุกแท่ง
    LOG_DIR = None          # บอทจริงตั้งเป็น state/<บอท> ตอนเริ่ม (live/runner.py) · backtest/วิจัย = ไม่จดไฟล์

    def __init__(self, inner: Strategy, skip_regimes: tuple[str, ...] = (), skip_sessions: tuple[str, ...] = (),
                 mode: str = "active") -> None:
        self.inner = inner
        self.tag, self.symbol, self.plan = inner.tag, inner.symbol, inner.plan
        self.skip_regimes, self.skip_sessions = tuple(skip_regimes), tuple(skip_sessions)
        self.mode = mode
        self.sig = signature(self.skip_regimes, self.skip_sessions)
        self.m5: deque[float] = deque(maxlen=ER_BARS)     # ราคาปิดของแท่ง M5 ที่ปิดแล้ว
        self._bucket: int | None = None
        self._last_close = 0.0
        self.blocked = 0

    def regime(self) -> str | None:
        return regime_of(efficiency(list(self.m5))) if len(self.m5) == ER_BARS else None

    def _feed(self, bar: Bar) -> None:
        b = int(bar.time.timestamp()) // 300
        if self._bucket is not None and b != self._bucket:
            self.m5.append(self._last_close)          # แท่ง M5 ก่อนหน้าปิดแล้ว
        self._bucket, self._last_close = b, bar.close

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        self._feed(bar)
        self.inner.last_bar = bar
        sigs = self.inner.on_bar(bar, account)
        if not any(isinstance(s, EnterSignal) for s in sigs):
            return sigs
        rg = self.regime()
        if (rg is not None and rg in self.skip_regimes) or session_of(bar.time.hour) in self.skip_sessions:
            self.blocked += 1
            if self.mode == "observe":
                self._flag(bar, sigs, rg)
                return sigs
            return [s for s in sigs if not isinstance(s, EnterSignal)]
        return sigs

    def _flag(self, bar: Bar, sigs: list[Signal], rg: str | None) -> None:
        if self.LOG_DIR is None:
            return
        if self.FRESH_SEC and bar.time.timestamp() < time.time() - self.FRESH_SEC:
            return
        try:
            with (self.LOG_DIR / "gate_flags.jsonl").open("a", encoding="utf-8") as f:
                for s in sigs:
                    if isinstance(s, EnterSignal):
                        f.write(json.dumps({"t": bar.time.isoformat(), "key": s.key, "side": s.side.name, "gate": self.sig,
                                            "regime": rg, "session": session_of(bar.time.hour)}) + "\n")
        except OSError:
            pass

    def status(self) -> dict:
        st = dict(self.inner.status())
        if st:
            st["gate"] = {"skip_regimes": list(self.skip_regimes), "skip_sessions": list(self.skip_sessions), "mode": self.mode,
                          "regime": self.regime(), "blocked": self.blocked}
        return st
