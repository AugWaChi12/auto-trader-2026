"""ทดสอบส่วนที่ "ผิดแล้วเสียเงินจริง": เวลา, sizing, fill, SL/TP, กันส่งซ้ำ, เพดานขาดทุน"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot.backtest.sim_broker import SimBroker
from bot.core.timeutil import UTC, server_to_utc
from bot.core.types import Bar, EnterSignal, OrderReply, OrderRequest, RejectReason, Side, SymbolSpec
from bot.execution.order_guard import OrderGuard
from bot.risk.news import NewsCalendar, nfp_times
from bot.risk.risk_manager import RiskConfig, RiskManager

XAU = SymbolSpec(name="XAUUSD", point=0.01, contract_size=100, volume_min=0.01, volume_step=0.01,
                 min_spread=0.20, slippage=0.0, commission_per_lot=0.0, margin_rate=0.01)
T0 = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)
M15 = timedelta(minutes=15)


def bar(t: datetime, o: float, h: float, l: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(t, o, h, l, c, spread)


def broker_with_price(price: float = 2000.0, balance: float = 10_000.0) -> SimBroker:
    b = SimBroker({"XAUUSD": XAU}, balance, M15)
    b.on_bar("XAUUSD", bar(T0, price, price, price, price))
    return b


# ------------------------------------------------------------------ เวลา server
@pytest.mark.parametrize("server, expected_utc", [
    (datetime(2024, 1, 15, 9, 0), datetime(2024, 1, 15, 7, 0)),    # หน้าหนาว GMT+2
    (datetime(2024, 7, 15, 10, 0), datetime(2024, 7, 15, 7, 0)),   # US DST GMT+3
    (datetime(2024, 3, 20, 10, 0), datetime(2024, 3, 20, 7, 0)),   # US เข้า DST แล้ว แต่ยุโรปยัง (10 มี.ค. vs 31 มี.ค.)
])
def test_server_time_ny_close(server: datetime, expected_utc: datetime) -> None:
    assert server_to_utc(server, "ny_close") == expected_utc.replace(tzinfo=UTC)


def test_server_time_fixed() -> None:
    assert server_to_utc(datetime(2024, 1, 15, 9, 0), "fixed:3") == datetime(2024, 1, 15, 6, 0, tzinfo=UTC)


# ------------------------------------------------------------------ sizing
def test_size_risks_exactly_one_percent() -> None:
    b = broker_with_price()
    rm = RiskManager(RiskConfig(risk_per_trade=0.01), b)
    sig = EnterSignal("XAUUSD", Side.BUY, sl=1995.0, tp=None, tag="t", key="k")
    # เสี่ยง 100 USD, SL ห่าง 5.00 → เสีย 500/lot → 0.20 lot
    assert rm.size(sig, 2000.0, XAU).volume == pytest.approx(0.20)


def test_size_rounds_down_never_up() -> None:
    b = broker_with_price()
    rm = RiskManager(RiskConfig(risk_per_trade=0.01), b)
    sig = EnterSignal("XAUUSD", Side.BUY, sl=1993.0, tp=None, tag="t", key="k")
    # 100 / 700 = 0.1428 → 0.14 (ปัดลง)
    assert rm.size(sig, 2000.0, XAU).volume == pytest.approx(0.14)


def test_size_rejects_when_below_min_lot() -> None:
    b = broker_with_price(balance=100.0)
    rm = RiskManager(RiskConfig(risk_per_trade=0.01), b)
    sig = EnterSignal("XAUUSD", Side.BUY, sl=1990.0, tp=None, tag="t", key="k")
    d = rm.size(sig, 2000.0, XAU)   # เสี่ยง 1 USD แต่ 0.01 lot เสีย 10 USD
    assert not d.ok and "too_small" in d.reason


# ------------------------------------------------------------------ fill / SL / TP
def test_buy_fills_at_ask_and_sell_at_bid() -> None:
    b = broker_with_price(2000.0)
    r1 = b.send(OrderRequest("XAUUSD", Side.BUY, 0.1, 1990.0, None, "t", "g1"))
    r2 = b.send(OrderRequest("XAUUSD", Side.SELL, 0.1, 2010.0, None, "t", "g2"))
    assert r1.fill_price == pytest.approx(2000.20)
    assert r2.fill_price == pytest.approx(2000.00)


def test_same_bar_sl_and_tp_counts_as_loss() -> None:
    b = broker_with_price(2000.0)
    b.send(OrderRequest("XAUUSD", Side.BUY, 0.1, 1995.0, 2010.0, "t", "g"))
    b.on_bar("XAUUSD", bar(T0 + M15, 2000, 2015, 1990, 2005))
    assert b.closed[0].exit_reason == "sl"
    assert b.closed[0].exit_price == pytest.approx(1995.0)


def test_gap_through_stop_fills_at_open() -> None:
    b = broker_with_price(2000.0)
    b.send(OrderRequest("XAUUSD", Side.BUY, 0.1, 1995.0, None, "t", "g"))
    b.on_bar("XAUUSD", bar(T0 + M15, 1980, 1985, 1975, 1982))
    assert b.closed[0].exit_price == pytest.approx(1980.0)
    assert b.closed[0].r_multiple < -1   # แย่กว่า 1R ได้จริง


def test_short_stop_triggers_on_ask() -> None:
    b = broker_with_price(2000.0)
    b.send(OrderRequest("XAUUSD", Side.SELL, 0.1, 2005.0, None, "t", "g"))
    # bid high 2004.85 + spread 0.20 = ask 2005.05 ≥ SL
    b.on_bar("XAUUSD", bar(T0 + M15, 2000, 2004.85, 1999, 2000))
    assert b.closed and b.closed[0].exit_reason == "sl"


def test_balance_equals_initial_plus_pnl() -> None:
    b = broker_with_price(2000.0)
    b.send(OrderRequest("XAUUSD", Side.BUY, 0.5, 1990.0, 2010.0, "t", "g"))
    b.on_bar("XAUUSD", bar(T0 + M15, 2001, 2012, 2000, 2011))
    assert b.balance() == pytest.approx(10_000 + sum(t.pnl for t in b.closed))
    assert b.closed[0].pnl == pytest.approx((2010.0 - 2000.20) * 0.5 * 100)


def test_invalid_stops_rejected() -> None:
    b = broker_with_price(2000.0)
    r = b.send(OrderRequest("XAUUSD", Side.BUY, 0.1, 2001.0, None, "t", "g"))
    assert not r.accepted and r.reason is RejectReason.INVALID_STOPS


# ------------------------------------------------------------------ OrderGuard
def _sig(key: str = "xau_bo:2025-01-15") -> EnterSignal:
    return EnterSignal("XAUUSD", Side.BUY, 1990.0, None, "xau_bo", key)


def test_guard_blocks_duplicate_after_accept() -> None:
    g = OrderGuard()
    assert g.check(_sig(), T0, T0, 10_000) == ""
    g.on_reply(_sig(), OrderReply(True, ticket=1), T0, 10_000)
    assert g.check(_sig(), T0 + M15, T0 + M15, 10_000) == "duplicate"
    assert g.check(_sig("xau_bo:2025-01-16"), T0 + M15, T0 + M15, 10_000) == ""


def test_guard_stale_data() -> None:
    assert OrderGuard().check(_sig(), T0, T0 - timedelta(minutes=30), 10_000) == "stale_data"


def test_guard_no_money_clears_when_equity_rises() -> None:
    g = OrderGuard()
    g.on_reply(_sig(), OrderReply(False, RejectReason.NO_MONEY), T0, 10_000)
    assert g.check(_sig(), T0 + M15, T0 + M15, 10_000).startswith("backoff")
    assert g.check(_sig(), T0 + M15, T0 + M15, 10_500) == ""


def test_guard_exponential_backoff_on_requote() -> None:
    g = OrderGuard()
    g.on_reply(_sig(), OrderReply(False, RejectReason.PRICE_CHANGED), T0, 10_000)
    g.on_reply(_sig(), OrderReply(False, RejectReason.PRICE_CHANGED), T0, 10_000)
    # ครั้งที่ 2 → พัก 4 วินาที
    assert g.check(_sig(), T0 + timedelta(seconds=3), T0, 10_000).startswith("backoff")
    assert g.check(_sig(), T0 + timedelta(seconds=5), T0, 10_000) == ""


def test_guard_invalid_stops_consumes_signal() -> None:
    g = OrderGuard()
    g.on_reply(_sig(), OrderReply(False, RejectReason.INVALID_STOPS), T0, 10_000)
    assert g.check(_sig(), T0, T0, 10_000) == "duplicate"


# ------------------------------------------------------------------ เพดานขาดทุน
def test_daily_halt_and_kill_switch() -> None:
    b = broker_with_price(2000.0)
    rm = RiskManager(RiskConfig(daily_loss_limit=0.03, max_drawdown=0.05), b)
    rm.on_time(T0)
    b.send(OrderRequest("XAUUSD", Side.BUY, 1.0, 1900.0, None, "t", "g"))   # 1 lot = 100 USD/ดอลลาร์
    b.on_bar("XAUUSD", bar(T0 + M15, 1997, 1998, 1996, 1997))             # -3.20 * 100 = -320 (-3.2%)
    assert rm.on_time(T0 + M15) == ["day_halt"]
    assert rm.can_open(_sig(), T0 + M15) == "daily_halt"
    b.on_bar("XAUUSD", bar(T0 + 2 * M15, 1995, 1995, 1994, 1994.5))         # -5.7%
    assert "kill" in rm.on_time(T0 + 2 * M15)
    # วันใหม่: ปลด daily halt แต่ kill ยังอยู่
    rm.on_time(T0 + timedelta(days=1))
    assert rm.can_open(_sig(), T0 + timedelta(days=1)) == "killed"


# ------------------------------------------------------------------ ข่าว
def test_nfp_is_first_friday_830_new_york() -> None:
    t = [x for x in nfp_times(2025, 2025) if x.month == 1][0]
    assert t == datetime(2025, 1, 3, 13, 30, tzinfo=UTC)   # 08:30 EST = 13:30 UTC


def test_news_blackout_window() -> None:
    ev = datetime(2025, 1, 3, 13, 30, tzinfo=UTC)
    cal = NewsCalendar([ev], timedelta(minutes=30))
    assert cal.in_blackout(ev - timedelta(minutes=29))
    assert cal.in_blackout(ev + timedelta(minutes=30))
    assert not cal.in_blackout(ev + timedelta(minutes=31))


def test_account_floor_blocks_test_bots_only_when_real_equity_low() -> None:
    b = broker_with_price()
    real = {"eq": 1000.0}
    rm = RiskManager(RiskConfig(account_floor=600.0, daily_loss_limit=1.0, max_drawdown=1.0), b,
                     real_equity=lambda: real["eq"])
    assert rm.can_open(_sig(), T0) == ""
    real["eq"] = 590.0
    assert rm.can_open(_sig(), T0) == "account_floor"
    assert RiskManager(RiskConfig(), b, real_equity=lambda: 1.0).can_open(_sig(), T0) == ""   # floor 0 = ปิด


def test_swap_accrues_per_calendar_night() -> None:
    from dataclasses import replace
    spec = replace(XAU, swap_long_rate=-5 * 365 / 200_000, swap_short_rate=0.01)   # มูลค่า 2000 × 100 oz → $5/lot/คืน
    b = SimBroker({"XAUUSD": spec}, 10_000.0, timedelta(days=1))
    mon = datetime(2025, 1, 6, tzinfo=UTC)
    b.on_bar("XAUUSD", bar(mon, 2000, 2000, 2000, 2000))
    b.send(OrderRequest("XAUUSD", Side.BUY, 1.0, 1900.0, None, "t", "g"))
    for i in range(1, 8):                                          # อังคาร → จันทร์ถัดไป = 7 คืน
        b.on_bar("XAUUSD", bar(mon + timedelta(days=i), 2000, 2001, 1999, 2000))
    b.close(1, "end")
    t = b.closed[0]
    price_pnl = (2000.0 - 2000.20) * 1.0 * 100
    assert t.pnl == pytest.approx(price_pnl - 5.0 * 7)
