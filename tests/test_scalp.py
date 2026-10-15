"""ทดสอบ indicator และกฎของห้องทดลองสแกลป์"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from bot.core.timeutil import UTC
from bot.core.types import Bar, EnterSignal, ExitSignal, Position, Side
from bot.strategy.scalp import RSI, Bollinger, BollingerFade, BollingerFadeConfig, RsiReversion, RsiReversionConfig

M5 = timedelta(minutes=5)


def test_rsi_matches_wilder_reference() -> None:
    closes = [100, 101, 99, 98, 102, 103, 101, 100, 104, 105, 103, 102, 101, 106]
    rsi = RSI(2)
    mine = [rsi.update(c) for c in closes]
    d = pd.Series(closes).diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    # Wilder: seed = ค่าเฉลี่ย n ตัวแรก แล้ว smoothing (n-1)/n
    ag, al = up[1:3].mean(), dn[1:3].mean()
    ref = [None, None, 100 - 100 / (1 + ag / al)]
    for i in range(3, len(closes)):
        ag = (ag * 1 + up[i]) / 2
        al = (al * 1 + dn[i]) / 2
        ref.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    assert mine[2:] == pytest.approx(ref[2:])


def test_bollinger() -> None:
    bb = Bollinger(4)
    assert [bb.update(x) for x in (1, 2, 3)] == [None] * 3
    m, sd = bb.update(4)
    assert m == 2.5 and sd == pytest.approx(pd.Series([1, 2, 3, 4]).std(ddof=0))


class _Acct:
    def __init__(self, pos=()):  # noqa: ANN001
        self.pos = list(pos)

    def positions(self, symbol=None, tag=None):  # noqa: ANN001, ANN201
        return self.pos


def test_rsi_reversion_buys_dip_in_uptrend_and_exits_on_ema5() -> None:
    s = RsiReversion(RsiReversionConfig("rsi", "XAUUSD", rsi_th=10, sl_atr=1.0), "M5")
    t = datetime(2025, 1, 5, 14, tzinfo=UTC)   # 250 แท่ง M5 = ~21 ชม. → แท่งทดสอบตกจันทร์ ~11:00 UTC (ในช่วงเทรด)
    c = 2000.0
    for i in range(250):                       # ขาขึ้นเรียบๆ ให้ EMA200 อยู่ใต้ราคา
        c += 0.5
        s.on_bar(Bar(t + i * M5, c - 0.5, c + 0.2, c - 0.7, c), _Acct())
    t2 = t + 250 * M5
    s.on_bar(Bar(t2, c, c, c - 3, c - 3), _Acct())
    sig = s.on_bar(Bar(t2 + M5, c - 3, c - 3, c - 6, c - 6), _Acct())   # ลงแรง 2 แท่ง → RSI2 ต่ำมาก
    assert sig and isinstance(sig[0], EnterSignal) and sig[0].side is Side.BUY
    pos = Position(1, "XAUUSD", Side.BUY, 0.01, c - 6, c - 10, None, t2 + 2 * M5, "rsi", "g", "single", c - 10)
    out = s.on_bar(Bar(t2 + 2 * M5, c - 6, c + 1, c - 6, c + 1), _Acct([pos]))   # ปิดเหนือ EMA5
    assert isinstance(out[0], ExitSignal) and out[0].reason == "ema5"


def test_bollinger_fade_only_in_asia_window() -> None:
    s = BollingerFade(BollingerFadeConfig("bb", "XAUUSD", k=2.0), "M1")
    t = datetime(2025, 1, 6, 1, tzinfo=UTC)
    for i in range(30):
        s.on_bar(Bar(t + i * timedelta(minutes=1), 100, 100.5, 99.5, 100 + (0.1 if i % 2 else -0.1)), _Acct())
    sig = s.on_bar(Bar(t + 30 * timedelta(minutes=1), 100, 100, 97, 97), _Acct())
    assert sig and sig[0].side is Side.BUY and sig[0].tp > 97
    s2 = BollingerFade(BollingerFadeConfig("bb", "XAUUSD", k=2.0), "M1")
    t3 = datetime(2025, 1, 6, 10, tzinfo=UTC)               # นอกช่วงเอเชีย
    for i in range(30):
        s2.on_bar(Bar(t3 + i * timedelta(minutes=1), 100, 100.5, 99.5, 100 + (0.1 if i % 2 else -0.1)), _Acct())
    assert not s2.on_bar(Bar(t3 + 30 * timedelta(minutes=1), 100, 100, 97, 97), _Acct())
