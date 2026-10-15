"""
regime.py — จัดสภาพตลาดบนแท่ง H1 (สร้างจาก M15 แบบ incremental ไม่มองอนาคต)

    TREND_UP / TREND_DOWN : ADX(14) ≥ 25 และ EMA50 ขยับใน 10 แท่ง ≥ 0.5 ATR(14)
    HIGH_VOL              : ATR(24) อยู่ใน percentile ≥ 90 ของ 2,400 แท่ง H1 ล่าสุด (~100 วัน)
    DEAD                  : percentile ≤ 10
    RANGE                 : ที่เหลือ

ทำไม ATR(24) สำหรับ percentile: H1 ช่วงเอเชียเงียบกว่าช่วงนิวยอร์กเสมอ ถ้าใช้ ATR(14)
ตลาดจะ "DEAD" ทุกคืนตามเวลา ไม่ใช่ตามสภาพจริง — ATR(24) ครอบคลุมทั้งวันจึงตัดผลของเวลาออก

ค่าคงที่ทุกตัวกำหนดล่วงหน้า (research/hypotheses.md ด่าน 0) — ห้ามจูน
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .types import Bar


class Regime(Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


class BarAggregator:
    """รวมแท่งเล็กเป็นแท่งใหญ่ (เช่น M15 → H1) คืนแท่งใหญ่เมื่อ "ปิดแล้ว" เท่านั้น"""

    def __init__(self, minutes: int) -> None:
        self.step = timedelta(minutes=minutes)
        self._cur: list | None = None   # [bucket, open, high, low, close, volume]

    def update(self, bar: Bar) -> Bar | None:
        epoch = datetime(1970, 1, 1, tzinfo=bar.time.tzinfo)
        bucket = epoch + ((bar.time - epoch) // self.step) * self.step
        done = None
        if self._cur is not None and bucket != self._cur[0]:
            b, o, h, l, c, v = self._cur
            done = Bar(b, o, h, l, c, 0.0, v)
            self._cur = None
        if self._cur is None:
            self._cur = [bucket, bar.open, bar.high, bar.low, bar.close, bar.volume]
        else:
            self._cur[2] = max(self._cur[2], bar.high)
            self._cur[3] = min(self._cur[3], bar.low)
            self._cur[4] = bar.close
            self._cur[5] += bar.volume
        return done


class WilderATR:
    def __init__(self, n: int) -> None:
        self.n, self.value, self._seed, self._prev = n, None, [], None

    def update(self, b: Bar) -> float | None:
        tr = b.high - b.low if self._prev is None else max(b.high - b.low, abs(b.high - self._prev), abs(b.low - self._prev))
        self._prev = b.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


class ADX:
    """Average Directional Index แบบ Wilder"""

    def __init__(self, n: int = 14) -> None:
        self.n = n
        self._prev: Bar | None = None
        self._tr = self._pdm = self._mdm = 0.0
        self._count = 0
        self._dx_seed: list[float] = []
        self.value: float | None = None
        self.plus_di = self.minus_di = 0.0

    def update(self, b: Bar) -> float | None:
        p = self._prev
        self._prev = b
        if p is None:
            return None
        up, down = b.high - p.high, p.low - b.low
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        tr = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        n = self.n
        self._count += 1
        if self._count <= n:                       # สะสมช่วงแรกเป็นผลรวม
            self._tr += tr
            self._pdm += pdm
            self._mdm += mdm
            if self._count < n:
                return None
        else:                                      # Wilder smoothing แบบผลรวม
            self._tr = self._tr - self._tr / n + tr
            self._pdm = self._pdm - self._pdm / n + pdm
            self._mdm = self._mdm - self._mdm / n + mdm
        if self._tr <= 0:
            return self.value
        self.plus_di = 100 * self._pdm / self._tr
        self.minus_di = 100 * self._mdm / self._tr
        s = self.plus_di + self.minus_di
        dx = 100 * abs(self.plus_di - self.minus_di) / s if s > 0 else 0.0
        if self.value is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) == n:
                self.value = sum(self._dx_seed) / n
        else:
            self.value = (self.value * (n - 1) + dx) / n
        return self.value


class RollingPercentile:
    """percentile rank ของค่าล่าสุดเทียบหน้าต่าง N ค่า (O(log N) ต่อการอัปเดต)"""

    def __init__(self, n: int) -> None:
        self.n = n
        self._q: deque[float] = deque()
        self._sorted: list[float] = []

    def update(self, x: float) -> float | None:
        self._q.append(x)
        bisect.insort(self._sorted, x)
        if len(self._q) > self.n:
            old = self._q.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]
        if len(self._q) < self.n:
            return None
        return bisect.bisect_left(self._sorted, x) / (len(self._sorted) - 1)


@dataclass(frozen=True)
class RegimeConfig:
    adx_period: int = 14
    adx_trend: float = 25.0
    ema_period: int = 50
    slope_bars: int = 10
    slope_atr: float = 0.5
    vol_atr: int = 24
    vol_window: int = 2400
    high_vol_pct: float = 0.90
    dead_pct: float = 0.10


@dataclass(frozen=True)
class RegimeState:
    time: datetime          # เวลาปิดของแท่ง H1 ที่ใช้ตัดสิน
    regime: Regime
    adx: float | None
    slope: float | None     # หน่วย ATR(14)
    vol_pct: float | None
    h1: Bar


class RegimeClassifier:
    def __init__(self, cfg: RegimeConfig | None = None, tf_minutes: int = 60) -> None:
        self.cfg = c = cfg or RegimeConfig()
        self._agg = BarAggregator(tf_minutes)
        self._tf = timedelta(minutes=tf_minutes)
        self._adx = ADX(c.adx_period)
        self._atr = WilderATR(14)
        self._vol_atr = WilderATR(c.vol_atr)
        self._pct = RollingPercentile(c.vol_window)
        self._k = 2 / (c.ema_period + 1)
        self._ema: float | None = None
        self._ema_count = 0
        self._ema_hist: deque[float] = deque(maxlen=c.slope_bars + 1)
        self.state: RegimeState | None = None

    @property
    def atr(self) -> float | None:
        """ATR(14) ของ timeframe ใหญ่ (ใช้กำหนดระยะ SL ของ setup)"""
        return self._atr.value

    def update(self, bar: Bar) -> RegimeState | None:
        """ป้อนแท่ง M15 — คืน state ใหม่เมื่อแท่ง H1 ปิด (มิฉะนั้น None)"""
        h1 = self._agg.update(bar)
        if h1 is None:
            return None
        return self.update_htf(h1)

    def update_htf(self, h1: Bar) -> RegimeState:
        c = self.cfg
        adx = self._adx.update(h1)
        atr = self._atr.update(h1)
        vatr = self._vol_atr.update(h1)
        pct = self._pct.update(vatr) if vatr is not None else None
        self._ema = h1.close if self._ema is None else self._ema + self._k * (h1.close - self._ema)
        self._ema_count += 1
        self._ema_hist.append(self._ema)
        slope = None
        if atr and len(self._ema_hist) == self._ema_hist.maxlen and self._ema_count >= c.ema_period:
            slope = (self._ema_hist[-1] - self._ema_hist[0]) / atr

        if adx is None or slope is None or pct is None:
            regime = Regime.UNKNOWN
        elif pct >= c.high_vol_pct:
            regime = Regime.HIGH_VOL
        elif pct <= c.dead_pct:
            regime = Regime.DEAD
        elif adx >= c.adx_trend and slope >= c.slope_atr:
            regime = Regime.TREND_UP
        elif adx >= c.adx_trend and slope <= -c.slope_atr:
            regime = Regime.TREND_DOWN
        else:
            regime = Regime.RANGE
        self.state = RegimeState(h1.time + self._tf, regime, adx, slope, pct, h1)
        return self.state
