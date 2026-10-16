"""
levels.py — ระดับราคาสำคัญที่นักเทรดทั่วโลกเห็นตรงกัน (และวาง stop ไว้เลยออกไป)

    PDH / PDL : high / low ของวันทำการก่อนหน้า
    PWH / PWL : high / low ของสัปดาห์ก่อนหน้า

นับวัน/สัปดาห์ตาม tz ที่กำหนด (ทอง = UTC, NAS100 = America/New_York) และข้ามแท่งวันเสาร์/อาทิตย์
ไม่งั้นแท่งเปิดตลาดคืนวันอาทิตย์จะกลายเป็น "เมื่อวาน" ที่กว้างแค่ไม่กี่เหรียญ
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..core.timeutil import SessionClock
from ..core.types import Bar


class KeyLevels:
    def __init__(self, tz: str) -> None:
        self.clock = SessionClock(tz)
        self.day: date | None = None
        self.week: tuple[int, int] | None = None
        self._dh = self._dl = None
        self._wh = self._wl = None
        self.pdh = self.pdl = self.pwh = self.pwl = None

    def update(self, bar: Bar) -> None:
        local = self.clock.local(bar.time)
        if local.weekday() >= 5:
            return
        d = local.date()
        w = tuple(local.isocalendar())[:2]
        if d != self.day:
            if self._dh is not None:
                self.pdh, self.pdl = self._dh, self._dl
            self.day, self._dh, self._dl = d, bar.high, bar.low
        else:
            self._dh, self._dl = max(self._dh, bar.high), min(self._dl, bar.low)
        if w != self.week:
            if self._wh is not None:
                self.pwh, self.pwl = self._wh, self._wl
            self.week, self._wh, self._wl = w, bar.high, bar.low
        else:
            self._wh, self._wl = max(self._wh, bar.high), min(self._wl, bar.low)

    def upper(self) -> dict[str, float]:
        return {k: v for k, v in (("PDH", self.pdh), ("PWH", self.pwh)) if v is not None}

    def lower(self) -> dict[str, float]:
        return {k: v for k, v in (("PDL", self.pdl), ("PWL", self.pwl)) if v is not None}


def hold_exits(strategy, bar: Bar, account, max_hold: timedelta, bar_duration: timedelta) -> list:  # noqa: ANN001
    """time stop + ปิดก่อนสุดสัปดาห์ (ศุกร์ 20:00 UTC) — ใช้ร่วมกันทุก setup ที่ถือข้ามวันได้"""
    from ..core.types import ExitSignal
    positions = account.positions(strategy.symbol, strategy.tag)
    if not positions:
        return []
    now = bar.time + bar_duration
    if now.weekday() == 4 and now.hour >= 20:
        return [ExitSignal(strategy.symbol, strategy.tag, "weekend")]
    # ไม้ที่ล็อกกำไรแบบไต่อยู่ (plan.lock_tiers) ปล่อยวิ่งต่อ ไม่ตัดด้วยเวลา — SL ที่ล็อกไว้คุมเอง
    locking = bool(getattr(getattr(strategy, "plan", None), "lock_tiers", ()))
    if any(now - p.open_time >= max_hold and not (locking and p.moved_to_be) for p in positions):
        return [ExitSignal(strategy.symbol, strategy.tag, "time_stop")]
    return []


def in_rollover(t: datetime) -> bool:
    """21:00–23:00 UTC: สเปรดถ่าง สภาพคล่องหาย — ไม่เข้าไม้ใหม่"""
    return 21 <= t.hour < 23
