"""ทดสอบชิ้นส่วนของ regime classifier ด้วยข้อมูลที่รู้คำตอบ"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from bot.core.regime import ADX, BarAggregator, Regime, RegimeClassifier, RegimeConfig, RollingPercentile
from bot.core.timeutil import UTC
from bot.core.types import Bar

T0 = datetime(2025, 1, 6, tzinfo=UTC)


def test_aggregator_emits_closed_h1_only() -> None:
    agg = BarAggregator(60)
    m15 = [Bar(T0 + timedelta(minutes=15 * i), 10 + i, 20 + i, 5 + i, 11 + i) for i in range(5)]
    out = [agg.update(b) for b in m15]
    assert out[:4] == [None] * 4
    h1 = out[4]
    assert h1.time == T0 and h1.open == 10 and h1.high == 23 and h1.low == 5 and h1.close == 14


def test_rolling_percentile() -> None:
    p = RollingPercentile(5)
    assert [p.update(x) for x in (1, 2, 3, 4)] == [None] * 4
    assert p.update(5) == 1.0          # สูงสุดในหน้าต่าง
    assert p.update(0) == 0.0          # ต่ำสุด (1 ถูกดันออกไปแล้ว)


def h1_series(step: float, n: int, noise: float = 0.0, seed: int = 1) -> list[Bar]:
    rnd = random.Random(seed)
    out, c = [], 100.0
    for i in range(n):
        o = c
        c = o + step + rnd.gauss(0, noise)
        out.append(Bar(T0 + timedelta(hours=i), o, max(o, c) + 0.2, min(o, c) - 0.2, c))
    return out


def test_adx_high_in_steady_trend_low_in_noise() -> None:
    a = ADX(14)
    for b in h1_series(1.0, 100):
        a.update(b)
    assert a.value > 50 and a.plus_di > a.minus_di
    # แกว่งรอบค่าคงที่ (ไม่ใช่ random walk — random walk เกิดเทรนด์ได้จริงและ ADX สูงได้)
    rnd = random.Random(3)
    a2 = ADX(14)
    for i in range(300):
        o, c = 100 + rnd.gauss(0, 1), 100 + rnd.gauss(0, 1)
        a2.update(Bar(T0 + timedelta(hours=i), o, max(o, c) + 0.2, min(o, c) - 0.2, c))
    assert a2.value < 25


@pytest.mark.parametrize("step, expected", [(1.0, Regime.TREND_UP), (-1.0, Regime.TREND_DOWN)])
def test_classifier_detects_trend(step: float, expected: Regime) -> None:
    clf = RegimeClassifier(RegimeConfig(vol_window=50))
    st = None
    for b in h1_series(step, 200, noise=0.3):
        st = clf.update_htf(b)
    assert st.regime is expected


def test_classifier_high_vol_overrides_trend() -> None:
    clf = RegimeClassifier(RegimeConfig(vol_window=50))
    bars = h1_series(0.2, 200, noise=0.2)
    for b in bars:
        clf.update_htf(b)
    t = bars[-1].time
    c = bars[-1].close
    for i in range(1, 30):                       # ความผันผวนพุ่ง 20 เท่า
        st = clf.update_htf(Bar(t + timedelta(hours=i), c, c + 20, c - 20, c + (5 if i % 2 else -5)))
    assert st.regime is Regime.HIGH_VOL
