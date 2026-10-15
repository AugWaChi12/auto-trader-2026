"""
level_sweep.py — H7/H8: กวาด stop ที่ระดับสำคัญ (Liquidity Sweep at Key Levels)

ความเชื่อของนักเทรด: stop ของคนส่วนใหญ่กองอยู่เลย high/low ของเมื่อวานและสัปดาห์ก่อน
ราคามักถูกลากไปเก็บ stop แล้วกลับ — ต่างจาก sweep รอบแรก (research/RESULTS.md) ที่ใช้แค่ขอบกรอบเอเชีย

กฎ (ฝั่งบน — ฝั่งล่างกลับกัน):
  1) regime ไม่ใช่ HIGH_VOL (ตลาดตื่นตระหนก ระดับไม่ศักดิ์สิทธิ์)
  2) high ของแท่ง M15 เลยระดับ (PDH / PWH) อย่างน้อย 0.05 ATR(H1) → เริ่มนับ sweep
  3) ภายใน max_bars แท่ง มีแท่งปิดกลับใต้ระดับ → SELL
     ถ้าอยู่นอกระดับนานกว่านั้น = ทะลุจริง → ระดับนั้นใช้ไม่ได้ทั้งวัน
  4) SL = จุดสุดของไส้ + buffer_atr × ATR(H1), TP = tp_r × R, time stop 24 ชม., ปิดก่อนสุดสัปดาห์
  5) ไม่เข้าช่วง rollover 21–23 UTC, ระดับละ 1 ไม้ต่อวัน

พารามิเตอร์ (grid): max_bars, buffer_atr, tp_r
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.interfaces import IAccount
from ..core.regime import Regime, RegimeClassifier
from ..core.types import TIMEFRAME_MINUTES, Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, Strategy, clamp01, status_dict
from .levels import KeyLevels, hold_exits, in_rollover


@dataclass(frozen=True)
class LevelSweepConfig:
    tag: str
    symbol: str
    tz: str = "UTC"
    max_bars: int = 2
    buffer_atr: float = 0.25
    tp_r: float = 2.0
    min_sweep_atr: float = 0.05
    max_hold_h: int = 24
    side_mode: str = "rule"
    seed: int = 7
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


@dataclass
class _Sweep:
    extreme: float
    bars: int = 0


class LevelSweep(Strategy):
    def __init__(self, cfg: LevelSweepConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag, self.symbol, self.plan = cfg.tag, cfg.symbol, cfg.plan
        self._bar = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        self.clf = RegimeClassifier()
        self.levels = KeyLevels(cfg.tz)
        self._day: date | None = None
        self._active: dict[str, _Sweep] = {}
        self._done: set[str] = set()         # ระดับที่ใช้ไปแล้ว/ทะลุจริงแล้ววันนี้
        self._rand = random.Random(cfg.seed)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        self.clf.update(bar)
        self.levels.update(bar)
        if self.levels.day != self._day:
            self._day, self._active, self._done = self.levels.day, {}, set()

        exits = hold_exits(self, bar, account, timedelta(hours=self.cfg.max_hold_h), self._bar)
        if exits:
            return exits
        st, atr = self.clf.state, self.clf.atr
        close_t = bar.time + self._bar
        if (st is None or atr is None or st.regime in (Regime.HIGH_VOL, Regime.UNKNOWN) or in_rollover(close_t)
                or (close_t.weekday() == 4 and close_t.hour >= 18)):
            return []

        for name, level in self.levels.upper().items():
            sig = self._check(name, level, bar, atr, Side.SELL)
            if sig:
                return [sig]
        for name, level in self.levels.lower().items():
            sig = self._check(name, level, bar, atr, Side.BUY)
            if sig:
                return [sig]
        return []

    def _check(self, name: str, level: float, bar: Bar, atr: float, side: Side) -> EnterSignal | None:
        if name in self._done:
            return None
        upper = side is Side.SELL
        beyond = (bar.high - level) if upper else (level - bar.low)
        extreme = bar.high if upper else bar.low
        sw = self._active.get(name)
        if beyond >= self.cfg.min_sweep_atr * atr:
            if sw is None:
                sw = self._active[name] = _Sweep(extreme)
            sw.extreme = max(sw.extreme, extreme) if upper else min(sw.extreme, extreme)
        if sw is None:
            return None
        sw.bars += 1
        back_inside = bar.close < level if upper else bar.close > level
        if not back_inside:
            if sw.bars >= self.cfg.max_bars:
                self._done.add(name)          # ทะลุจริง
                del self._active[name]
            return None
        self._done.add(name)
        del self._active[name]
        d = abs(sw.extreme - bar.close) + self.cfg.buffer_atr * atr
        coin = self._rand.random()
        if self.cfg.side_mode == "long":
            side = Side.BUY
        elif self.cfg.side_mode == "random":
            side = Side.BUY if coin < 0.5 else Side.SELL
        sl = bar.close - int(side) * d
        tp = bar.close + int(side) * self.cfg.tp_r * d
        return EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{self._day}:{name}",
                           reason=f"sweep {name} {level:.2f} to {sw.extreme:.2f}, closed back {bar.close:.2f}")

    def status(self) -> dict:
        b, st, atr = self.last_bar, self.clf.state, self.clf.atr
        if b is None or st is None or atr is None:
            return status_dict(self, "level_sweep", "รอข้อมูล (warmup ~100 วัน)")
        if st.regime in (Regime.HIGH_VOL, Regime.UNKNOWN):
            return status_dict(self, "level_sweep", "ผันผวนสูง พักกลยุทธ์", 0.0, None, f"regime {st.regime.value}")
        for name, sw in self._active.items():
            side = "SELL" if name in ("PDH", "PWH") else "BUY"
            return status_dict(self, "level_sweep", f"กวาด {name} แล้ว รอปิดกลับ", 0.85, side, f"ไส้ถึง {sw.extreme:.2f}")
        cands = [(abs(lv - b.close), n, lv, "SELL") for n, lv in self.levels.upper().items() if n not in self._done]
        cands += [(abs(b.close - lv), n, lv, "BUY") for n, lv in self.levels.lower().items() if n not in self._done]
        if not cands:
            return status_dict(self, "level_sweep", "ระดับวันนี้ใช้ไปหมดแล้ว")
        dist, name, lv, side = min(cands)
        return status_dict(self, "level_sweep", f"เฝ้า {name}", 0.8 * (1 - clamp01(dist / (2 * atr))), side,
                           f"{name} {lv:.2f} ห่าง {dist:.2f} ({dist / atr:.1f} ATR)")
