"""
narrow_range.py — กลยุทธ์จากงานวิจัย v5 (research/hypotheses_v5_gold.md)

  NarrowRange : เมื่อวานแคบสุดใน n วัน → แท่งปิดทะลุ high/low เมื่อวาน (หลังลอนดอนเปิด) เข้าตามทิศ · วันละ 1 ไม้
  TrendDay    : ถึงเวลาตัดสิน กรอบวันนี้เกิน k × ADR → ตามทิศจากเปิดวัน ถือถึงเวลาปิด
                (สมมติฐาน G5 "สวนวันที่วิ่งเกินกำลัง" แพ้ทั้งสองช่วงข้อมูล จึงทดสอบทิศกลับกัน)
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from ..core.interfaces import IAccount
from ..core.timeutil import minutes_of_day, parse_hhmm
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, flat_signals, status_dict

NR_WINDOW = SessionWindow("UTC", "00:00", "07:00", "19:00", "20:00")


@dataclass(frozen=True)
class NarrowRangeConfig:
    tag: str
    symbol: str
    nr_n: int = 4
    sl_mode: str = "mid"           # "mid" = กลางกรอบเมื่อวาน | "opposite" = อีกฝั่งของกรอบ
    tp_r: float | None = None
    side_mode: str = "rule"        # "rule" = กลยุทธ์จริง | "random" = benchmark ที่กลไกเหมือน
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class NarrowRange(Strategy):
    def __init__(self, cfg: NarrowRangeConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self.rng = SessionRange(NR_WINDOW, timeframe)
        self._rand = random.Random(7)
        self._days: deque[tuple[float, float]] = deque(maxlen=cfg.nr_n)
        self._day = None
        self._hi = self._lo = 0.0
        self._box: tuple[float, float] | None = None

    def _track(self, bar: Bar) -> None:
        day = bar.time.date()
        if day != self._day:
            if self._day is not None and self._day.weekday() < 5:
                self._days.append((self._hi, self._lo))
            self._day, self._hi, self._lo = day, bar.high, bar.low
            self._box = None
            if len(self._days) == self.cfg.nr_n:
                widths = [h - lo for h, lo in self._days]
                if widths[-1] == min(widths):
                    self._box = self._days[-1]
        else:
            self._hi, self._lo = max(self._hi, bar.high), min(self._lo, bar.low)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        self._track(bar)
        rng = self.rng
        rng.update(bar)
        exits = flat_signals(self, rng, account)
        if exits or not rng.in_trade_window or self._box is None:
            return exits
        hi, lo = self._box
        if lo <= bar.close <= hi:
            return []
        side = Side.BUY if bar.close > hi else Side.SELL
        if self.cfg.side_mode == "random":
            side = Side.BUY if self._rand.random() < 0.5 else Side.SELL
        mid = (hi + lo) / 2
        sl = mid if self.cfg.sl_mode == "mid" else (lo if side == Side.BUY else hi)
        if (bar.close - sl) * int(side) <= 0:          # สุ่มทิศอาจอยู่ผิดฝั่ง SL → ใช้ระยะเท่ากันอีกด้าน
            sl = bar.close - int(side) * abs(bar.close - sl)
        tp = bar.close + int(side) * self.cfg.tp_r * abs(bar.close - sl) if self.cfg.tp_r else None
        return [EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{rng.day}",
                            reason=f"close {bar.close:.2f} beyond NR{self.cfg.nr_n} [{lo:.2f}, {hi:.2f}]")]

    def status(self) -> dict:
        if self._box is None:
            return status_dict(self, "narrow_range", f"เมื่อวานไม่ใช่วันแคบสุดใน {self.cfg.nr_n} วัน")
        hi, lo = self._box
        return status_dict(self, "narrow_range", "เฝ้าขอบกรอบเมื่อวาน", detail=f"กรอบ {lo:.2f}–{hi:.2f}")


@dataclass(frozen=True)
class TrendDayConfig:
    tag: str
    symbol: str
    entry_time: str = "14:00"
    min_range_adr: float = 1.3
    sl_adr: float = 0.5
    tp_r: float | None = None
    adr_lookback: int = 20
    side_mode: str = "rule"
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class TrendDay(Strategy):
    def __init__(self, cfg: TrendDayConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self._rand = random.Random(7)
        entry = minutes_of_day(parse_hhmm(cfg.entry_time))
        end = f"{(entry + 30) // 60:02d}:{(entry + 30) % 60:02d}"
        self.rng = SessionRange(SessionWindow("UTC", "00:00", cfg.entry_time, end, "20:00"), timeframe, cfg.adr_lookback)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        rng = self.rng
        rng.update(bar)
        exits = flat_signals(self, rng, account)
        if exits or not rng.in_trade_window or not rng.complete or rng.range_open is None:
            return exits
        adr, ratio = rng.daily.adr, rng.width_ratio()
        move = bar.close - rng.range_open
        if adr is None or ratio is None or ratio < self.cfg.min_range_adr or move == 0:
            return []
        side = Side.BUY if move > 0 else Side.SELL
        if self.cfg.side_mode == "random":
            side = Side.BUY if self._rand.random() < 0.5 else Side.SELL
        d = self.cfg.sl_adr * adr
        tp = bar.close + int(side) * self.cfg.tp_r * d if self.cfg.tp_r else None
        return [EnterSignal(self.symbol, side, bar.close - int(side) * d, tp, self.tag, key=f"{self.tag}:{rng.day}",
                            reason=f"day range {ratio:.2f} ADR, move {move:+.2f}")]

    def status(self) -> dict:
        ratio = self.rng.width_ratio()
        return status_dict(self, "trend_day", "รอเวลาตัดสิน", detail=f"ตัดสิน {self.cfg.entry_time} UTC · กรอบ {ratio or 0:.2f} ADR")
