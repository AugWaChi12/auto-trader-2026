"""
order_guard.py — กันออเดอร์ซ้ำ / พักเมื่อถูกปฏิเสธ / ไม่เทรดด้วยราคาเก่า

port มาจาก BackoffTable (C++) และสถานะ IN_FLIGHT (controller.py) ของโปรเจค PUBG

    PUBG                              ทอง
    ------------------------------    ---------------------------------------------
    queuedIds_ / IN_FLIGHT            consumed keys: สัญญาณเดียวกันส่งได้ครั้งเดียว
    PickupReply::Gone                 INVALID_STOPS / INVALID_VOLUME → ทิ้งสัญญาณนี้
    PickupReply::InventoryFull        NO_MONEY → พักจน equity เพิ่ม (เหมือนล้างเมื่อ FreeCapacity เพิ่ม)
    PickupReply::Denied (backoff)     PRICE_CHANGED / CONNECTION / อื่นๆ → exponential backoff
    Stale-frame guard                 แท่งล่าสุดเก่าเกิน stale_after → ไม่เข้าไม้ใหม่

ทำไม consumed key สำคัญที่สุด:
  กลยุทธ์ส่งสัญญาณเดิมซ้ำทุกแท่งตราบที่เงื่อนไขยังจริง (ตั้งใจออกแบบให้ retry ได้)
  ในเกม หยิบซ้ำแค่เสียเวลา — ในตลาด ส่งซ้ำ = เปิดไม้ซ้ำ = เสี่ยงเป็นสองเท่า
  live (เฟส 2) ต้องโหลด key จากไม้ที่เปิดอยู่ (comment ของออเดอร์) ตอนสตาร์ท
  เพื่อให้รีสตาร์ทบอทกลางวันแล้วไม่เข้าซ้ำ
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.types import EnterSignal, OrderReply, RejectReason


@dataclass(frozen=True)
class GuardConfig:
    stale_after: timedelta = timedelta(minutes=5)   # live: แท่งล่าสุดต้องปิดไม่เกินเท่านี้
    backoff_base: timedelta = timedelta(seconds=2)
    backoff_max: timedelta = timedelta(minutes=2)
    no_money_max: timedelta = timedelta(hours=1)
    key_ttl: timedelta = timedelta(days=7)


@dataclass
class _Backoff:
    until: datetime
    reason: RejectReason
    failures: int
    equity_at: float = 0.0


class OrderGuard:
    def __init__(self, cfg: GuardConfig | None = None) -> None:
        self.cfg = cfg or GuardConfig()
        self._consumed: dict[str, datetime] = {}
        self._backoff: dict[tuple[str, str], _Backoff] = {}   # (symbol, tag) -> backoff

    def mark_consumed(self, key: str, now: datetime) -> None:
        self._consumed[key] = now

    def is_consumed(self, key: str) -> bool:
        return key in self._consumed

    def check(self, sig: EnterSignal, now: datetime, last_bar_close: datetime | None, equity: float) -> str:
        """คืนเหตุผลที่ห้ามส่ง หรือ "" ถ้าผ่าน"""
        if sig.key in self._consumed:
            return "duplicate"
        if last_bar_close is None or now - last_bar_close > self.cfg.stale_after:
            return "stale_data"
        b = self._backoff.get((sig.symbol, sig.tag))
        if b:
            if b.reason is RejectReason.NO_MONEY and equity > b.equity_at:
                del self._backoff[(sig.symbol, sig.tag)]   # มีเงินเพิ่มแล้ว ลองได้ทันที
            elif now < b.until:
                return f"backoff:{b.reason.value}"
        return ""

    def on_reply(self, sig: EnterSignal, reply: OrderReply, now: datetime, equity: float) -> None:
        k = (sig.symbol, sig.tag)
        if reply.accepted:
            self._consumed[sig.key] = now
            self._backoff.pop(k, None)
            return
        if reply.reason in (RejectReason.INVALID_STOPS, RejectReason.INVALID_VOLUME):
            self._consumed[sig.key] = now   # สัญญาณนี้แก้ไม่ได้แล้ว ไม่ต้องลองซ้ำ
            return
        prev = self._backoff.get(k)
        failures = prev.failures + 1 if prev else 1
        if reply.reason is RejectReason.NO_MONEY:
            wait = self.cfg.no_money_max
        else:
            wait = min(self.cfg.backoff_base * 2 ** (failures - 1), self.cfg.backoff_max)
        self._backoff[k] = _Backoff(now + wait, reply.reason, failures, equity)

    def gc(self, now: datetime) -> None:
        cutoff = now - self.cfg.key_ttl
        self._consumed = {k: t for k, t in self._consumed.items() if t >= cutoff}
