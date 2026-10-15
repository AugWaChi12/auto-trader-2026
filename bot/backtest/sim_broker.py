"""
sim_broker.py — โบรกจำลองสำหรับ backtest (implement ทั้ง 3 interface เหมือน MockWorld ใน PUBG)

สมมติฐาน (ตั้งใจให้ "มองโลกแง่ร้าย" — backtest ที่สวยเกินจริงอันตรายกว่าที่แย่เกินจริง):
  - ราคาแท่ง = bid, ask = bid + spread (spread = max(สเปรดของแท่ง, min_spread ใน spec))
  - Market order fill ที่ราคาปิดของแท่งสัญญาณ + slippage (BUY ที่ ask, SELL ที่ bid)
  - SL/TP เช็คกับ high/low ของแท่งถัดๆ ไป:
      * เปิด gap ข้าม SL  → ออกที่ราคาเปิด (แย่กว่า SL)
      * แท่งเดียวแตะทั้ง SL และ TP → นับว่าโดน SL ก่อนเสมอ
      * SL ออกแบบ stop (มี slippage), TP ออกแบบ limit (ไม่มี slippage)
  - ค่าคอมคิดตอนปิด (ไป-กลับ ตามสัดส่วน volume)
  - ไม่คิด swap (กลยุทธ์ปิดไม้ภายในวัน)
ข้อจำกัด: ภายในแท่งไม่รู้ว่าราคาเดินทางไหนก่อน — ใช้ timeframe เล็ก (M5/M15) จะใกล้ความจริงกว่า
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..core.interfaces import IAccount, IMarketData, IOrderExecutor
from ..core.types import (Bar, ClosedTrade, OrderReply, OrderRequest, Position, RejectReason, Side,
                          SymbolSpec)


class SimBroker(IMarketData, IAccount, IOrderExecutor):
    def __init__(self, specs: dict[str, SymbolSpec], initial_balance: float, bar_duration: timedelta) -> None:
        self._specs = specs
        self._balance = initial_balance
        self._bar_duration = bar_duration
        self._positions: dict[int, Position] = {}
        self._last: dict[str, Bar] = {}
        self._now: datetime | None = None
        self._next_ticket = 1
        self.closed: list[ClosedTrade] = []

    # ------------------------------------------------------------------ IMarketData
    def now(self) -> datetime:
        assert self._now is not None
        return self._now

    def last_bar(self, symbol: str) -> Bar | None:
        return self._last.get(symbol)

    def last_bar_close(self, symbol: str) -> datetime | None:
        b = self._last.get(symbol)
        return b.time + self._bar_duration if b else None

    def spec(self, symbol: str) -> SymbolSpec:
        return self._specs[symbol]

    # ------------------------------------------------------------------ IAccount
    def balance(self) -> float:
        return self._balance

    def equity(self) -> float:
        return self._balance + sum(self._unrealized(p) for p in self._positions.values())

    def free_margin(self) -> float:
        used = sum(p.volume * self._specs[p.symbol].contract_size * p.entry_price * self._specs[p.symbol].margin_rate
                   for p in self._positions.values())
        return self.equity() - used

    def positions(self, symbol: str | None = None, tag: str | None = None) -> list[Position]:
        return [p for p in self._positions.values()
                if (symbol is None or p.symbol == symbol) and (tag is None or p.tag == tag)]

    # ------------------------------------------------------------------ ป้อนแท่ง
    def on_bar(self, symbol: str, bar: Bar) -> None:
        """เรียกก่อน engine เห็นแท่งนี้: ตรวจ SL/TP ของไม้ที่เปิดอยู่ด้วยช่วงราคาของแท่ง"""
        self._last[symbol] = bar
        self._now = bar.time + self._bar_duration
        for pos in list(self._positions.values()):
            if pos.symbol == symbol:
                self._check_exit(pos, bar)

    def spread(self, symbol: str) -> float:
        bar = self._last[symbol]
        return max(bar.spread, self._specs[symbol].min_spread)

    def _check_exit(self, pos: Position, bar: Bar) -> None:
        spec = self._specs[pos.symbol]
        slip = spec.slippage
        if pos.side is Side.BUY:
            o, h, l = bar.open, bar.high, bar.low                # ออกฝั่ง bid
            if o <= pos.sl:
                return self._close(pos, o - slip, self._stop_reason(pos))
            if pos.tp is not None and o >= pos.tp:
                return self._close(pos, o, "tp")
            if l <= pos.sl:
                return self._close(pos, pos.sl - slip, self._stop_reason(pos))
            if pos.tp is not None and h >= pos.tp:
                return self._close(pos, pos.tp, "tp")
        else:
            s = max(bar.spread, spec.min_spread)
            o, h, l = bar.open + s, bar.high + s, bar.low + s    # ออกฝั่ง ask
            if o >= pos.sl:
                return self._close(pos, o + slip, self._stop_reason(pos))
            if pos.tp is not None and o <= pos.tp:
                return self._close(pos, o, "tp")
            if h >= pos.sl:
                return self._close(pos, pos.sl + slip, self._stop_reason(pos))
            if pos.tp is not None and l <= pos.tp:
                return self._close(pos, pos.tp, "tp")
        return None

    @staticmethod
    def _stop_reason(pos: Position) -> str:
        if pos.sl == pos.initial_sl:
            return "sl"
        locked = (pos.sl - pos.entry_price) * int(pos.side)
        return "trail" if locked > 0 else "be"

    # ------------------------------------------------------------------ IOrderExecutor
    def send(self, req: OrderRequest) -> OrderReply:
        spec = self._specs[req.symbol]
        bar = self._last.get(req.symbol)
        if bar is None:
            return OrderReply(False, RejectReason.MARKET_CLOSED, message="no price")
        if req.volume < spec.volume_min or abs(spec.round_volume_down(req.volume) - req.volume) > 1e-9:
            return OrderReply(False, RejectReason.INVALID_VOLUME, message=f"volume {req.volume}")

        bid = bar.close
        ask = bid + self.spread(req.symbol)
        if req.side is Side.BUY:
            fill = ask + spec.slippage
            stops_ok = req.sl < bid and (req.tp is None or req.tp > fill)
        else:
            fill = bid - spec.slippage
            stops_ok = req.sl > ask and (req.tp is None or req.tp < fill)
        if not stops_ok:
            return OrderReply(False, RejectReason.INVALID_STOPS,
                              message=f"sl={req.sl:.2f} tp={req.tp} bid={bid:.2f} ask={ask:.2f}")

        margin = req.volume * spec.contract_size * fill * spec.margin_rate
        if margin > self.free_margin():
            return OrderReply(False, RejectReason.NO_MONEY, message=f"margin {margin:.2f}")

        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = Position(
            ticket=ticket, symbol=req.symbol, side=req.side, volume=req.volume, entry_price=fill,
            sl=req.sl, tp=req.tp, open_time=self.now(), tag=req.tag, group_id=req.group_id,
            role=req.role, initial_sl=req.sl,
        )
        return OrderReply(True, ticket=ticket, fill_price=fill)

    def modify(self, ticket: int, sl: float, tp: float | None) -> OrderReply:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderReply(False, RejectReason.UNKNOWN, message="no such position")
        bid = self._last[pos.symbol].close
        ask = bid + self.spread(pos.symbol)
        ok = sl < bid if pos.side is Side.BUY else sl > ask
        if not ok:
            return OrderReply(False, RejectReason.INVALID_STOPS, message=f"sl {sl:.2f} beyond price")
        pos.sl, pos.tp = sl, tp
        return OrderReply(True, ticket=ticket)

    def close(self, ticket: int, reason: str) -> OrderReply:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderReply(False, RejectReason.UNKNOWN, message="no such position")
        spec = self._specs[pos.symbol]
        bid = self._last[pos.symbol].close
        price = bid - spec.slippage if pos.side is Side.BUY else bid + self.spread(pos.symbol) + spec.slippage
        self._close(pos, price, reason)
        return OrderReply(True, ticket=ticket, fill_price=price)

    # ------------------------------------------------------------------ ภายใน
    def _unrealized(self, pos: Position) -> float:
        spec = self._specs[pos.symbol]
        bid = self._last[pos.symbol].close
        price = bid if pos.side is Side.BUY else bid + self.spread(pos.symbol)
        return spec.pnl(pos.side, pos.entry_price, price, pos.volume)

    def _close(self, pos: Position, price: float, reason: str) -> None:
        spec = self._specs[pos.symbol]
        pnl = spec.pnl(pos.side, pos.entry_price, price, pos.volume) - spec.commission_per_lot * pos.volume
        risk = (pos.risk_per_unit + spec.slippage) * pos.volume * spec.contract_size \
            + spec.commission_per_lot * pos.volume
        self._balance += pnl
        del self._positions[pos.ticket]
        self.closed.append(ClosedTrade(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side, volume=pos.volume,
            entry_price=pos.entry_price, exit_price=price, open_time=pos.open_time,
            close_time=self.now(), pnl=pnl, risk_amount=risk, exit_reason=reason,
            tag=pos.tag, group_id=pos.group_id, role=pos.role,
        ))
