"""
session_breakout.py — Session Range Breakout

ใช้ได้ทั้ง:
  - Asian Range Breakout ของทอง  : กรอบ 00:00–07:00 UTC, เข้าตอนลอนดอน/นิวยอร์ก
  - Opening Range Breakout NAS100 : กรอบ 09:30–10:00 นิวยอร์ก, เข้าก่อนเที่ยง

กฎ:
  1) เก็บ high/low ของกรอบ
  2) วัดความกว้างเทียบ ADR — แคบเกิน = มักหลอก, กว้างเกิน = วิ่งไปแล้ว → ข้ามวัน
  3) แท่งปิดทะลุ high + buffer → BUY / ทะลุ low - buffer → SELL
  4) SL: "mid" = กลางกรอบ, "opposite" = อีกฝั่งของกรอบ
  5) TP: tp_r * R หรือ None (ปล่อยวิ่งด้วย trailing ตาม ManagementPlan)
  6) วันละ 1 สัญญาณ (key ไม่มีทิศ) → โดน SL แล้วจบวัน ไม่กลับข้างไล่
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, clamp01, flat_signals, status_dict


@dataclass(frozen=True)
class BreakoutConfig:
    tag: str
    symbol: str
    window: SessionWindow
    min_range_adr: float = 0.15
    max_range_adr: float = 0.60
    buffer_adr: float = 0.02       # ต้องปิดพ้นกรอบอย่างน้อยเท่านี้ (สัดส่วน ADR)
    sl_mode: str = "mid"           # "mid" | "opposite"
    tp_r: float | None = None
    adr_lookback: int = 10
    plan: ManagementPlan = field(default_factory=ManagementPlan)


class SessionBreakout(Strategy):
    def __init__(self, cfg: BreakoutConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self.rng = SessionRange(cfg.window, timeframe, cfg.adr_lookback)

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        rng = self.rng
        rng.update(bar)

        exits = flat_signals(self, rng, account)
        if exits or not rng.in_trade_window:
            return exits

        ratio = rng.width_ratio()
        if ratio is None or not (self.cfg.min_range_adr <= ratio <= self.cfg.max_range_adr):
            return []

        adr = rng.daily.adr
        assert adr is not None
        buf = self.cfg.buffer_adr * adr
        mid = (rng.high + rng.low) / 2

        if bar.close > rng.high + buf:
            side = Side.BUY
            sl = mid if self.cfg.sl_mode == "mid" else rng.low - buf
        elif bar.close < rng.low - buf:
            side = Side.SELL
            sl = mid if self.cfg.sl_mode == "mid" else rng.high + buf
        else:
            return []

        tp = None
        if self.cfg.tp_r is not None:
            tp = bar.close + int(side) * self.cfg.tp_r * abs(bar.close - sl)

        return [EnterSignal(
            symbol=self.symbol, side=side, sl=sl, tp=tp, tag=self.tag,
            key=f"{self.tag}:{rng.day}",
            reason=f"close {bar.close:.2f} beyond range [{rng.low:.2f}, {rng.high:.2f}] width={ratio:.2f}ADR",
        )]

    def status(self) -> dict:
        b, rng, c = self.last_bar, self.rng, self.cfg
        w = c.window
        if b is None:
            return status_dict(self, "breakout", "รอข้อมูล")
        if not rng.complete:
            return status_dict(self, "breakout", f"กำลังสร้างกรอบ {w.range_start}–{w.range_end} ({w.tz})")
        if not rng.in_trade_window:
            return status_dict(self, "breakout", f"นอกช่วงเข้า ({w.range_end}–{w.trade_end} {w.tz})")
        ratio, adr = rng.width_ratio(), rng.daily.adr
        if ratio is None or adr is None or not (c.min_range_adr <= ratio <= c.max_range_adr):
            return status_dict(self, "breakout", "กรอบวันนี้ไม่เข้าเกณฑ์" + (f" ({ratio:.2f} ADR)" if ratio else ""))
        buf = c.buffer_adr * adr
        up, dn = rng.high + buf - b.close, b.close - (rng.low - buf)
        side, dist = ("BUY", up) if up < dn else ("SELL", dn)
        return status_dict(self, "breakout", "เฝ้าขอบกรอบ", 1 - clamp01(dist / max(rng.width, 1e-9)), side,
                           f"กรอบ {rng.low:.2f}–{rng.high:.2f} · ห่างจุดทะลุ {max(dist, 0):.2f}")
