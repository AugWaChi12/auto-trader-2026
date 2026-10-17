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
from .base import ManagementPlan, Strategy, clamp01, status_dict
from .levels import ALWAYS_OPEN, hold_exits, in_rollover
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
                 side_mode: str, seed: int, be_at_r: float | None = None) -> None:
        self.tag, self.symbol = tag, symbol
        # be_at_r: เลื่อน SL ไปจุดเข้าเมื่อกำไรเคยถึงกี่ R (partial_frac=1 → ไม่แบ่งไม้ แค่เลื่อน BE)
        self.plan = ManagementPlan(partial_r=be_at_r, partial_frac=1.0, be_offset_r=0.05, trail_atr_mult=None)
        self._bar = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        self._max_hold = self._bar * max_hold
        self._window = window
        self._side_mode = side_mode
        self._rand = random.Random(seed)
        self.atr = ATR(14)

    def _can_enter(self, bar: Bar) -> bool:
        t = bar.time + self._bar
        if not (self._window.start_hour <= t.hour < self._window.end_hour and not in_rollover(t)):
            return False
        return self.symbol in ALWAYS_OPEN or (not (t.weekday() == 4 and t.hour >= 19) and t.weekday() < 5)

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
    tp_r: float | None = None          # ปิดเมื่อกำไรถึงกี่ R (None = ออกตาม EMA5 เท่านั้น)
    be_at_r: float | None = None
    sl_price: float | None = None      # SL/TP เป็นระยะราคาตายตัว (เช่น ทอง 2.0 = $2 ต่อ 0.01 lot) — ทับค่าแบบ ATR
    tp_price: float | None = None      # ถ้าตั้ง tp_price จะปิดตาม TP/SL เท่านั้น ไม่ใช้ EMA5
    plan: ManagementPlan | None = None # ทับวิธีบริหารไม้ทั้งหมด (BE / trailing / ขยับ TP)


class RsiReversion(_ScalpBase):
    def __init__(self, cfg: RsiReversionConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed,
                         cfg.be_at_r)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
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
            if self.cfg.tp_price is None and not self.plan.tp_usd and not self.plan.lock_tiers and (
                    (p.side is Side.BUY and bar.close > e5) or (p.side is Side.SELL and bar.close < e5)):
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
        d = self.cfg.sl_price or self.cfg.sl_atr * atr
        tp_d = self.cfg.tp_price or (None if self.cfg.tp_r is None else self.cfg.tp_r * d)
        return self._signal(bar, rule, d, tp_d, f"RSI2={rsi:.0f}")

    def status(self) -> dict:
        b, rsi, e200 = self.last_bar, self.rsi.value, self.ema200.value
        if b is None or rsi is None or not self.ema200.ready:
            return status_dict(self, "rsi2", "รอข้อมูล")
        th = self.cfg.rsi_th
        if b.close > e200:   # ขาขึ้น → รอ RSI2 ดิ่งเพื่อ BUY
            prox, side, trigger = clamp01((60 - rsi) / (60 - th)), "BUY", f"< {th:.0f}"
        else:                # ขาลง → รอ RSI2 พุ่งเพื่อ SELL
            prox, side, trigger = clamp01((rsi - 40) / (60 - th)), "SELL", f"> {100 - th:.0f}"
        ok = self._can_enter(b)
        return status_dict(self, "rsi2", "เฝ้า RSI2" if ok else "นอกเวลาเทรด", prox if ok else 0.0, side,
                           f"RSI2 {rsi:.0f} (เข้าเมื่อ {trigger})")


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
    be_at_r: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    plan: ManagementPlan | None = None


class EmaCross(_ScalpBase):
    def __init__(self, cfg: EmaCrossConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed,
                         cfg.be_at_r)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
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
        d = self.cfg.sl_price or self.cfg.sl_atr * atr
        return self._signal(bar, rule, d, self.cfg.tp_price or self.cfg.tp_r * d, f"EMA{self.cfg.fast}/{self.cfg.slow} cross")

    def status(self) -> dict:
        b, atr = self.last_bar, self.atr.value
        if b is None or atr is None or self._prev_diff is None or not self.ema200.ready:
            return status_dict(self, "ema_cross", "รอข้อมูล")
        diff, up = self._prev_diff, b.close > self.ema200.value
        gap = -diff if up else diff               # ระยะที่ต้องวิ่งเพื่อให้ตัดในทิศที่ต้องการ
        prox = 0.0 if gap < 0 else 1 - clamp01(gap / (0.5 * atr))
        ok = self._can_enter(b)
        return status_dict(self, "ema_cross", "รอ EMA ตัดกัน" if ok else "นอกเวลาเทรด", prox if ok else 0.0,
                           "BUY" if up else "SELL",
                           f"EMA{self.cfg.fast}−EMA{self.cfg.slow} = {diff:+.2f}" + (" (ตัดไปแล้ว รอรอบใหม่)" if gap < 0 else ""))


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
    tp_r: float | None = None          # ปิดที่ min(เส้นกลาง, tp_r × R)
    be_at_r: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None      # ตั้งแล้วใช้ระยะนี้แทนเส้นกลาง
    plan: ManagementPlan | None = None


class BollingerFade(_ScalpBase):
    def __init__(self, cfg: BollingerFadeConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed,
                         cfg.be_at_r)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
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
        d = self.cfg.sl_price or self.cfg.sl_atr * atr
        tp_d = abs(mid - bar.close) if self.cfg.tp_r is None else min(abs(mid - bar.close), self.cfg.tp_r * d)
        if self.cfg.tp_price:
            tp_d = self.cfg.tp_price
        return self._signal(bar, rule, d, tp_d, f"BB({self.cfg.k}) fade")

    def status(self) -> dict:
        b, q = self.last_bar, self.bb._q
        if b is None or len(q) < q.maxlen:  # type: ignore[operator]
            return status_dict(self, "bb_fade", "รอข้อมูล")
        m = sum(q) / len(q)
        sd = math.sqrt(sum((x - m) ** 2 for x in q) / len(q)) or 1e-9
        z = (b.close - m) / sd
        ok = self._can_enter(b)
        w = self._window
        return status_dict(self, "bb_fade", "เฝ้าขอบ Bollinger" if ok else f"นอกช่วง {w.start_hour:02d}–{w.end_hour:02d} UTC",
                           clamp01(abs(z) / self.cfg.k) if ok else 0.0, "SELL" if z > 0 else "BUY",
                           f"ห่างเส้นกลาง {z:+.1f} SD (เข้าเมื่อเกิน ±{self.cfg.k})")
