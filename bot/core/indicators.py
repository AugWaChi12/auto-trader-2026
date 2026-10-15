"""
indicators.py — indicator แบบ incremental (อัปเดตทีละแท่ง O(1))

ใช้แบบ streaming แทนการคำนวณทั้ง DataFrame ล่วงหน้า เพื่อให้โค้ดชุดเดียว
ใช้ได้ทั้ง backtest และ live และกันการเผลอ "มองอนาคต" (look-ahead bias) โดยโครงสร้าง
"""
from __future__ import annotations

from collections import deque
from datetime import date

from .types import Bar


class ATR:
    """Average True Range แบบ Wilder"""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - self._prev_close), abs(bar.low - self._prev_close))
        self._prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class DailyRange:
    """ติดตาม high/low ของแต่ละวัน (ตามวันท้องถิ่นที่ส่งเข้ามา) และค่าเฉลี่ยช่วงกว้างรายวัน (ADR)

    ADR ใช้เป็นไม้บรรทัดว่ากรอบ session "แคบ" หรือ "กว้าง" — ไม่ผูกกับระดับราคา
    ทองที่ 1,800 กับ 2,600 มีความผันผวนเป็นดอลลาร์ต่างกันมาก
    """

    def __init__(self, lookback: int = 10) -> None:
        self._ranges: deque[float] = deque(maxlen=lookback)
        self.day: date | None = None
        self.high = float("-inf")
        self.low = float("inf")
        self.prev_high: float | None = None
        self.prev_low: float | None = None

    def update(self, local_day: date, bar: Bar) -> None:
        if local_day != self.day:
            if self.day is not None and self.high > self.low:
                self._ranges.append(self.high - self.low)
                self.prev_high, self.prev_low = self.high, self.low
            self.day = local_day
            self.high, self.low = bar.high, bar.low
        else:
            self.high = max(self.high, bar.high)
            self.low = min(self.low, bar.low)

    @property
    def adr(self) -> float | None:
        if len(self._ranges) < self._ranges.maxlen:  # type: ignore[operator]
            return None
        return sum(self._ranges) / len(self._ranges)
