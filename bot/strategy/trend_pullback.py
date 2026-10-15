"""
trend_pullback.py — เทรนด์ timeframe ใหญ่ + ย่อแตะ EMA บน timeframe เล็ก

กฎ (ฝั่ง BUY — SELL กลับกัน):
  1) เทรนด์: ราคาปิดแท่ง H4 ล่าสุด > EMA(trend_ema) ของ H4
     (H4 สร้างจากแท่ง M15 แบบ incremental — ใช้เฉพาะแท่ง H4 ที่ปิดแล้ว ไม่มองอนาคต)
  2) ย่อ: low ของแท่ง M15 แตะหรือหลุด EMA(pullback_ema) ของ M15
  3) ยืนยัน: แท่งเดียวกันปิดกลับเหนือ EMA และเป็นแท่งเขียว
  4) SL = close - sl_atr * ATR(14, M15), TP = tp_r * R
  5) วันละ 1 ไม้ เทรดเฉพาะช่วง trade_start–trade_end, ปิดหมดที่ flat_time

ข้อสังเกตจาก research: ตัวกรอง EMA50(H4) ของทองให้ผลเท่ากับซื้อเฉยๆ (ไม่มี edge เพิ่ม)
กลยุทธ์นี้จึงต้องเทียบกับ RandomEntry ฝั่ง long เสมอ
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.indicators import ATR
from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, flat_signals


class EMA:
    def __init__(self, period: int) -> None:
        self.k = 2 / (period + 1)
        self.value: float | None = None
        self.count = 0
        self.period = period

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.k * (x - self.value)
        self.count += 1
        return self.value

    @property
    def ready(self) -> bool:
        return self.count >= self.period


class HigherTimeframeClose:
    """รวมแท่งเล็กเป็นแท่งใหญ่ คืนราคาปิดเมื่อแท่งใหญ่ "ปิดแล้ว" เท่านั้น"""

    def __init__(self, minutes: int) -> None:
        self.step = timedelta(minutes=minutes)
        self._bucket: datetime | None = None
        self._last_close: float | None = None

    def update(self, bar: Bar) -> float | None:
        epoch = datetime(1970, 1, 1, tzinfo=bar.time.tzinfo)
        bucket = epoch + ((bar.time - epoch) // self.step) * self.step
        closed = None
        if self._bucket is not None and bucket != self._bucket:
            closed = self._last_close
        self._bucket = bucket
        self._last_close = bar.close
        return closed


@dataclass(frozen=True)
class PullbackConfig:
    tag: str
    symbol: str
    window: SessionWindow             # range_start/range_end ไม่ได้ใช้ (ใส่ช่วงสั้นๆ ก่อน trade start)
    trend_tf_min: int = 240
    trend_ema: int = 50
    pullback_ema: int = 20
    sl_atr: float = 1.5
    tp_r: float = 2.0
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class TrendPullback(Strategy):
    def __init__(self, cfg: PullbackConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self.rng = SessionRange(cfg.window, timeframe)
        self._htf = HigherTimeframeClose(cfg.trend_tf_min)
        self._trend_ema = EMA(cfg.trend_ema)
        self._htf_close: float | None = None
        self._ema = EMA(cfg.pullback_ema)
        self._atr = ATR(14)

    @property
    def trend(self) -> int:
        if self._htf_close is None or not self._trend_ema.ready or self._trend_ema.value is None:
            return 0
        return 1 if self._htf_close > self._trend_ema.value else -1

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        closed = self._htf.update(bar)
        if closed is not None:
            self._htf_close = closed
            self._trend_ema.update(closed)
        ema = self._ema.update(bar.close)
        atr = self._atr.update(bar)
        self.rng.update(bar)

        exits = flat_signals(self, self.rng, account)
        if exits or not self.rng.in_trade_window or atr is None or not self._ema.ready:
            return exits

        t = self.trend
        if t > 0 and bar.low <= ema < bar.close and bar.close > bar.open:
            side = Side.BUY
        elif t < 0 and bar.high >= ema > bar.close and bar.close < bar.open:
            side = Side.SELL
        else:
            return []
        risk = self.cfg.sl_atr * atr
        sl = bar.close - int(side) * risk
        tp = bar.close + int(side) * self.cfg.tp_r * risk
        return [EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{self.rng.day}",
                            reason=f"trend {'up' if t > 0 else 'down'}, pullback to EMA{self.cfg.pullback_ema}")]
