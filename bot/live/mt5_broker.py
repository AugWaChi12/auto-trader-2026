"""
mt5_broker.py — implement 3 interface ด้วย MetaTrader5 API (เทียบ SandboxWorld ใน PUBG)

Engine / RiskManager / PositionManager / กลยุทธ์ ใช้ตัวเดียวกับ backtest ทั้งหมด
ต่างกันแค่ "โลก" ที่อยู่ข้างหลัง interface

ความปลอดภัย:
  - connect() ปฏิเสธบัญชีที่ไม่ใช่ DEMO เสมอ (เฟสนี้ไม่มีทางเทรดเงินจริงได้โดยไม่แก้โค้ด)
  - SL/TP ส่งไปกับออเดอร์ → อยู่ฝั่ง server แม้บอท/เครื่องดับ ไม้ก็ยังมี stop
  - ออเดอร์ทุกตัวติด magic number → บอทไม่แตะไม้ที่คนเปิดเอง

ข้อมูลที่ต้องจำข้ามการรีสตาร์ท:
  - comment ของออเดอร์ = "<signal key>#<n>|<role>"  เช่น "xau_momo:2026-10-15#1|s"
    (MT5 จำกัด 31 ตัวอักษร) → กู้ group / role / tag / signal key ได้จาก server
  - initial_sl / moved_to_be / best_price → state/positions.json (บันทึกทุกแท่ง)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.interfaces import IAccount, IMarketData, IOrderExecutor
from ..core.timeutil import UTC, server_to_utc
from ..core.types import (Bar, OrderReply, OrderRequest, Position, RejectReason, Side, SymbolSpec,
                          timeframe_delta)

log = logging.getLogger(__name__)

ROLE_CODE = {"single": "s", "partial": "p", "runner": "r"}
ROLE_NAME = {v: k for k, v in ROLE_CODE.items()}

# retcode → เหตุผลที่ OrderGuard ใช้เลือกวิธี backoff
RETCODES: dict[int, RejectReason] = {
    10004: RejectReason.PRICE_CHANGED,   # requote
    10006: RejectReason.UNKNOWN,         # rejected
    10014: RejectReason.INVALID_VOLUME,
    10015: RejectReason.PRICE_CHANGED,   # invalid price
    10016: RejectReason.INVALID_STOPS,
    10017: RejectReason.TRADE_DISABLED,
    10018: RejectReason.MARKET_CLOSED,
    10019: RejectReason.NO_MONEY,
    10020: RejectReason.PRICE_CHANGED,
    10021: RejectReason.PRICE_CHANGED,   # off quotes
    10024: RejectReason.PRICE_CHANGED,   # too many requests
    10026: RejectReason.TRADE_DISABLED,  # autotrading disabled by server
    10027: RejectReason.TRADE_DISABLED,  # autotrading disabled by client (ปุ่ม Algo Trading)
    10031: RejectReason.CONNECTION,
}
DONE = (10008, 10009, 10010)  # placed / done / done partial


def encode_comment(group_id: str, role: str) -> str:
    c = f"{group_id}|{ROLE_CODE[role]}"
    if len(c) > 31:
        raise ValueError(f"comment ยาวเกิน 31 ตัวอักษร: {c}")
    return c


def decode_comment(comment: str) -> tuple[str, str, str, str] | None:
    """คืน (tag, signal_key, group_id, role) หรือ None ถ้าไม่ใช่ของบอท"""
    if "|" not in comment or "#" not in comment:
        return None
    group, code = comment.rsplit("|", 1)
    key = group.split("#", 1)[0]
    if code not in ROLE_NAME or ":" not in key:
        return None
    return key.split(":", 1)[0], key, group, ROLE_NAME[code]


class NotDemoAccount(RuntimeError):
    pass


class Mt5Broker(IMarketData, IAccount, IOrderExecutor):
    def __init__(self, symbols: list[str], specs: dict[str, SymbolSpec], timeframe: str, time_mode: str,
                 magic: int, state_dir: Path, mt5: Any = None, deviation_points: int = 30,
                 allocation: float | None = None) -> None:
        if mt5 is None:
            import MetaTrader5 as mt5  # noqa: N813
        self.mt5 = mt5
        self.symbols = symbols
        self._specs = dict(specs)
        self.timeframe = timeframe
        self._tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
        self._bar_duration = timeframe_delta(timeframe)
        self.time_mode = time_mode
        self.magic = magic
        self.deviation = deviation_points
        self._names: dict[str, str] = {}          # canonical -> ชื่อในโบรก
        self._last: dict[str, Bar] = {}
        self._cache: dict[int, Position] = {}     # ticket -> Position (เก็บ meta ที่ server ไม่รู้)
        # allocation: ทุนเสมือนของบอทนี้ → balance/equity คิดจากกำไรขาดทุนของ magic ตัวเองเท่านั้น
        # ทำให้บอทหลายตัวบนบัญชีเดียวมีเพดานขาดทุน/ขนาดไม้เป็นของตัวเอง ไม่ลากกันล้ม
        self.allocation = allocation
        self._realized_cache: tuple[float, float] | None = None
        self._state_path = state_dir / "positions.json"
        self._meta: dict[str, dict] = self._load_meta()

    # ------------------------------------------------------------------ เชื่อมต่อ
    def connect(self, path: str | None) -> None:
        mt5 = self.mt5
        ok = mt5.initialize(path=path) if path else mt5.initialize()
        if not ok:
            raise ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")
        acc = mt5.account_info()
        if acc is None:
            raise ConnectionError(f"account_info() = None: {mt5.last_error()}")
        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise NotDemoAccount(f"บัญชี {acc.login}@{acc.server} ไม่ใช่ DEMO — เฟสนี้อนุญาตเฉพาะ demo")
        if acc.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            log.warning("บัญชีไม่ใช่ Hedging — partial/runner และกลยุทธ์สวนทางกันจะทำงานผิด")
        log.info("connected DEMO %s@%s balance=%.2f %s leverage=1:%d", acc.login, acc.server, acc.balance,
                 acc.currency, acc.leverage)
        from ..data.mt5_source import find_symbol
        for canonical in self.symbols:
            name = find_symbol(canonical, mt5)
            mt5.symbol_select(name, True)
            self._names[canonical] = name
            info = mt5.symbol_info(name)
            base = self._specs[canonical]
            self._specs[canonical] = SymbolSpec(
                name=canonical, point=info.point, contract_size=info.trade_contract_size,
                volume_min=info.volume_min, volume_step=info.volume_step, volume_max=info.volume_max,
                min_spread=base.min_spread, slippage=base.slippage,
                commission_per_lot=base.commission_per_lot, margin_rate=base.margin_rate)
            log.info("symbol %s → %s contract=%s min_lot=%s", canonical, name, info.trade_contract_size, info.volume_min)

    def algo_trading_enabled(self) -> bool:
        t = self.mt5.terminal_info()
        return bool(t and t.trade_allowed)

    def connected(self) -> bool:
        t = self.mt5.terminal_info()
        return bool(t and t.connected)

    # ------------------------------------------------------------------ แท่งราคา
    def _to_bar(self, r: Any, spec: SymbolSpec) -> Bar:
        t = server_to_utc(datetime.fromtimestamp(int(r["time"]), UTC).replace(tzinfo=None), self.time_mode)
        return Bar(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                   float(r["spread"]) * spec.point, float(r["tick_volume"]))

    def closed_bars(self, symbol: str, count: int) -> list[Bar]:
        """แท่งที่ปิดแล้วล่าสุด count แท่ง (start_pos=1 ข้ามแท่งที่กำลังก่อตัว)"""
        rates = self.mt5.copy_rates_from_pos(self._names[symbol], self._tf, 1, count)
        if rates is None:
            raise ConnectionError(f"copy_rates_from_pos({symbol}) failed: {self.mt5.last_error()}")
        spec = self._specs[symbol]
        return [self._to_bar(r, spec) for r in rates]

    def set_last_bar(self, symbol: str, bar: Bar) -> None:
        self._last[symbol] = bar

    # ------------------------------------------------------------------ IMarketData
    def now(self) -> datetime:
        return datetime.now(UTC)

    def last_bar(self, symbol: str) -> Bar | None:
        return self._last.get(symbol)

    def last_bar_close(self, symbol: str) -> datetime | None:
        b = self._last.get(symbol)
        return b.time + self._bar_duration if b else None

    def spec(self, symbol: str) -> SymbolSpec:
        return self._specs[symbol]

    # ------------------------------------------------------------------ IAccount
    def _realized(self) -> float:
        """กำไรขาดทุนที่ปิดแล้วทั้งหมดของ magic นี้ (cache 5 วินาที)"""
        import time
        now = time.monotonic()
        if self._realized_cache and now - self._realized_cache[0] < 5:
            return self._realized_cache[1]
        deals = self.mt5.history_deals_get(datetime(2020, 1, 1, tzinfo=UTC), datetime.now(UTC) + timedelta(days=1)) or ()
        total = sum(d.profit + d.commission + d.swap + d.fee for d in deals if d.magic == self.magic)
        self._realized_cache = (now, total)
        return total

    def balance(self) -> float:
        if self.allocation is not None:
            return self.allocation + self._realized()
        return float(self.mt5.account_info().balance)

    def equity(self) -> float:
        if self.allocation is not None:
            floating = sum(p.profit + p.swap for p in (self.mt5.positions_get() or ()) if p.magic == self.magic)
            return self.balance() + floating
        return float(self.mt5.account_info().equity)

    def free_margin(self) -> float:
        real = float(self.mt5.account_info().margin_free)
        return min(real, self.equity()) if self.allocation is not None else real

    def positions(self, symbol: str | None = None, tag: str | None = None) -> list[Position]:
        raw = self.mt5.positions_get() or ()
        live: dict[int, Position] = {}
        canonical = {v: k for k, v in self._names.items()}
        for p in raw:
            if p.magic != self.magic or p.symbol not in canonical:
                continue
            decoded = decode_comment(p.comment)
            if decoded is None:
                continue
            ptag, _, group, role = decoded
            pos = self._cache.get(p.ticket)
            if pos is None:
                meta = self._meta.get(str(p.ticket), {})
                pos = Position(
                    ticket=p.ticket, symbol=canonical[p.symbol], side=Side.BUY if p.type == 0 else Side.SELL,
                    volume=p.volume, entry_price=p.price_open, sl=p.sl, tp=p.tp or None,
                    open_time=datetime.fromtimestamp(p.time, UTC), tag=ptag, group_id=group, role=role,
                    initial_sl=meta.get("initial_sl", p.sl), moved_to_be=meta.get("moved_to_be", False),
                    best_price=meta.get("best_price", p.price_open))
            pos.sl, pos.tp, pos.volume = p.sl, (p.tp or None), p.volume
            live[p.ticket] = pos
        self._cache = live
        return [p for p in live.values()
                if (symbol is None or p.symbol == symbol) and (tag is None or p.tag == tag)]

    # ------------------------------------------------------------------ IOrderExecutor
    def _filling(self, name: str) -> int:
        mode = self.mt5.symbol_info(name).filling_mode
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def _result(self, res: Any) -> OrderReply:
        if res is None:
            return OrderReply(False, RejectReason.CONNECTION, message=str(self.mt5.last_error()))
        if res.retcode in DONE:
            return OrderReply(True, ticket=res.order, fill_price=res.price, message=res.comment)
        return OrderReply(False, RETCODES.get(res.retcode, RejectReason.UNKNOWN),
                          message=f"retcode {res.retcode}: {res.comment}")

    def send(self, req: OrderRequest) -> OrderReply:
        mt5 = self.mt5
        name = self._names[req.symbol]
        info = mt5.symbol_info(name)
        tick = mt5.symbol_info_tick(name)
        if info is None or tick is None:
            return OrderReply(False, RejectReason.CONNECTION, message="no symbol info/tick")
        buy = req.side is Side.BUY
        price = tick.ask if buy else tick.bid
        sl = round(req.sl, info.digits)
        tp = round(req.tp, info.digits) if req.tp is not None else 0.0
        min_dist = info.trade_stops_level * info.point
        if (buy and sl > tick.bid - min_dist) or (not buy and sl < tick.ask + min_dist):
            return OrderReply(False, RejectReason.INVALID_STOPS, message=f"sl {sl} vs bid {tick.bid} ask {tick.ask}")
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": float(req.volume),
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price,
            "sl": sl, "tp": tp, "deviation": self.deviation, "magic": self.magic,
            "comment": encode_comment(req.group_id, req.role),
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(name),
        }
        reply = self._result(mt5.order_send(request))
        log.info("SEND %s %s %.2f @%.2f sl=%.2f tp=%s %s → %s %s", req.symbol, req.side.name, req.volume, price, sl,
                 tp or "-", request["comment"], "OK" if reply.accepted else reply.reason.value, reply.message)
        if reply.accepted and reply.ticket:
            fill = reply.fill_price or price
            self._cache[reply.ticket] = Position(
                ticket=reply.ticket, symbol=req.symbol, side=req.side, volume=req.volume, entry_price=fill,
                sl=sl, tp=req.tp, open_time=self.now(), tag=req.tag, group_id=req.group_id, role=req.role,
                initial_sl=sl)
            self.save_meta()
        return reply

    def modify(self, ticket: int, sl: float, tp: float | None) -> OrderReply:
        mt5 = self.mt5
        found = mt5.positions_get(ticket=ticket)
        if not found:
            return OrderReply(False, RejectReason.UNKNOWN, message="no such position")
        p = found[0]
        info = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        sl = round(sl, info.digits)
        min_dist = info.trade_stops_level * info.point
        if (p.type == 0 and sl > tick.bid - min_dist) or (p.type == 1 and sl < tick.ask + min_dist):
            return OrderReply(False, RejectReason.INVALID_STOPS, message=f"sl {sl} beyond price")
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p.symbol, "sl": sl,
                   "tp": round(tp, info.digits) if tp is not None else 0.0, "magic": self.magic}
        reply = self._result(mt5.order_send(request))
        log.info("MODIFY #%s sl %.2f → %.2f → %s %s", ticket, p.sl, sl, "OK" if reply.accepted else reply.reason.value,
                 reply.message)
        if reply.accepted:
            self.save_meta()
        return reply

    def close(self, ticket: int, reason: str) -> OrderReply:
        mt5 = self.mt5
        found = mt5.positions_get(ticket=ticket)
        if not found:
            return OrderReply(False, RejectReason.UNKNOWN, message="no such position")
        p = found[0]
        tick = mt5.symbol_info_tick(p.symbol)
        buy_pos = p.type == 0
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": ticket,
            "type": mt5.ORDER_TYPE_SELL if buy_pos else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if buy_pos else tick.ask, "deviation": self.deviation, "magic": self.magic,
            "comment": reason[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(p.symbol),
        }
        reply = self._result(mt5.order_send(request))
        log.info("CLOSE #%s (%s) → %s %s", ticket, reason, "OK" if reply.accepted else reply.reason.value, reply.message)
        return reply

    # ------------------------------------------------------------------ ประวัติ / state
    def bot_deals(self, since: datetime) -> list[Any]:
        deals = self.mt5.history_deals_get(since, datetime.now(UTC) + timedelta(days=1)) or ()
        return [d for d in deals if d.magic == self.magic]

    def consumed_keys(self, days: int = 3) -> set[str]:
        """signal key ที่เคยเปิดไม้แล้ว (จากไม้ที่เปิดอยู่ + deal ย้อนหลัง) → กันเข้าซ้ำหลังรีสตาร์ท"""
        keys = set()
        for p in self.mt5.positions_get() or ():
            if p.magic == self.magic and (d := decode_comment(p.comment)):
                keys.add(d[1])
        for deal in self.bot_deals(datetime.now(UTC) - timedelta(days=days)):
            if deal.entry == self.mt5.DEAL_ENTRY_IN and (d := decode_comment(deal.comment)):
                keys.add(d[1])
        return keys

    def _load_meta(self) -> dict[str, dict]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_meta(self) -> None:
        meta = {str(t): {"initial_sl": p.initial_sl, "moved_to_be": p.moved_to_be, "best_price": p.best_price}
                for t, p in self._cache.items()}
        self._meta = meta
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        tmp.replace(self._state_path)
