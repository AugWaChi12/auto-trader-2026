"""
daily_trend.py — T1: Trend following รายวัน (Donchian breakout แบบ Turtle)

ที่มา: time-series momentum (Moskowitz–Ooi–Pedersen 2012), ระบบ Turtle (Donchian 20/55 วัน)
หลักฐานวิชาการแข็งที่สุดในบรรดาเทคนิคทั้งหมดที่เราทดสอบ — แต่ผลดีมาจาก "หลายตลาดพร้อมกัน"

กฎ (ต่อหนึ่งตลาด):
  1) ราคาปิดสูงกว่า high สูงสุดของ entry_n วันก่อนหน้า → BUY (ต่ำกว่า low ต่ำสุด → SELL)
  2) SL เริ่มต้น = stop_atr × ATR(20) — ขนาดไม้ = % ความเสี่ยงคงที่ → ตลาดผันผวนมากได้ไม้เล็ก (vol targeting)
  3) ออกเมื่อราคาปิดหลุด low ต่ำสุดของ exit_n วัน (ฝั่ง SELL: ทะลุ high สูงสุด) หรือโดน SL
  4) ไม่มี TP — ปล่อยกำไรวิ่ง (กำไรมาจากไม้ใหญ่ไม่กี่ไม้)
พารามิเตอร์ (grid): entry_n, stop_atr · exit_n = entry_n / 2 (ตาย ตัวตาม Turtle)
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from ..core.indicators import ATR
from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, ExitSignal, Side, Signal
from .base import ManagementPlan, Strategy, clamp01, status_dict


@dataclass(frozen=True)
class DailyTrendConfig:
    tag: str
    symbol: str
    entry_n: int = 50
    exit_n: int | None = None         # None = entry_n // 2
    stop_atr: float = 3.0
    side_mode: str = "rule"           # "random" / "long" = benchmark
    seed: int = 7
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=None, trail_atr_mult=None))


class DailyTrend(Strategy):
    def __init__(self, cfg: DailyTrendConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag, self.symbol, self.plan = cfg.tag, cfg.symbol, cfg.plan
        self.exit_n = cfg.exit_n or max(cfg.entry_n // 2, 2)
        n = max(cfg.entry_n, self.exit_n)
        self._hist: deque[Bar] = deque(maxlen=n)      # แท่งก่อนหน้า (ไม่รวมแท่งปัจจุบัน)
        self.atr = ATR(20)
        self._rand = random.Random(cfg.seed)

    def _channel(self, n: int) -> tuple[float, float] | None:
        if len(self._hist) < n:
            return None
        recent = list(self._hist)[-n:]
        return max(b.high for b in recent), min(b.low for b in recent)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        atr = self.atr.update(bar)
        entry_ch, exit_ch = self._channel(self.cfg.entry_n), self._channel(self.exit_n)
        self._hist.append(bar)
        if atr is None or entry_ch is None or exit_ch is None:
            return []

        pos = account.positions(self.symbol, self.tag)
        if pos:
            p = pos[0]
            ex_hi, ex_lo = exit_ch
            if (p.side is Side.BUY and bar.close < ex_lo) or (p.side is Side.SELL and bar.close > ex_hi):
                return [ExitSignal(self.symbol, self.tag, "channel_exit")]
            return []

        hi, lo = entry_ch
        if bar.close > hi:
            rule = Side.BUY
        elif bar.close < lo:
            rule = Side.SELL
        else:
            return []
        coin = self._rand.random()
        side = Side.BUY if self.cfg.side_mode == "long" else (
            (Side.BUY if coin < 0.5 else Side.SELL) if self.cfg.side_mode == "random" else rule)
        d = self.cfg.stop_atr * atr
        return [EnterSignal(self.symbol, side, bar.close - int(side) * d, None, self.tag,
                            key=f"{self.tag}:{bar.time:%Y%m%d%H%M}",
                            reason=f"Donchian {self.cfg.entry_n}d breakout {'up' if rule is Side.BUY else 'down'}")]

    def status(self) -> dict:
        b, ch, atr = self.last_bar, self._channel(self.cfg.entry_n), self.atr.value
        if b is None or ch is None or atr is None:
            return status_dict(self, "daily_trend", f"รอข้อมูล {self.cfg.entry_n} วัน")
        hi, lo = ch
        up, dn = hi - b.close, b.close - lo
        side, dist = ("BUY", up) if up < dn else ("SELL", dn)
        return status_dict(self, "daily_trend", f"เฝ้าทะลุกรอบ {self.cfg.entry_n} วัน",
                           1 - clamp01(dist / max(hi - lo, 1e-9) * 2), side,
                           f"กรอบ {lo:.2f}–{hi:.2f} · ห่าง {dist:.2f} ({dist / atr:.1f} ATR)")
