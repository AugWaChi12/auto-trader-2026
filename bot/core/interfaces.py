"""
interfaces.py — จุดเชื่อมระหว่าง "สมองของบอท" กับ "โลก" ที่มันเทรดอยู่

หลักเดียวกับ Interfaces.h ของ AutoPickup (IItemScanner / ILootExecutor / IPlayerState):
Engine, RiskManager, OrderGuard รู้จักแค่ interface เหล่านี้ ไม่รู้ว่าข้างหลังเป็นอะไร

    Interface        Backtest (เฟส 1)              Demo / Live (เฟส 2)
    -------------    ---------------------------   ------------------------------
    IMarketData      SimBroker: เล่นแท่งย้อนหลัง     MT5: copy_rates_from_pos
    IOrderExecutor   SimBroker: จำลอง fill + spread  MT5: order_send (server ตรวจ)
    IAccount         SimBroker: บัญชีในหน่วยความจำ   MT5: account_info / positions_get

สลับ implementation ที่จุดเดียวตอนประกอบ Engine
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .types import Bar, OrderReply, OrderRequest, Position, SymbolSpec


class IMarketData(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """เวลาปัจจุบันของโลกนี้ (backtest = เวลาปิดของแท่งล่าสุด, live = เวลาจริง UTC)"""

    @abstractmethod
    def last_bar(self, symbol: str) -> Bar | None:
        """แท่งล่าสุดที่ปิดแล้ว"""

    @abstractmethod
    def spec(self, symbol: str) -> SymbolSpec: ...


class IAccount(ABC):
    @abstractmethod
    def balance(self) -> float: ...

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def free_margin(self) -> float: ...

    @abstractmethod
    def positions(self, symbol: str | None = None, tag: str | None = None) -> list[Position]: ...


class IOrderExecutor(ABC):
    # คืน accepted=True เมื่อ server รับคำสั่งแล้ว ถ้าปฏิเสธต้องบอก reason
    # เพื่อให้ OrderGuard เลือกวิธี backoff ได้ถูก (เหมือน PickupReply ใน PUBG)
    @abstractmethod
    def send(self, req: OrderRequest) -> OrderReply: ...

    @abstractmethod
    def modify(self, ticket: int, sl: float, tp: float | None) -> OrderReply: ...

    @abstractmethod
    def close(self, ticket: int, reason: str) -> OrderReply: ...
