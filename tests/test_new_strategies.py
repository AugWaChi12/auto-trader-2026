"""ทดสอบ SessionMomentum / TrendPullback / HigherTimeframeClose ด้วยแท่งที่รู้คำตอบล่วงหน้า"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from bot.core.timeutil import UTC
from bot.core.types import Bar, EnterSignal, Side
from bot.strategy.base import SessionWindow
from bot.strategy.narrow_range import NarrowRange, NarrowRangeConfig, TrendDay, TrendDayConfig
from bot.strategy.session_momentum import MomentumConfig, SessionMomentum
from bot.strategy.trend_pullback import HigherTimeframeClose, PullbackConfig, TrendPullback

from .test_strategies import _NoPositions, warmup

M15 = timedelta(minutes=15)


def morning(d: date, end_price: float, after: int = 2) -> list[Bar]:
    """00:00 → 12:00 ราคาเดินเส้นตรงจาก 2000 ไป end_price แล้วต่ออีก `after` แท่ง"""
    t = datetime(d.year, d.month, d.day, tzinfo=UTC)
    n = 48
    bars = []
    for i in range(n + after):
        c = 2000 + (end_price - 2000) * min(i + 1, n) / n
        o = 2000 + (end_price - 2000) * min(i, n) / n
        bars.append(Bar(t, o, max(o, c) + 0.1, min(o, c) - 0.1, c))
        t += M15
    return bars


def run(strategy, bars) -> list[EnterSignal]:
    out = []
    for b in bars:
        out += [s for s in strategy.on_bar(b, _NoPositions()) if isinstance(s, EnterSignal)]
    return out


def test_momentum_follows_strong_morning() -> None:
    s = SessionMomentum(MomentumConfig("m", "XAUUSD", min_move_adr=0.35, sl_adr=0.5), "M15")
    sigs = run(s, warmup() + morning(date(2025, 1, 20), 2010))      # +10 = 0.5 ADR (ADR = 20)
    assert len(sigs) >= 1
    e = sigs[0]
    assert e.side is Side.BUY and abs(e.sl - (2010 - 10)) < 1e-9


def test_momentum_short_on_down_morning_and_skip_small_move() -> None:
    s = SessionMomentum(MomentumConfig("m", "XAUUSD", min_move_adr=0.35), "M15")
    assert run(s, warmup() + morning(date(2025, 1, 20), 1990))[0].side is Side.SELL
    s2 = SessionMomentum(MomentumConfig("m", "XAUUSD", min_move_adr=0.35), "M15")
    assert not run(s2, warmup() + morning(date(2025, 1, 20), 2003))  # 0.15 ADR < 0.35


def test_momentum_benchmark_long_ignores_direction() -> None:
    s = SessionMomentum(MomentumConfig("m", "XAUUSD", min_move_adr=0.35, side_mode="long"), "M15")
    assert run(s, warmup() + morning(date(2025, 1, 20), 1990))[0].side is Side.BUY


def test_htf_close_only_after_bucket_closes() -> None:
    h = HigherTimeframeClose(240)
    t = datetime(2025, 1, 6, tzinfo=UTC)
    outs = [h.update(Bar(t + i * M15, 0, 0, 0, float(i))) for i in range(17)]
    assert outs[:16] == [None] * 16          # แท่ง 00:00–03:45 ยังอยู่ใน bucket แรก
    assert outs[16] == 15.0                  # แท่ง 04:00 มาถึง → bucket แรกปิดที่ close ของแท่ง 03:45


def test_pullback_buys_touch_of_ema_in_uptrend() -> None:
    win = SessionWindow("UTC", "06:45", "07:00", "17:00", "20:00")
    s = TrendPullback(PullbackConfig("pb", "XAUUSD", win, trend_ema=3, pullback_ema=5, sl_atr=1.0, tp_r=2.0), "M15")
    t = datetime(2025, 1, 6, tzinfo=UTC)
    bars = []
    close = 2000.0
    while t < datetime(2025, 1, 7, 8, 0, tzinfo=UTC):     # ขาขึ้นสม่ำเสมอ: low ไม่เคยแตะ EMA5
        o, close = close, close + 0.5
        bars.append(Bar(t, o, close + 0.1, o - 0.1, close))
        t += M15
    assert not run(s, bars)
    ema_before = s._ema.value
    c = close + 0.2
    ema_after = ema_before + (2 / 6) * (c - ema_before)
    sigs = run(s, [Bar(t, close, c + 0.1, ema_after - 0.5, c)])   # ย่อหลุด EMA แล้วปิดเขียวเหนือ EMA
    assert len(sigs) == 1 and sigs[0].side is Side.BUY
    assert sigs[0].tp is not None and sigs[0].tp - c == pytest.approx(2.0 * (c - sigs[0].sl))


def _day_bars(d: date, end_price: float, swing: float) -> list[Bar]:
    """00:00 → 14:30 ราคาเดินเส้นตรงจาก 2000 ไป end_price · swing = ความกว้างของแต่ละแท่ง"""
    t = datetime(d.year, d.month, d.day, tzinfo=UTC)
    bars = []
    for i in range(59):
        c = 2000 + (end_price - 2000) * (i + 1) / 58
        o = 2000 + (end_price - 2000) * i / 58
        bars.append(Bar(t, o, max(o, c) + swing, min(o, c) - swing, c))
        t += M15
    return bars


def _run_trend_day(side_price: float) -> list:
    s = TrendDay(TrendDayConfig(tag="t", symbol="XAUUSD", adr_lookback=3), "M15")
    d = date(2026, 3, 2)
    for _ in range(4):
        for b in _day_bars(d, 2000.5, 1.0):
            s.last_bar = b
            s.on_bar(b, _NoPositions())
        d += timedelta(days=1)
    out = []
    for b in _day_bars(d, side_price, 1.0):
        s.last_bar = b
        out += [x for x in s.on_bar(b, _NoPositions()) if isinstance(x, EnterSignal)]
    return out


def test_trend_day_follows_a_wide_day_and_ignores_a_normal_one() -> None:
    down = _run_trend_day(1950.0)
    assert down and down[0].side == Side.SELL and down[0].sl > 1950 and down[0].tp is None
    assert _run_trend_day(2000.5) == []


def test_narrow_range_breaks_yesterday_only_after_the_narrowest_day() -> None:
    s = NarrowRange(NarrowRangeConfig(tag="n", symbol="XAUUSD", nr_n=4), "M15")
    d = date(2026, 3, 2)
    for swing in (5.0, 5.0, 5.0, 1.0):
        for b in _day_bars(d, 2000.0, swing):
            s.last_bar = b
            s.on_bar(b, _NoPositions())
        d += timedelta(days=1)
    t = datetime(d.year, d.month, d.day, 8, tzinfo=UTC)
    inside, above = Bar(t, 2000, 2000.5, 1999.5, 2000), Bar(t + M15, 2000, 2010, 2000, 2009)
    assert s.on_bar(inside, _NoPositions()) == []
    sig = s.on_bar(above, _NoPositions())
    assert sig[0].side == Side.BUY and sig[0].sl == 2000.0
