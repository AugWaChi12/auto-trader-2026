"""
microstructure.py — 3 สายกลยุทธ์ใหม่ของศูนย์วิจัย (VWAP / Fair Value Gap / RSI Divergence)

  1) VwapReversion (vwap) : ราคาฉีกห่างจาก Session VWAP (ถ่วงน้ำหนักด้วย tick volume รายวัน) เกิน band_atr × ATR
                            → สวนกลับเข้าหาเส้นค่าเฉลี่ยสถาบัน (ออกที่ VWAP หรือ tp_r × R)
  2) FairValueGap  (fvg)  : เกิดช่องว่าง 3 แท่ง (Imbalance / FVG) ขนาดอย่างน้อย min_gap_atr × ATR ตามทิศ EMA200
                            → รอราคาย่อกลับมาแตะโซนช่องว่างภายใน expire_bars แท่ง แล้วเข้าตามเทรนด์
  3) RsiDivergence (div)  : ราคาทำ High/Low ใหม่ในรอบ lookback แท่ง แต่ RSI(14) ไม่ทำจุดสุดโต่งตาม (หมดแรง)
                            → เข้าสวนเมื่อแท่งปิดยืนยันกลับทิศ
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import NamedTuple

from ..core.interfaces import IAccount
from ..core.types import Bar, ExitSignal, Side, Signal
from .base import ManagementPlan, clamp01, status_dict
from .levels import hold_exits
from .scalp import RSI, ScalpWindow, _ScalpBase
from .trend_pullback import EMA


# ---------------------------------------------------------------------------- 1) Session VWAP Reversion
@dataclass(frozen=True)
class VwapReversionConfig:
    tag: str
    symbol: str
    band_atr: float = 2.0
    sl_atr: float = 1.5
    tp_r: float | None = 1.5
    max_hold: int = 24
    window: ScalpWindow = field(default_factory=lambda: ScalpWindow(0, 24))
    side_mode: str = "rule"
    seed: int = 7
    plan: ManagementPlan | None = None


class VwapReversion(_ScalpBase):
    def __init__(self, cfg: VwapReversionConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
        self._day: date | None = None
        self._pv = 0.0
        self._vol = 0.0
        self._bars_today = 0
        self.vwap: float | None = None

    def _update_vwap(self, bar: Bar) -> float:
        d = bar.time.date()
        if d != self._day:
            self._day, self._pv, self._vol, self._bars_today = d, 0.0, 0.0, 0
        tp = (bar.high + bar.low + bar.close) / 3.0
        v = max(bar.volume, 1.0)
        self._pv += tp * v
        self._vol += v
        self._bars_today += 1
        self.vwap = self._pv / self._vol
        return self.vwap

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        vwap, atr = self._update_vwap(bar), self.atr.update(bar)
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits:
            return exits
        pos = account.positions(self.symbol, self.tag)
        if pos:
            p = pos[0]
            if not self.plan.tp_usd and not self.plan.lock_tiers and (
                    (p.side is Side.BUY and bar.close >= vwap) or (p.side is Side.SELL and bar.close <= vwap)):
                return [ExitSignal(self.symbol, self.tag, "vwap")]
            return []
        if atr is None or self._bars_today < 6 or not self._can_enter(bar):
            return []
        dist = bar.close - vwap
        band = self.cfg.band_atr * atr
        if dist < -band:
            rule = Side.BUY
        elif dist > band:
            rule = Side.SELL
        else:
            return []
        sl_d = self.cfg.sl_atr * atr
        tp_d = abs(dist) if self.cfg.tp_r is None else min(abs(dist), self.cfg.tp_r * sl_d)
        return self._signal(bar, rule, sl_d, tp_d, f"VWAP({self.cfg.band_atr}ATR)")

    def status(self) -> dict:
        b, atr, vwap = self.last_bar, self.atr.value, self.vwap
        if b is None or atr is None or vwap is None or self._bars_today < 6:
            return status_dict(self, "vwap", "รอข้อมูล")
        z = (b.close - vwap) / (atr or 1e-9)
        ok = self._can_enter(b)
        return status_dict(self, "vwap", "เฝ้า VWAP" if ok else "นอกเวลาเทรด",
                           clamp01(abs(z) / self.cfg.band_atr) if ok else 0.0,
                           "SELL" if z > 0 else "BUY",
                           f"ห่าง VWAP {z:+.1f} ATR (เข้าเมื่อเกิน ±{self.cfg.band_atr})")


# ---------------------------------------------------------------------------- 2) Fair Value Gap (FVG Pullback)
class _Zone(NamedTuple):
    side: Side
    low: float
    high: float
    age: int


@dataclass(frozen=True)
class FairValueGapConfig:
    tag: str
    symbol: str
    min_gap_atr: float = 0.3
    expire_bars: int = 12
    sl_atr: float = 1.5
    tp_r: float = 2.0
    max_hold: int = 24
    window: ScalpWindow = field(default_factory=lambda: ScalpWindow(0, 24))
    side_mode: str = "rule"
    seed: int = 7
    plan: ManagementPlan | None = None


class FairValueGap(_ScalpBase):
    def __init__(self, cfg: FairValueGapConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
        self.ema200 = EMA(200)
        self._w: deque[Bar] = deque(maxlen=3)
        self._zone: _Zone | None = None

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        e200, atr = self.ema200.update(bar.close), self.atr.update(bar)
        self._w.append(bar)
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits or account.positions(self.symbol, self.tag):
            return exits
        if atr is None or not self.ema200.ready:
            return []
        # ตรวจว่าโซน FVG เดิมถูกย่อมาแตะหรือทะลุทิ้งแล้ว
        z = self._zone
        if z is not None:
            if z.age >= self.cfg.expire_bars or (z.side is Side.BUY and bar.close < z.low) or (z.side is Side.SELL and bar.close > z.high):
                self._zone = None
            elif self._can_enter(bar):
                if z.side is Side.BUY and bar.low <= z.high and bar.close >= z.low and bar.close > e200:
                    self._zone = None
                    d = self.cfg.sl_atr * atr
                    return self._signal(bar, Side.BUY, d, self.cfg.tp_r * d, "FVG pullback BUY")
                if z.side is Side.SELL and bar.high >= z.low and bar.close <= z.high and bar.close < e200:
                    self._zone = None
                    d = self.cfg.sl_atr * atr
                    return self._signal(bar, Side.SELL, d, self.cfg.tp_r * d, "FVG pullback SELL")
            if self._zone is not None:
                self._zone = _Zone(z.side, z.low, z.high, z.age + 1)
        # ตรวจเกิด FVG ใหม่จาก 3 แท่งล่าสุด
        if len(self._w) == 3:
            b0, _, b2 = self._w[0], self._w[1], self._w[2]
            min_gap = self.cfg.min_gap_atr * atr
            if b2.low - b0.high >= min_gap and b2.close > e200:
                self._zone = _Zone(Side.BUY, b0.high, b2.low, 0)
            elif b0.low - b2.high >= min_gap and b2.close < e200:
                self._zone = _Zone(Side.SELL, b2.high, b0.low, 0)
        return []

    def status(self) -> dict:
        b, atr = self.last_bar, self.atr.value
        if b is None or atr is None or not self.ema200.ready:
            return status_dict(self, "fvg", "รอข้อมูล")
        z = self._zone
        if z is None:
            return status_dict(self, "fvg", "รอเกิด FVG", 0.1, "BUY" if b.close > self.ema200.value else "SELL",
                               "ยังไม่มีช่องว่าง FVG ที่เปิดอยู่")
        mid = (z.low + z.high) / 2
        prox = 1.0 - clamp01(abs(b.close - mid) / (2.0 * atr))
        return status_dict(self, "fvg", "รอย่อแตะ FVG", prox, z.side.name,
                           f"โซน {z.low:.2f}–{z.high:.2f} (อายุ {z.age}/{self.cfg.expire_bars} แท่ง)")


# ---------------------------------------------------------------------------- 3) RSI Divergence
@dataclass(frozen=True)
class RsiDivergenceConfig:
    tag: str
    symbol: str
    lookback: int = 20
    rsi_ext: float = 35.0      # ขาขึ้นต้องเคยลงต่ำกว่า rsi_ext · ขาลงต้องเคยขึ้นเกิน 100 - rsi_ext
    sl_atr: float = 1.5
    tp_r: float = 2.0
    max_hold: int = 24
    window: ScalpWindow = field(default_factory=lambda: ScalpWindow(0, 24))
    side_mode: str = "rule"
    seed: int = 7
    plan: ManagementPlan | None = None


class RsiDivergence(_ScalpBase):
    def __init__(self, cfg: RsiDivergenceConfig, timeframe: str) -> None:
        super().__init__(cfg.tag, cfg.symbol, timeframe, cfg.window, cfg.max_hold, cfg.side_mode, cfg.seed)
        self.cfg = cfg
        if cfg.plan is not None:
            self.plan = cfg.plan
        self.rsi = RSI(14)
        self._hist: deque[tuple[float, float, float]] = deque(maxlen=cfg.lookback + 2)   # (low, high, rsi)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        r, atr = self.rsi.update(bar.close), self.atr.update(bar)
        exits = hold_exits(self, bar, account, self._max_hold, self._bar)
        if exits or account.positions(self.symbol, self.tag):
            if r is not None:
                self._hist.append((bar.low, bar.high, r))
            return exits
        if r is None or atr is None:
            return []
        prev = list(self._hist)
        self._hist.append((bar.low, bar.high, r))
        if len(prev) < self.cfg.lookback or not self._can_enter(bar):
            return []
        window = prev[:-2]               # เทียบกับจุดสุดโต่งก่อนหน้า (เว้น 2 แท่งติดกัน)
        if not window:
            return []
        min_low, _, min_r = min(window, key=lambda x: x[0])
        _, max_high, max_r = max(window, key=lambda x: x[1])
        # Bullish divergence: ราคาทำ Low ใหม่ต่ำกว่าเดิม แต่ RSI ยกสูงขึ้น (> min_r + 3) และแท่งปิดเขียว
        if bar.low < min_low and min_r < self.cfg.rsi_ext and r >= min_r + 3.0 and bar.close > bar.open:
            d = self.cfg.sl_atr * atr
            return self._signal(bar, Side.BUY, d, self.cfg.tp_r * d, f"Bull div RSI {min_r:.0f}->{r:.0f}")
        # Bearish divergence: ราคาทำ High ใหม่สูงกว่าเดิม แต่ RSI ลดต่ำลง (< max_r - 3) และแท่งปิดแดง
        if bar.high > max_high and max_r > 100.0 - self.cfg.rsi_ext and r <= max_r - 3.0 and bar.close < bar.open:
            d = self.cfg.sl_atr * atr
            return self._signal(bar, Side.SELL, d, self.cfg.tp_r * d, f"Bear div RSI {max_r:.0f}->{r:.0f}")
        return []

    def status(self) -> dict:
        b, r = self.last_bar, self.rsi.value
        if b is None or r is None or len(self._hist) < self.cfg.lookback:
            return status_dict(self, "div", "รอข้อมูล")
        prox = clamp01( abs(r - 50.0) / (50.0 - self.cfg.rsi_ext / 2) )
        return status_dict(self, "div", "เฝ้า Divergence", prox, "BUY" if r < 50 else "SELL",
                           f"RSI(14) = {r:.0f} (รอบ {self.cfg.lookback} แท่ง)")
