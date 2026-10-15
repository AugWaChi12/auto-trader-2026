"""
random_entry.py — กลยุทธ์ "ไม่มีสมอง" ไว้เป็น benchmark

ถ้ากลยุทธ์จริงชนะ RandomEntry ที่ใช้ช่วงเวลา, SL/TP และความถี่ใกล้เคียงกันไม่ได้
แปลว่า "edge" ที่เห็นมาจากทิศของตลาด (เช่น ทองขาขึ้น) ไม่ใช่จากกฎของกลยุทธ์

side_mode:
  "long"   : ซื้ออย่างเดียว — วัดว่าตลาดขาขึ้นแจกกำไรให้เท่าไร
  "random" : สุ่มทิศ — ควรได้ ≈ 0R ลบต้นทุน
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..core.indicators import ATR
from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, flat_signals, status_dict


@dataclass(frozen=True)
class RandomConfig:
    tag: str
    symbol: str
    window: SessionWindow
    prob_per_bar: float = 0.05        # โอกาสเข้าในแต่ละแท่งของช่วงเทรด
    side_mode: str = "random"
    sl_atr: float = 1.5
    tp_r: float | None = 2.0
    seed: int = 7
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class RandomEntry(Strategy):
    def __init__(self, cfg: RandomConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self.rng = SessionRange(cfg.window, timeframe)
        self._atr = ATR(14)
        self._rand = random.Random(cfg.seed)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        atr = self._atr.update(bar)
        self.rng.update(bar)
        exits = flat_signals(self, self.rng, account)
        # สุ่มทุกแท่งเสมอ (แม้นอกช่วง) เพื่อให้ลำดับสุ่มไม่ขึ้นกับสถานะอื่น → ผลทำซ้ำได้
        roll, coin = self._rand.random(), self._rand.random()
        if exits or not self.rng.in_trade_window or atr is None or roll >= self.cfg.prob_per_bar:
            return exits
        side = Side.BUY if self.cfg.side_mode == "long" or coin < 0.5 else Side.SELL
        risk = self.cfg.sl_atr * atr
        sl = bar.close - int(side) * risk
        tp = None if self.cfg.tp_r is None else bar.close + int(side) * self.cfg.tp_r * risk
        return [EnterSignal(self.symbol, side, sl, tp, self.tag, key=f"{self.tag}:{self.rng.day}")]

    def status(self) -> dict:
        mode = "ซื้ออย่างเดียว" if self.cfg.side_mode == "long" else "สุ่มทิศ"
        state = "กำลังสุ่มจังหวะเข้า" if self.rng.in_trade_window else "นอกช่วงเวลา"
        return status_dict(self, "benchmark", f"กลุ่มควบคุม ({mode})", 0.0, None,
                           f"{state} · วันละไม่เกิน 1 ไม้ · SL {self.cfg.sl_atr} ATR / TP {self.cfg.tp_r}R")
