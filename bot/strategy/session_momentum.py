"""
session_momentum.py — ตามทิศของช่วงเช้า (Session Momentum)

ที่มา (research/explore.py, in-sample 2023-10 → 2025-06, XAUUSD):
  ทิศของ เอเชีย+ลอนดอน (00–12 UTC) บอกทิศของ นิวยอร์ก+ปลายวัน (12–21 UTC) ได้
  ทั้งเช้าขึ้น (+0.084%) และเช้าลง (+0.062%) — ไม่ใช่แค่ drift ขาขึ้น (ซื้อเฉยๆ ได้ +0.024%)
  เช้าที่วิ่งแรง (tercile บน) ได้ผลชัดที่สุด
  ข้อควรระวัง: ครึ่งแรกของ in-sample ผลอ่อนมาก (t=0.54)

กฎ:
  1) วัดการเคลื่อนที่ตั้งแต่ราคาเปิดของ measure_start ถึงราคาปิดแท่งแรกหลัง entry_time
  2) |move| >= min_move_adr * ADR → เข้าตามทิศ
  3) SL = sl_adr * ADR จากราคาเข้า, ไม่มี TP — ถือจนถึง flat_time (เหมือนที่วัดใน research)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..core.interfaces import IAccount
from ..core.timeutil import minutes_of_day, parse_hhmm
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, flat_signals


@dataclass(frozen=True)
class MomentumConfig:
    tag: str
    symbol: str
    tz: str = "UTC"
    measure_start: str = "00:00"
    entry_time: str = "12:00"
    flat_time: str = "20:00"
    min_move_adr: float = 0.3
    sl_adr: float = 0.5
    adr_lookback: int = 10
    side_mode: str = "follow"   # "follow" = กลยุทธ์จริง | "long" / "random" = benchmark ที่กลไกเหมือนกันทุกอย่าง
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class SessionMomentum(Strategy):
    def __init__(self, cfg: MomentumConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        entry = minutes_of_day(parse_hhmm(cfg.entry_time))
        window_end = f"{(entry + 30) // 60:02d}:{(entry + 30) % 60:02d}"   # ให้ลองได้ 2 แท่งถ้าแท่งแรกถูกปฏิเสธ
        self._rand = random.Random(7)
        self.rng = SessionRange(SessionWindow(cfg.tz, cfg.measure_start, cfg.entry_time, window_end, cfg.flat_time),
                                timeframe, cfg.adr_lookback)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        rng = self.rng
        rng.update(bar)
        exits = flat_signals(self, rng, account)
        if exits or not rng.in_trade_window or not rng.complete or rng.range_open is None:
            return exits
        adr = rng.daily.adr
        if adr is None:
            return []
        move = bar.close - rng.range_open
        if abs(move) < self.cfg.min_move_adr * adr or move == 0:
            return []
        side = Side.BUY if move > 0 else Side.SELL
        if self.cfg.side_mode == "long":
            side = Side.BUY
        elif self.cfg.side_mode == "random":
            side = Side.BUY if self._rand.random() < 0.5 else Side.SELL
        sl = bar.close - int(side) * self.cfg.sl_adr * adr
        return [EnterSignal(self.symbol, side, sl, None, self.tag, key=f"{self.tag}:{rng.day}",
                            reason=f"morning move {move:+.2f} = {move / adr:+.2f} ADR")]
