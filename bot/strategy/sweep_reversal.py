"""
sweep_reversal.py — Liquidity Sweep Reversal (ล่าคนโดน stop)

แนวคิด: stop ของคนเทรด breakout กองอยู่เลยขอบกรอบ ราคามักถูกลากไปกินแล้วกลับตัว
ซึ่งเป็นด้านตรงข้ามของ SessionBreakout บนกรอบชุดเดียวกัน — backtest จะบอกว่าตัวไหนเวิร์ก

กฎ (ฝั่งบน — ฝั่งล่างกลับกัน):
  1) ในช่วงเทรด แท่งใด high > range_high + min_sweep → เริ่ม "sweep" จดจุดสุดของไส้
  2) ภายใน max_bars_outside แท่ง ถ้ามีแท่ง "ปิดกลับเข้ากรอบ" (close < range_high) → SELL
     (แท่งเดียวที่ทั้งเจาะและปิดกลับ = ไส้ยาว ก็นับ)
  3) SL = จุดสุดของไส้ + sl_buffer
  4) TP = อีกฝั่งของกรอบ (range_low) — ถ้า R:R ต่ำกว่า min_rr ไม่เข้า
  5) ถ้าอยู่นอกกรอบนานเกิน max_bars_outside = เป็น breakout จริง → ปิดฝั่งนั้นทั้งวัน
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.interfaces import IAccount
from ..core.types import Bar, EnterSignal, Side, Signal
from .base import ManagementPlan, SessionRange, SessionWindow, Strategy, clamp01, flat_signals, status_dict


@dataclass(frozen=True)
class SweepConfig:
    tag: str
    symbol: str
    window: SessionWindow
    min_range_adr: float = 0.15
    max_range_adr: float = 0.70
    min_sweep_adr: float = 0.02      # ต้องเจาะเลยขอบอย่างน้อยเท่านี้ถึงนับว่า sweep
    sl_buffer_adr: float = 0.03
    max_bars_outside: int = 3
    min_rr: float = 2.0
    adr_lookback: int = 10
    plan: ManagementPlan = field(default_factory=lambda: ManagementPlan(partial_r=1.5, trail_atr_mult=None))


@dataclass
class _SweepState:
    active: bool = False
    extreme: float = 0.0
    bars: int = 0
    dead: bool = False     # วันนี้ฝั่งนี้กลายเป็น breakout จริงแล้ว


class SweepReversal(Strategy):
    def __init__(self, cfg: SweepConfig, timeframe: str) -> None:
        self.cfg = cfg
        self.tag = cfg.tag
        self.symbol = cfg.symbol
        self.plan = cfg.plan
        self.rng = SessionRange(cfg.window, timeframe, cfg.adr_lookback)
        self._up = _SweepState()
        self._down = _SweepState()

    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        rng = self.rng
        rng.update(bar)
        if rng.new_day:
            self._up, self._down = _SweepState(), _SweepState()

        exits = flat_signals(self, rng, account)
        if exits or not rng.in_trade_window:
            return exits

        ratio = rng.width_ratio()
        if ratio is None or not (self.cfg.min_range_adr <= ratio <= self.cfg.max_range_adr):
            return []
        adr = rng.daily.adr
        assert adr is not None

        sig = self._check_side(bar, Side.SELL, self._up, adr)
        if sig is None:
            sig = self._check_side(bar, Side.BUY, self._down, adr)
        return [sig] if sig else []

    def _check_side(self, bar: Bar, side: Side, st: _SweepState, adr: float) -> EnterSignal | None:
        rng, cfg = self.rng, self.cfg
        # side = ทิศที่จะเข้า: SELL หลัง sweep ขอบบน, BUY หลัง sweep ขอบล่าง
        if st.dead:
            return None
        edge = rng.high if side is Side.SELL else rng.low
        opposite = rng.low if side is Side.SELL else rng.high
        beyond = (bar.high - edge) if side is Side.SELL else (edge - bar.low)
        extreme = bar.high if side is Side.SELL else bar.low

        if beyond >= cfg.min_sweep_adr * adr:
            if not st.active:
                st.active, st.extreme, st.bars = True, extreme, 0
            st.extreme = max(st.extreme, extreme) if side is Side.SELL else min(st.extreme, extreme)
        if not st.active:
            return None

        st.bars += 1
        closed_inside = bar.close < edge if side is Side.SELL else bar.close > edge
        if not closed_inside:
            if st.bars >= cfg.max_bars_outside:
                st.dead = True
            return None

        st.active = False  # sweep นี้ถูกใช้แล้ว (ถ้าไม่ได้เข้า ก็รอ sweep ครั้งใหม่)
        sl = st.extreme + (cfg.sl_buffer_adr * adr) * (1 if side is Side.SELL else -1)
        risk = abs(bar.close - sl)
        reward = abs(bar.close - opposite)
        if risk <= 0 or reward / risk < cfg.min_rr:
            return None
        return EnterSignal(
            symbol=self.symbol, side=side, sl=sl, tp=opposite, tag=self.tag,
            key=f"{self.tag}:{rng.day}",
            reason=f"sweep {'high' if side is Side.SELL else 'low'} to {st.extreme:.2f}, "
                   f"closed back {bar.close:.2f}, rr={reward / risk:.1f}",
        )

    def status(self) -> dict:
        b, rng, c = self.last_bar, self.rng, self.cfg
        if b is None or not rng.complete:
            return status_dict(self, "sweep", "กำลังสร้างกรอบ")
        if not rng.in_trade_window:
            return status_dict(self, "sweep", "นอกช่วงเข้า")
        if self._up.active:
            return status_dict(self, "sweep", "กวาด stop ขอบบนแล้ว รอปิดกลับ", 0.85, "SELL", f"ไส้ถึง {self._up.extreme:.2f}")
        if self._down.active:
            return status_dict(self, "sweep", "กวาด stop ขอบล่างแล้ว รอปิดกลับ", 0.85, "BUY", f"ไส้ถึง {self._down.extreme:.2f}")
        up, dn = rng.high - b.close, b.close - rng.low
        side, dist = ("SELL", up) if up < dn else ("BUY", dn)
        return status_dict(self, "sweep", "เฝ้าขอบกรอบ", 0.7 * (1 - clamp01(dist / max(rng.width / 2, 1e-9))), side,
                           f"กรอบ {rng.low:.2f}–{rng.high:.2f}")
