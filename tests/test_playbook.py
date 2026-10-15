"""ทดสอบ KeyLevels / LevelSweep / VolExpansion / hold_exits ด้วยแท่งที่รู้คำตอบ"""
from __future__ import annotations

from datetime import datetime, timedelta

from bot.core.regime import Regime
from bot.core.timeutil import UTC
from bot.core.types import Bar, EnterSignal, ExitSignal, Position, Side
from bot.strategy.level_sweep import LevelSweep, LevelSweepConfig
from bot.strategy.levels import KeyLevels, hold_exits
from bot.strategy.vol_expansion import VolExpansion, VolExpansionConfig

M15 = timedelta(minutes=15)
MON = datetime(2025, 1, 6, tzinfo=UTC)


class _Acct:
    def __init__(self, positions=()):  # noqa: ANN001
        self._p = list(positions)

    def positions(self, symbol=None, tag=None):  # noqa: ANN001, ANN201
        return self._p


def day_bars(start: datetime, hi: float, lo: float, n: int = 96) -> list[Bar]:
    mid = (hi + lo) / 2
    return [Bar(start + i * M15, mid, hi, lo, mid) for i in range(n)]


def test_key_levels_skip_weekend_and_roll_days() -> None:
    k = KeyLevels("UTC")
    for b in day_bars(MON, 110, 90) + day_bars(MON + timedelta(days=1), 120, 100):
        k.update(b)
    assert (k.pdh, k.pdl) == (110, 90)
    k.update(Bar(datetime(2025, 1, 12, 23, tzinfo=UTC), 300, 301, 299, 300))   # อาทิตย์ — ต้องไม่นับ
    k.update(Bar(datetime(2025, 1, 13, 0, tzinfo=UTC), 100, 101, 99, 100))     # จันทร์สัปดาห์ใหม่
    assert k.pwh == 120 and k.pwl == 90


def test_hold_exits_time_stop_and_weekend() -> None:
    class S:
        symbol, tag = "XAUUSD", "t"
    pos = Position(1, "XAUUSD", Side.BUY, 0.01, 100, 90, None, MON, "t", "g", "single", 90)
    late = Bar(MON + timedelta(hours=24), 1, 1, 1, 1)
    assert isinstance(hold_exits(S(), late, _Acct([pos]), timedelta(hours=24), M15)[0], ExitSignal)
    early = Bar(MON + timedelta(hours=2), 1, 1, 1, 1)
    assert hold_exits(S(), early, _Acct([pos]), timedelta(hours=24), M15) == []
    fri = Bar(datetime(2025, 1, 10, 20, tzinfo=UTC), 1, 1, 1, 1)
    pos2 = Position(1, "XAUUSD", Side.BUY, 0.01, 100, 90, None, fri.time - timedelta(hours=1), "t", "g", "single", 90)
    assert hold_exits(S(), fri, _Acct([pos2]), timedelta(hours=24), M15)[0].reason == "weekend"


def _force_regime(strategy, regime: Regime, atr: float = 4.0) -> None:  # noqa: ANN001
    """ข้ามการ warmup 100 วันของ classifier: ตั้ง state/ATR ตรงๆ"""
    from types import SimpleNamespace
    strategy.clf.state = SimpleNamespace(regime=regime)
    strategy.clf._atr.value = atr
    strategy.clf.update = lambda bar: None


def test_level_sweep_sells_wick_above_pdh() -> None:
    s = LevelSweep(LevelSweepConfig("sw", "XAUUSD", max_bars=2, buffer_atr=0.25, tp_r=2.0), "M15")
    _force_regime(s, Regime.RANGE, atr=4.0)
    for b in day_bars(MON, 110, 90):
        assert not [x for x in s.on_bar(b, _Acct()) if isinstance(x, EnterSignal)]
    tue = MON + timedelta(days=1, hours=8)
    sig = s.on_bar(Bar(tue, 105, 112, 104, 108), _Acct())      # ไส้ไป 112 > PDH 110 ปิด 108
    assert len(sig) == 1 and sig[0].side is Side.SELL
    assert abs(sig[0].sl - (112 + 1.0)) < 1e-9                   # extreme + 0.25×4
    assert abs(sig[0].tp - (108 - 2 * 5.0)) < 1e-9
    assert not s.on_bar(Bar(tue + M15, 108, 113, 107, 108), _Acct())   # ระดับใช้ไปแล้ววันนี้


def test_level_sweep_skips_high_vol_and_real_breakout() -> None:
    s = LevelSweep(LevelSweepConfig("sw", "XAUUSD", max_bars=2), "M15")
    _force_regime(s, Regime.RANGE)
    for b in day_bars(MON, 110, 90):
        s.on_bar(b, _Acct())
    tue = MON + timedelta(days=1, hours=8)
    out = [s.on_bar(Bar(tue + i * M15, o, h, l, c), _Acct()) for i, (o, h, l, c) in
           enumerate([(109, 113, 109, 112), (112, 115, 111, 114), (114, 114, 106, 107)])]
    assert out == [[], [], []]                                   # อยู่นอก 2 แท่ง = ทะลุจริง
    s2 = LevelSweep(LevelSweepConfig("sw", "XAUUSD"), "M15")
    _force_regime(s2, Regime.HIGH_VOL)
    for b in day_bars(MON, 110, 90):
        s2.on_bar(b, _Acct())
    assert not s2.on_bar(Bar(tue, 105, 112, 104, 108), _Acct())


def test_vol_expansion_breaks_box_only_after_dead() -> None:
    s = VolExpansion(VolExpansionConfig("vx", "XAUUSD", box_n=3, sl_atr=1.0, tp_r=2.0), "M15")
    s.clf._atr.value = 5.0
    from types import SimpleNamespace
    states = iter([Regime.RANGE, Regime.RANGE, Regime.RANGE, Regime.DEAD, Regime.DEAD, Regime.DEAD])

    def fake_update(bar):  # noqa: ANN001, ANN202 — คืน "แท่ง H1 ปิด" ทุกแท่งที่ป้อน
        r = next(states, Regime.DEAD)
        # ของจริง: h1 ที่ส่งออกมาคือแท่งที่ "ปิดไปแล้ว" ไม่รวมแท่งปัจจุบัน → กรอบเงียบ 99–101 ตลอด
        return SimpleNamespace(regime=r, h1=SimpleNamespace(high=101, low=99))
    s.clf.update = fake_update
    t = MON + timedelta(hours=8)
    for i in range(6):
        s.on_bar(Bar(t + i * M15, 100, 101, 99, 100), _Acct())
    sig = s.on_bar(Bar(t + 6 * M15, 100, 104, 100, 103), _Acct())   # ปิด 103 > กรอบ 101
    assert sig and sig[0].side is Side.BUY and abs(sig[0].tp - sig[0].sl - 15.0) < 1e-9


def test_benchmark_random_keeps_distance_and_flips_side() -> None:
    sides = set()
    for seed in range(12):
        s = LevelSweep(LevelSweepConfig("sw", "XAUUSD", side_mode="random", seed=seed), "M15")
        _force_regime(s, Regime.RANGE)
        for b in day_bars(MON, 110, 90):
            s.on_bar(b, _Acct())
        sig = s.on_bar(Bar(MON + timedelta(days=1, hours=8), 105, 112, 104, 108), _Acct())[0]
        assert abs(abs(sig.sl - 108) - 5.0) < 1e-9
        sides.add(sig.side)
    assert sides == {Side.BUY, Side.SELL}
