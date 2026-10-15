"""
runner.py — เล่นแท่งย้อนหลังของหลาย symbol ตามลำดับเวลา แล้วป้อนเข้า Engine
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime

from ..core.types import Bar, ClosedTrade, SymbolSpec, timeframe_delta
from ..engine import Engine, EngineStats
from ..execution.order_guard import OrderGuard
from ..risk.news import NewsCalendar
from ..risk.risk_manager import RiskConfig, RiskManager
from ..strategy.base import Strategy
from .sim_broker import SimBroker


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    trades: list[ClosedTrade]
    equity: list[tuple[datetime, float]]
    stats: EngineStats
    killed: str


def run_backtest(bars: dict[str, list[Bar]], specs: dict[str, SymbolSpec], strategies: list[Strategy],
                 risk_cfg: RiskConfig, timeframe: str, initial_balance: float,
                 news: NewsCalendar | None = None) -> BacktestResult:
    broker = SimBroker(specs, initial_balance, timeframe_delta(timeframe))
    risk = RiskManager(risk_cfg, broker, news)
    engine = Engine(broker, broker, broker, strategies, risk, OrderGuard(), broker.last_bar_close)

    # merge แท่งของทุก symbol ตามเวลา (ลำดับ symbol คงที่เมื่อเวลาเท่ากัน)
    order = {s: i for i, s in enumerate(bars)}
    stream = heapq.merge(*([(b.time, order[s], s, b) for b in series] for s, series in bars.items()))

    equity: list[tuple[datetime, float]] = []
    last_day = None
    for _, _, symbol, bar in stream:
        broker.on_bar(symbol, bar)
        engine.on_bar(symbol, bar)
        now = broker.now()
        # เก็บ equity ทุกแท่ง แต่ไม่ต้องละเอียดเกิน → 1 จุดต่อ (เวลา) ที่ไม่ซ้ำ
        if equity and equity[-1][0] == now:
            equity[-1] = (now, broker.equity())
        else:
            equity.append((now, broker.equity()))
        if now.date() != last_day:
            last_day = now.date()
            engine.guard.gc(now)

    if broker.positions():
        engine.close_all("end")
        equity.append((broker.now(), broker.equity()))

    return BacktestResult(initial_balance, broker.balance(), broker.closed, equity, engine.stats,
                          risk.kill_reason)
