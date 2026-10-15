"""
vol_expansion.py — H5/H6: ตลาดเงียบ → ระเบิด (Volatility Expansion Breakout)

ที่มา: ด่าน 0 (research/hypotheses.md) ยืนยันว่าระดับความผันผวน "คงอยู่และเปลี่ยนสภาพ" จริง
ความเชื่อของนักเทรด: ช่วงบีบตัวมักจบด้วยการวิ่งแรง ทางที่ทะลุกรอบคือทางที่วิ่ง

กฎ:
  1) มีแท่ง H1 ที่ regime = DEAD (ATR(24) percentile ≤ 10) ภายใน 24 แท่ง H1 ล่าสุด
  2) กรอบ = high/low ของ box_n แท่ง H1 ที่ปิดแล้วล่าสุด
  3) แท่ง M15 ปิดนอกกรอบ → เข้าตามทาง (วันละ 1 ไม้)
  4) SL = sl_atr × ATR(14, H1), TP = tp_r × R, time stop 24 ชม., ปิดก่อนสุดสัปดาห์
  5) ไม่เข้าช่วง rollover 21–23 UTC

พารามิเตอร์ (grid): box_n, sl_atr, tp_r — ค่าอื่นตายตัว
side_mode "random" / "long" = benchmark: จุดเข้า ระยะ SL/TP เหมือนเดิมทุกอย่าง ต่างแค่ทิศ
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from ..core.interfaces import IAccount
from ..core.regime import Regime, RegimeClassifier
from ..core.timeutil import SessionClock
from ..core.types import TIMEFRAME_MINUTES, Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, Strategy, clamp01, status_dict
from .levels import hold_exits, in_rollover


@dataclass(frozen=True)
class VolExpansionConfig:
    tag: str
    symbol: str
    box_n: int = 24
    sl_atr: float = 1.5
    tp_r: float = 2.0
    dead_lookback: int = 24
    max_hold_h: int = 24
    side_mode: str = "rule"
    seed: int = 7
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class VolExpansion(Strategy):
    def __init__(self, cfg: VolExpansionConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag, self.symbol, self.plan = cfg.tag, cfg.symbol, cfg.plan
        self._bar = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        self.clf = RegimeClassifier()
        self._box: deque[tuple[float, float]] = deque(maxlen=cfg.box_n)
        self._dead: deque[bool] = deque(maxlen=cfg.dead_lookback)
        self._utc = SessionClock("UTC")
        self._rand = random.Random(cfg.seed)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        st = self.clf.update(bar)
        if st is not None:
            self._box.append((st.h1.high, st.h1.low))
            self._dead.append(st.regime is Regime.DEAD)
        exits = hold_exits(self, bar, account, timedelta(hours=self.cfg.max_hold_h), self._bar)
        if exits:
            return exits
        close_t = bar.time + self._bar
        atr = self.clf.atr
        if (atr is None or len(self._box) < self.cfg.box_n or not any(self._dead) or in_rollover(close_t)
                or (close_t.weekday() == 4 and close_t.hour >= 18)):
            return []
        hi = max(h for h, _ in self._box)
        lo = min(l for _, l in self._box)
        if bar.close > hi:
            side = Side.BUY
        elif bar.close < lo:
            side = Side.SELL
        else:
            return []
        coin = self._rand.random()
        if self.cfg.side_mode == "long":
            side = Side.BUY
        elif self.cfg.side_mode == "random":
            side = Side.BUY if coin < 0.5 else Side.SELL
        d = self.cfg.sl_atr * atr
        sl = bar.close - int(side) * d
        tp = bar.close + int(side) * self.cfg.tp_r * d
        return [EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{close_t.date()}",
                            reason=f"break {'up' if bar.close > hi else 'down'} of {self.cfg.box_n}h box after DEAD")]

    def status(self) -> dict:
        b, st = self.last_bar, self.clf.state
        if b is None or st is None or st.vol_pct is None or len(self._box) < self.cfg.box_n:
            return status_dict(self, "vol_expansion", "รอข้อมูล (warmup ~100 วัน)")
        if not any(self._dead):
            return status_dict(self, "vol_expansion", "ตลาดยังไม่เงียบพอ", 0.0, None,
                               f"ความผันผวน percentile {st.vol_pct:.0%} (ต้อง ≤ 10% ภายใน 24 ชม.)")
        hi, lo = max(h for h, _ in self._box), min(lo_ for _, lo_ in self._box)
        up, dn = hi - b.close, b.close - lo
        side, dist = ("BUY", up) if up < dn else ("SELL", dn)
        return status_dict(self, "vol_expansion", "ตลาดเงียบ เฝ้าการระเบิด", 1 - clamp01(dist / max(hi - lo, 1e-9)),
                           side, f"กรอบ {self.cfg.box_n} ชม. {lo:.2f}–{hi:.2f}")
