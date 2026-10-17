"""
engine.py — ประกอบทุกชิ้นเข้าด้วยกัน (เทียบ AutoPickupController ใน PUBG)

Engine รู้จักแค่ interface → โค้ดชุดเดียวกันใช้ได้ทั้ง backtest และ live

ลำดับต่อแท่งที่ปิด (ต่อ symbol):
    1) RiskManager.on_time   : ตรวจเพดานรายวัน / kill switch (ถ้าโดน → ปิดไม้)
    2) PositionManager.on_bar: เลื่อน BE / trailing
    3) Strategy.on_bar       : ได้สัญญาณเข้า/ออก
    4) สัญญาณเข้า → OrderGuard.check → RiskManager.can_open → size → แบ่งไม้ → send
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable

from .core.interfaces import IAccount, IMarketData, IOrderExecutor
from .core.types import Bar, EnterSignal, ExitSignal, OrderRequest, Side
from .execution.order_guard import OrderGuard
from .execution.position_manager import PositionManager
from .risk.risk_manager import RiskManager
from .strategy.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class SignalLog:
    time: datetime
    key: str
    tag: str
    outcome: str        # "sent" | เหตุผลที่ไม่ได้ส่ง
    detail: str = ""


@dataclass
class EngineStats:
    signals: list[SignalLog] = field(default_factory=list)
    events: list[tuple[datetime, str]] = field(default_factory=list)


class _NoPositions:
    def positions(self, symbol=None, tag=None):  # noqa: ANN001, ANN201
        return []


_NO_POSITIONS = _NoPositions()


class Engine:
    def __init__(self, market: IMarketData, account: IAccount, executor: IOrderExecutor,
                 strategies: list[Strategy], risk: RiskManager, guard: OrderGuard,
                 last_bar_close: Callable[[str], datetime | None]) -> None:
        self.market = market
        self.account = account
        self.executor = executor
        self.strategies = strategies
        self.risk = risk
        self.guard = guard
        self._last_bar_close = last_bar_close
        self.manager = PositionManager({s.tag: s.plan for s in strategies}, account, executor, spec=market.spec)
        self.stats = EngineStats()
        self._by_symbol: dict[str, list[Strategy]] = {}
        for s in strategies:
            self._by_symbol.setdefault(s.symbol, []).append(s)
        self._groups = 0

    def on_bar(self, symbol: str, bar: Bar) -> None:
        now = self.market.now()
        for ev in self.risk.on_time(now):
            self.stats.events.append((now, ev))
            if ev == "kill" or (ev == "day_halt" and self.risk.cfg.close_on_daily_halt):
                self.close_all("halt")
            elif ev == "day_target":
                self.close_all("target")

        self.manager.on_bar(symbol, bar)

        for strat in self._by_symbol.get(symbol, []):
            strat.last_bar = bar
            for sig in strat.on_bar(bar, self.account):
                if isinstance(sig, ExitSignal):
                    for p in self.account.positions(sig.symbol, sig.tag):
                        self.executor.close(p.ticket, sig.reason)
                else:
                    self._enter(sig, now)

    def warmup(self, symbol: str, bar: Bar) -> None:
        """ป้อนแท่งประวัติให้กลยุทธ์/ATR สะสมสถานะ (ADR, EMA, กรอบ session) โดยไม่เทรด — ใช้ตอนเริ่ม live"""
        self.manager.warm(symbol, bar)
        for strat in self._by_symbol.get(symbol, []):
            strat.last_bar = bar
            strat.on_bar(bar, _NO_POSITIONS)

    def close_all(self, reason: str) -> None:
        for p in self.account.positions():
            self.executor.close(p.ticket, reason)

    # ------------------------------------------------------------------
    def _enter(self, sig: EnterSignal, now: datetime) -> None:
        if self.guard.is_consumed(sig.key) or self.risk.killed:
            return  # เงียบไว้ — กลยุทธ์ส่งซ้ำทุกแท่งเป็นเรื่องปกติ / kill แล้วไม่ต้องจด
        why = self.guard.check(sig, now, self._last_bar_close(sig.symbol), self.account.equity()) \
            or self.risk.can_open(sig, now)
        if why:
            self._log(now, sig, why)
            return

        spec = self.market.spec(sig.symbol)
        bar = self.market.last_bar(sig.symbol)
        assert bar is not None
        spread = max(bar.spread, spec.min_spread)
        entry = bar.close + spread + spec.slippage if sig.side is Side.BUY else bar.close - spec.slippage

        if spec.min_stop and abs(entry - sig.sl) < spec.min_stop:
            if not self.risk.cfg.widen_stops:
                self._log(now, sig, "stops_level", f"SL {abs(entry - sig.sl):.2f} < {spec.min_stop}")
                return
            # ขยาย SL ออกไปที่ระยะขั้นต่ำ +25% — ราคาขยับระหว่างแท่งปิดถึงตอนส่ง (BTC $20+ ใน 1 วิ) ถ้าเผื่อน้อย
            # โบรกจะปฏิเสธ (เจอจริง: เผื่อ 5% แล้วเหลือห่าง $199.8 < $200) · เสี่ยงต่อไม้มากขึ้นตามระยะที่ขยาย
            sig = replace(sig, sl=entry - int(sig.side) * spec.min_stop * 1.25)
        decision = self.risk.size(sig, entry, spec)
        if not decision.ok:
            self._log(now, sig, "size", decision.reason)
            return

        self._groups += 1
        # key ไม่ซ้ำอยู่แล้ว (consumed หลังส่ง) — เลขต่อท้ายแค่กันชนกรณีส่งซ้ำ key เดิม ต้องสั้นเพราะ comment MT5 ≤ 31 ตัว
        group = f"{sig.key}#{self._groups % 100}"
        for req in self._split(sig, decision.volume, entry, group):
            try:
                reply = self.executor.send(req)
            except Exception as e:  # noqa: BLE001
                # ไม่รู้ว่าออเดอร์ถึง server หรือยัง → fail-safe: ถือว่าใช้สัญญาณนี้ไปแล้ว ห้ามส่งซ้ำ
                self.guard.mark_consumed(sig.key, now)
                self._log(now, sig, "send_error", repr(e))
                log.exception("send failed for %s — marked consumed to avoid duplicate orders", sig.key)
                return
            self.guard.on_reply(sig, reply, now, self.account.equity())
            if not reply.accepted:
                self._log(now, sig, f"rejected:{reply.reason.value}", reply.message)
                return
        self._log(now, sig, "sent", f"{sig.side.name} {decision.volume} @~{entry:.2f} sl={sig.sl:.2f} | {sig.reason}")

    def _split(self, sig: EnterSignal, volume: float, entry: float, group: str) -> list[OrderRequest]:
        plan = next(s.plan for s in self._by_symbol[sig.symbol] if s.tag == sig.tag)
        spec = self.market.spec(sig.symbol)
        if plan.tp_usd or plan.sl_usd or plan.lock_tiers:
            # กำไร/ขาดทุน $X ต่อไม้ ไม่ว่าขนาดไม้เท่าไร: ระยะราคา = X / (lot × contract)
            per_price = volume * spec.contract_size
            tp = entry + int(sig.side) * plan.tp_usd / per_price if plan.tp_usd else (None if plan.lock_tiers else sig.tp)
            sl = entry - int(sig.side) * plan.sl_usd / per_price if plan.sl_usd else sig.sl
            return [OrderRequest(sig.symbol, sig.side, volume, sl, tp, sig.tag, group, "single")]
        if plan.partial_r is not None:
            a = spec.round_volume_down(volume * plan.partial_frac)
            b = round(volume - a, 8)
            if a >= spec.volume_min and b >= spec.volume_min:
                tp1 = entry + int(sig.side) * plan.partial_r * abs(entry - sig.sl)
                if sig.tp is None or (tp1 - sig.tp) * int(sig.side) < 0:  # TP1 ต้องมาก่อน TP สุดท้าย
                    return [
                        OrderRequest(sig.symbol, sig.side, a, sig.sl, tp1, sig.tag, group, "partial"),
                        OrderRequest(sig.symbol, sig.side, b, sig.sl, sig.tp, sig.tag, group, "runner"),
                    ]
        return [OrderRequest(sig.symbol, sig.side, volume, sig.sl, sig.tp, sig.tag, group, "single")]

    def _log(self, now: datetime, sig: EnterSignal, outcome: str, detail: str = "") -> None:
        self.stats.signals.append(SignalLog(now, sig.key, sig.tag, outcome, detail))
        log.debug("%s %s %s %s", now, sig.key, outcome, detail)
