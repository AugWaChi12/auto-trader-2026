"""
position_manager.py — บริหารไม้หลังเข้า: เลื่อน SL ไป break-even และ trailing

ทำงานตอนแท่งปิด (ไม่ใช่ทุก tick) → SL ที่เลื่อนมีผลตั้งแต่แท่งถัดไป
ใช้ได้เหมือนกันทั้ง backtest และ live เพราะเรียกผ่าน IOrderExecutor.modify()

ไม้ที่แตกจากสัญญาณเดียว (group):
    partial : มี TP ที่ partial_r → ถึงแล้วปิดเองฝั่ง server
    runner  : เมื่อ partial หายไปจากรายการ (= ถึง TP แล้ว เพราะทั้งคู่ใช้ SL เดียวกัน
              ถ้าโดน SL จะหายไปพร้อมกัน) → เลื่อน SL เป็น BE แล้วเริ่ม trail
    single  : แบ่งไม่ได้ → เลื่อน BE เมื่อราคาเคยไปถึง partial_r
"""
from __future__ import annotations

import logging

from ..core.indicators import ATR
from ..core.interfaces import IAccount, IOrderExecutor
from ..core.types import Bar, Position, RejectReason, Side
from ..strategy.base import ManagementPlan

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, plans: dict[str, ManagementPlan], account: IAccount, executor: IOrderExecutor,
                 atr_period: int = 14) -> None:
        self.plans = plans
        self.account = account
        self.executor = executor
        self._atr: dict[str, ATR] = {}
        self._atr_period = atr_period

    def warm(self, symbol: str, bar: Bar) -> None:
        self._atr.setdefault(symbol, ATR(self._atr_period)).update(bar)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        atr = self._atr.setdefault(symbol, ATR(self._atr_period)).update(bar)
        positions = self.account.positions(symbol)
        open_groups_roles = {(p.group_id, p.role) for p in positions}

        for pos in positions:
            plan = self.plans.get(pos.tag)
            if plan is None or pos.risk_per_unit <= 0:
                continue
            side = int(pos.side)
            if pos.side is Side.BUY:
                pos.best_price = max(pos.best_price, bar.high)
            else:
                pos.best_price = min(pos.best_price, bar.low)
            mfe_r = (pos.best_price - pos.entry_price) * side / pos.risk_per_unit

            new_sl = pos.sl
            if not pos.moved_to_be and plan.partial_r is not None:
                partial_done = pos.role == "runner" and (pos.group_id, "partial") not in open_groups_roles
                single_reached = pos.role == "single" and mfe_r >= plan.partial_r
                if partial_done or single_reached:
                    be = pos.entry_price + side * plan.be_offset_r * pos.risk_per_unit
                    new_sl = self._tighter(pos, new_sl, be)
                    pos.moved_to_be = True

            if plan.trail_atr_mult and atr and pos.role != "partial" and mfe_r >= plan.trail_start_r:
                trail = bar.close - side * plan.trail_atr_mult * atr
                new_sl = self._tighter(pos, new_sl, trail)

            new_tp = pos.tp
            if plan.tp_extend_at is not None and pos.tp is not None and pos.role != "partial":
                tp_dist = (pos.tp - pos.entry_price) * side
                if tp_dist > 0 and (bar.close - pos.entry_price) * side >= plan.tp_extend_at * tp_dist:
                    new_tp = pos.tp + side * plan.tp_extend_r * pos.risk_per_unit

            if new_sl != pos.sl or new_tp != pos.tp:
                reply = self.executor.modify(pos.ticket, new_sl, new_tp)
                if reply.accepted:
                    continue
                if reply.reason is RejectReason.INVALID_STOPS:
                    # ราคาย้อนเลยจุด stop ใหม่ไปแล้ว (แท่งกลับตัวแรง) → ปิดเลย ดีกว่าปล่อยไว้ที่ SL เดิม
                    self.executor.close(pos.ticket, "stop_moved")
                else:
                    log.warning("modify SL failed ticket=%s: %s %s", pos.ticket, reply.reason, reply.message)

    @staticmethod
    def _tighter(pos: Position, current: float, candidate: float) -> float:
        """SL ขยับได้ทางเดียว: เข้าหาราคา (ลดความเสี่ยง) เท่านั้น"""
        if pos.side is Side.BUY:
            return max(current, candidate)
        return min(current, candidate)
