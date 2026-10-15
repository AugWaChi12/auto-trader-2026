"""
risk_manager.py — ด่านสุดท้ายก่อนเงินจริงออกจากบัญชี

เทียบกับ PUBG:  maxAmmo / InventoryFull  →  ขนาดไม้ / margin / เพดานขาดทุน

หน้าที่:
  1) คำนวณขนาดไม้จาก % ความเสี่ยง:
         เงินที่ยอมเสีย  = equity * risk_per_trade
         เสียต่อ 1 lot  = |entry - SL| * contract_size + slippage + ค่าคอม
         lot           = ปัดลง(เงินที่ยอมเสีย / เสียต่อ 1 lot)
     ถ้าได้ต่ำกว่า volume_min → ไม่เข้า (ห้ามปัดขึ้น เพราะจะเสี่ยงเกินที่ตั้งไว้)
  2) เพดานรายวัน: equity ต่ำกว่าต้นวัน * (1 - daily_loss_limit) → หยุดทั้งวัน (+ ปิดไม้ ถ้าตั้งไว้)
  3) Drawdown จากจุดสูงสุด เกิน max_drawdown → kill switch หยุดถาวรจนคนมาปลด
  4) จำกัดจำนวนกลุ่มไม้ที่เปิดพร้อมกัน, ช่วงงดเทรดรอบข่าว
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..core.interfaces import IAccount
from ..core.types import EnterSignal, SymbolSpec
from .news import NewsCalendar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01
    daily_loss_limit: float = 0.03
    max_drawdown: float = 0.15
    max_open_groups: int = 4
    max_groups_per_symbol: int = 2
    close_on_daily_halt: bool = True
    day_tz: str = "UTC"            # วันเริ่มนับใหม่ตาม tz นี้ (prop firm หลายเจ้าใช้ Europe/Prague)
    max_margin_use: float = 0.5    # ห้ามใช้ margin เกินสัดส่วนนี้ของ free margin ต่อไม้
    # ถ้าขนาดที่คำนวณได้ต่ำกว่า lot ขั้นต่ำ: ยอมเปิด lot ขั้นต่ำ ถ้าความเสี่ยงจริงไม่เกินค่านี้ (0 = ไม่ยอม)
    # มีไว้สำหรับ demo ทุนเล็กที่ต้องการเก็บข้อมูลผลเป็น R — ไม่ควรใช้กับเงินจริง
    min_lot_max_risk: float = 0.0
    # ถ้า equity จริงของบัญชีต่ำกว่านี้ → ไม่เปิดไม้ใหม่ (0 = ปิด) — กันบอททดสอบผลาญบัญชีจนบอทหลักเทรดไม่ได้
    account_floor: float = 0.0


@dataclass(frozen=True)
class SizeDecision:
    volume: float
    reason: str = ""        # ว่าง = ผ่าน

    @property
    def ok(self) -> bool:
        return self.volume > 0


class RiskManager:
    def __init__(self, cfg: RiskConfig, account: IAccount, news: NewsCalendar | None = None,
                 real_equity: Callable[[], float] | None = None) -> None:
        self.cfg = cfg
        self.account = account
        self.real_equity = real_equity      # equity จริงของบัญชี (live) — account อาจเป็น sub-account เสมือน
        self.news = news
        self._tz = ZoneInfo(cfg.day_tz)
        self._day: date | None = None
        self.day_start_equity = 0.0
        self.peak_equity = 0.0
        self.halted_today = False
        self.killed = False
        self.kill_reason = ""

    # ------------------------------------------------------------------ เวลา/เพดาน
    def on_time(self, now: datetime) -> list[str]:
        """อัปเดตสถานะทุกแท่ง คืนรายการเหตุการณ์ใหม่ ("day_halt", "kill")"""
        equity = self.account.equity()
        events: list[str] = []
        day = now.astimezone(self._tz).date()
        if day != self._day:
            self._day = day
            self.day_start_equity = equity
            self.halted_today = False
        self.peak_equity = max(self.peak_equity, equity)

        if not self.halted_today and equity <= self.day_start_equity * (1 - self.cfg.daily_loss_limit):
            self.halted_today = True
            events.append("day_halt")
            log.warning("%s daily loss limit hit: equity %.2f (start %.2f)", now, equity, self.day_start_equity)
        if not self.killed and equity <= self.peak_equity * (1 - self.cfg.max_drawdown):
            self.killed = True
            self.kill_reason = f"drawdown {1 - equity / self.peak_equity:.1%} from peak {self.peak_equity:.2f}"
            events.append("kill")
            log.error("%s KILL SWITCH: %s", now, self.kill_reason)
        return events

    # ------------------------------------------------------------------ จำสถานะข้ามการรีสตาร์ท (live)
    def to_state(self) -> dict:
        return {"day": self._day.isoformat() if self._day else None, "day_start_equity": self.day_start_equity,
                "peak_equity": self.peak_equity, "halted_today": self.halted_today, "killed": self.killed,
                "kill_reason": self.kill_reason}

    def load_state(self, st: dict) -> None:
        """kill switch และ peak ต้องรอดการรีสตาร์ท — ไม่งั้นแค่ปิดเปิดบอทก็ "ล้างโทษ" ได้"""
        self.peak_equity = float(st.get("peak_equity", 0.0))
        self.killed = bool(st.get("killed", False))
        self.kill_reason = st.get("kill_reason", "")
        if st.get("day"):
            self._day = date.fromisoformat(st["day"])
            self.day_start_equity = float(st.get("day_start_equity", 0.0))
            self.halted_today = bool(st.get("halted_today", False))

    # ------------------------------------------------------------------ อนุญาตเข้าไหม
    def can_open(self, sig: EnterSignal, now: datetime) -> str:
        """คืนเหตุผลที่ห้ามเข้า หรือ "" ถ้าผ่าน"""
        if self.killed:
            return "killed"
        if self.halted_today:
            return "daily_halt"
        if self.cfg.account_floor > 0 and self.real_equity is not None and self.real_equity() < self.cfg.account_floor:
            return "account_floor"
        if self.news and self.news.in_blackout(now):
            return "news_blackout"
        groups = {p.group_id for p in self.account.positions()}
        if len(groups) >= self.cfg.max_open_groups:
            return "max_open_groups"
        sym_groups = {p.group_id for p in self.account.positions(sig.symbol)}
        if len(sym_groups) >= self.cfg.max_groups_per_symbol:
            return "max_groups_symbol"
        if self.account.positions(sig.symbol, sig.tag):
            return "strategy_in_position"
        return ""

    # ------------------------------------------------------------------ ขนาดไม้
    def size(self, sig: EnterSignal, entry: float, spec: SymbolSpec) -> SizeDecision:
        stop_dist = abs(entry - sig.sl)
        if stop_dist <= 0:
            return SizeDecision(0.0, "zero_stop")
        equity = self.account.equity()
        risk_money = min(equity, self.account.balance()) * self.cfg.risk_per_trade
        loss_per_lot = (stop_dist + spec.slippage) * spec.contract_size + spec.commission_per_lot
        volume = spec.round_volume_down(risk_money / loss_per_lot)

        # จำกัดด้วย margin
        margin_per_lot = entry * spec.contract_size * spec.margin_rate
        if margin_per_lot > 0:
            max_by_margin = spec.round_volume_down(self.account.free_margin() * self.cfg.max_margin_use / margin_per_lot)
            volume = min(volume, max_by_margin)

        if volume < spec.volume_min and self.cfg.min_lot_max_risk > 0:
            if loss_per_lot * spec.volume_min <= equity * self.cfg.min_lot_max_risk                     and margin_per_lot * spec.volume_min <= self.account.free_margin() * self.cfg.max_margin_use:
                return SizeDecision(spec.volume_min, f"min_lot risk {loss_per_lot * spec.volume_min / equity:.1%}")
        if volume < spec.volume_min:
            return SizeDecision(0.0, f"too_small (need {risk_money / loss_per_lot:.4f} lot, min {spec.volume_min})")
        return SizeDecision(volume)
