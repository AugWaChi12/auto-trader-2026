"""
scalp.py — ห้องทดลองสแกลป์ (research/hypotheses_v3_scalp.md)

    RsiReversion  (S1/S2) : RSI(2) สุดโต่งตามทิศ EMA200 → เข้าสวนระยะสั้น ออกเมื่อปิดข้าม EMA5
    EmaCross      (S3)    : EMA เร็วตัด EMA ช้าตามทิศ EMA200 → TP = tp_r × R
    BollingerFade (S4)    : ช่วงเอเชีย ปิดนอก Bollinger → สวนกลับไปเส้นกลาง

เข้าได้หลายไม้ต่อวัน (key ต่อแท่ง) แต่ถือได้ทีละไม้ต่อกลยุทธ์ (RiskManager.can_open)
side_mode "random" / "long" = benchmark: จังหวะเข้า ระยะ SL/TP และกฎออกเหมือนเดิม ต่างแค่ทิศ
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from ..core.indicators import ATR
from ..core.interfaces import IAccount
from ..core.types import TIMEFRAME_MINUTES, Bar, EnterSignal, ExitSignal, Side, Signal
from .base import ManagementPlan, Strategy
from .levels import hold_exits, in_rollover
from .trend_pullback import EMA


class RSI:
    """RSI แบบ Wilder"""

    def __init__(self, n: int) -> None:
        self.n, self.value = n, None
        self._prev: float | None = None
        self._gain = self._loss = 0.0
        self._count = 0

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        ch = close - self._prev
        self._prev = close
        g, l = max(ch, 0.0), max(-ch, 0.0)
        self._count += 1
        if self._count <= self.n:
            self._gain += g / self.n
            self._loss += l / self.n
            if self._count < self.n:
                return None
        else:
            self._gain = (self._gain * (self.n - 1) + g) / self.n
            self._loss = (self._loss * (self.n - 1) + l) / self.n
        self.value = 100.0 if self._loss == 0 else 100 - 100 / (1 + self._gain / self._loss)
        return self.value


class Bollinger:
    def __init__(self, n: int) -> None:
        self._q: deque[float] = deque(maxlen=n)

    def update(self, close: float) -> tuple[float, float] | None:
        self._q.append(close)
        if len(self._q) < self._q.maxlen:  # type: ignore[operator]
            return None
        m = sum(self._q) / len(self._q)
        sd = math.sqrt(sum((x - m) ** 2 for x in self._q) / len(self._q))
        return m, sd


@dataclass(frozen=True)
class ScalpWindow:
    start_hour: int = 7
    end_hour: int = 20


class _ScalpBase(Strategy):
    def __init__(self, tag: str, symbol: str, timeframe: str, window: ScalpWindow, max_hold: int,
                 side_mode: str, seed: int) -> None:
        self.tag, self.symbol = tag, symbol
        self.plan = ManagementPlan(partial_r=None, trail_atr_mult=None)
        self._bar = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        self._max_hold = self._bar * max_hold
        self._window = window
        self._side_mode = side_mode
        self._rand = random.Random(seed)
        self.atr = ATR(14)

    def _can_enter(self, bar: Bar) -> bool:
        t = bar.time + self._bar
        return (self._window.start_hour <= t.hour < self._window.end_hour and not in_rollover(t)
                and not (t.weekday() == 4 and t.hour >= 19) and t.weekday() < 5)

    def _side(self, rule: Side) -> Side:
        coin = self._rand.random()
        if self._side_mode == "long":
            return Side.BUY
        if self._side_mode == "random":
            return Side.BUY if coin < 0.5 else Side.SELL
        return rule

    def _signal(self, bar: Bar, rule: Side, d: float, tp_d: float | None, reason: str) -> list[Signal]:
        side = self._side(rule)
        sl = bar.close - int(side) * d
        tp = None if tp_d is None else bar.close + int(side) * tp_d
        return [EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{bar.time:%Y%m%d%H%M}", reason=reason)]


# ---------------------------------------------------------------------------- S1/S2
@dataclass(frozen=True)
class RsiReversionConfig:
    tag: str
    symbol: str
    rsi_th: float = 10.0
    sl_atr: float = 1.5
    max_hold: int = 12
    window: ScalpWindow = field(default_factory=ScalpWindow)
    side_mode: str = "rule"
    seed: int = 7


class RsiReversion(_ScalpBase):
    def __init__(self, cfg: RsiReversionConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        self.rsi, self.ema5, self.ema200 = RSI(2), EMA(5), EMA(200)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        rsi, e5, e200, atr = self.rsi.update(bar.close), self.ema5.update(bar.close), self.ema200.update(bar.close), \
            self.atr.update(bar)
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits:
            return exits
        pos = account.positions(self.symbol, self.tag)
        if pos:
            p = pos[0]
            if (p.side is Side.BUY and bar.close > e5) or (p.side is Side.SELL and bar.close < e5):
                return [ExitSignal(self.symbol, self.tag, "ema5")]
            return []
        if rsi is None or atr is None or not self.ema200.ready or not self._can_enter(bar):
            return []
        if rsi < self.cfg.rsi_th and bar.close > e200:
            rule = Side.BUY
        elif rsi > 100 - self.cfg.rsi_th and bar.close < e200:
            rule = Side.SELL
        else:
            return []
        return self._signal(bar, rule, self.cfg.sl_atr * atr, None, f"RSI2={rsi:.0f}")


# ---------------------------------------------------------------------------- S3
@dataclass(frozen=True)
class EmaCrossConfig:
    tag: str
    symbol: str
    fast: int = 9
    slow: int = 21
    sl_atr: float = 1.5
    tp_r: float = 1.5
    max_hold: int = 24
    window: ScalpWindow = field(default_factory=ScalpWindow)
    side_mode: str = "rule"
    seed: int = 7


class EmaCross(_ScalpBase):
    def __init__(self, cfg: EmaCrossConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        self.fast, self.slow, self.ema200 = EMA(cfg.fast), EMA(cfg.slow), EMA(200)
        self._prev_diff: float | None = None

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        f, s, e200, atr = self.fast.update(bar.close), self.slow.update(bar.close), self.ema200.update(bar.close), \
            self.atr.update(bar)
        diff, prev = f - s, self._prev_diff
        self._prev_diff = diff
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits or account.positions(self.symbol, self.tag):
            return exits
        if prev is None or atr is None or not self.ema200.ready or not self._can_enter(bar):
            return []
        if prev <= 0 < diff and bar.close > e200:
            rule = Side.BUY
        elif prev >= 0 > diff and bar.close < e200:
            rule = Side.SELL
        else:
            return []
        d = self.cfg.sl_atr * atr
        return self._signal(bar, rule, d, self.cfg.tp_r * d, f"EMA{self.cfg.fast}/{self.cfg.slow} cross")


# ---------------------------------------------------------------------------- S4
@dataclass(frozen=True)
class BollingerFadeConfig:
    tag: str
    symbol: str
    k: float = 2.5
    sl_atr: float = 1.5
    max_hold: int = 30
    window: ScalpWindow = field(default_factory=lambda: ScalpWindow(0, 6))
    side_mode: str = "rule"
    seed: int = 7


class BollingerFade(_ScalpBase):
    def __init__(self, cfg: BollingerFadeConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        self.bb = Bollinger(20)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        bb, atr = self.bb.update(bar.close), self.atr.update(bar)
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits or account.positions(self.symbol, self.tag):
            return exits
        if bb is None or atr is None or not self._can_enter(bar):
            return []
        mid, sd = bb
        if bar.close < mid - self.cfg.k * sd:
            rule = Side.BUY
        elif bar.close > mid + self.cfg.k * sd:
            rule = Side.SELL
        else:
            return []
        return self._signal(bar, rule, self.cfg.sl_atr * atr, abs(mid - bar.close), f"BB({self.cfg.k}) fade")
