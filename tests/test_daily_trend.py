"""T1 Donchian breakout: เข้าเมื่อปิดทะลุกรอบ N วัน, ออกเมื่อหลุดกรอบ N/2 วันฝั่งตรงข้าม"""
from __future__ import annotations

from datetime import datetime, timedelta

from bot.core.timeutil import UTC
from bot.core.types import Bar, EnterSignal, ExitSignal, Position, Side
from bot.strategy.daily_trend import DailyTrend, DailyTrendConfig

D = timedelta(days=1)
T0 = datetime(2020, 1, 1, tzinfo=UTC)


class _Acct:
    def __init__(self, pos=()):  # noqa: ANN001
        self.pos = list(pos)

    def positions(self, symbol=None, tag=None):  # noqa: ANN001, ANN201
        return self.pos


def flat(n: int) -> list[Bar]:
    return [Bar(T0 + i * D, 100, 101, 99, 100) for i in range(n)]


def test_breakout_long_with_atr_stop() -> None:
    s = DailyTrend(DailyTrendConfig("dt", "XAUUSD", entry_n=20, stop_atr=2.0), "D1")
    for b in flat(30):
        assert not s.on_bar(b, _Acct())
    sig = s.on_bar(Bar(T0 + 30 * D, 100, 103, 100, 102), _Acct())   # ปิด 102 > high 20 วัน (101)
    assert len(sig) == 1 and isinstance(sig[0], EnterSignal) and sig[0].side is Side.BUY
    assert sig[0].tp is None and 102 - sig[0].sl > 3.5                 # SL = 2 × ATR(≈2)


def test_exit_on_opposite_half_channel() -> None:
    s = DailyTrend(DailyTrendConfig("dt", "XAUUSD", entry_n=20, stop_atr=2.0), "D1")
    for b in flat(30):
        s.on_bar(b, _Acct())
    pos = Position(1, "XAUUSD", Side.BUY, 0.01, 102, 98, None, T0, "dt", "g", "single", 98)
    assert not s.on_bar(Bar(T0 + 30 * D, 100, 101, 99.5, 100), _Acct([pos]))   # ยังอยู่ในกรอบ 10 วัน
    out = s.on_bar(Bar(T0 + 31 * D, 100, 100, 98, 98.5), _Acct([pos]))        # ปิดต่ำกว่า low 10 วัน (99)
    assert isinstance(out[0], ExitSignal)


def test_no_signal_before_enough_history() -> None:
    s = DailyTrend(DailyTrendConfig("dt", "XAUUSD", entry_n=50), "D1")
    for b in flat(30):
        assert not s.on_bar(b, _Acct())
    assert not s.on_bar(Bar(T0 + 30 * D, 100, 150, 100, 149), _Acct())
