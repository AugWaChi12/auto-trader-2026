"""ทดสอบกลยุทธ์ด้วยแท่งที่สร้างเองแบบรู้คำตอบล่วงหน้า + ทดสอบ Engine ครบวงจร"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from bot.backtest.sim_broker import SimBroker
from bot.core.timeutil import UTC
from bot.core.types import Bar, EnterSignal, ExitSignal, Side, SymbolSpec
from bot.engine import Engine
from bot.execution.order_guard import OrderGuard
from bot.risk.risk_manager import RiskConfig, RiskManager
from bot.strategy.base import ManagementPlan, SessionWindow
from bot.strategy.session_breakout import BreakoutConfig, SessionBreakout
from bot.strategy.sweep_reversal import SweepConfig, SweepReversal

M15 = timedelta(minutes=15)
WIN = SessionWindow(tz="UTC", range_start="00:00", range_end="07:00", trade_end="16:00", flat_time="20:00")
XAU = SymbolSpec(name="XAUUSD", point=0.01, contract_size=100, min_spread=0.2, margin_rate=0.01)


def flat_day(d: date, price: float, width: float) -> list[Bar]:
    """วันที่ราคาแกว่งในกรอบ width รอบ price ทั้งวัน (ใช้อุ่น ADR)"""
    out = []
    t = datetime(d.year, d.month, d.day, tzinfo=UTC)
    for _ in range(96):
        out.append(Bar(t, price, price + width / 2, price - width / 2, price))
        t += M15
    return out


def warmup(n_days: int = 10, width: float = 20.0) -> list[Bar]:
    bars, d = [], date(2025, 1, 6)   # จันทร์
    while len(bars) < n_days * 96:
        if d.weekday() < 5:
            bars += flat_day(d, 2000.0, width)
        d += timedelta(days=1)
    return bars


def asian_range_then(d: date, after: list[tuple[float, float, float, float]]) -> list[Bar]:
    """กรอบเอเชีย 1995–2005 (กว้าง 10 = 0.5 ADR) แล้วตามด้วยแท่ง OHLC ที่กำหนด ตั้งแต่ 07:00"""
    t = datetime(d.year, d.month, d.day, tzinfo=UTC)
    bars = []
    for _ in range(28):   # 00:00–07:00
        bars.append(Bar(t, 2000, 2005, 1995, 2000))
        t += M15
    for o, h, l, c in after:
        bars.append(Bar(t, o, h, l, c))
        t += M15
    return bars


class _NoPositions:
    def positions(self, symbol=None, tag=None):
        return []


def run(strategy, bars) -> list:
    sigs = []
    for b in bars:
        sigs += strategy.on_bar(b, _NoPositions())
    return sigs


def test_breakout_fires_on_close_above_range() -> None:
    s = SessionBreakout(BreakoutConfig("bo", "XAUUSD", WIN, buffer_adr=0.0), "M15")
    day = date(2025, 1, 20)
    sigs = run(s, warmup() + asian_range_then(day, [(2000, 2004, 1999, 2003), (2003, 2008, 2002, 2007)]))
    enters = [x for x in sigs if isinstance(x, EnterSignal)]
    assert len(enters) == 1
    e = enters[0]
    assert e.side is Side.BUY and e.sl == 2000.0 and e.key == f"bo:{day}"


def test_breakout_skips_when_range_too_wide() -> None:
    s = SessionBreakout(BreakoutConfig("bo", "XAUUSD", WIN, max_range_adr=0.4), "M15")
    sigs = run(s, warmup() + asian_range_then(date(2025, 1, 20), [(2003, 2010, 2002, 2009)]))
    assert not sigs   # กรอบกว้าง 0.5 ADR > 0.4


def test_breakout_no_signal_before_range_complete() -> None:
    s = SessionBreakout(BreakoutConfig("bo", "XAUUSD", WIN), "M15")
    t = datetime(2025, 1, 20, 3, 0, tzinfo=UTC)
    sigs = run(s, warmup() + [Bar(t, 2000, 2050, 1999, 2049)])
    assert not sigs


def test_sweep_sells_after_wick_above_range() -> None:
    s = SweepReversal(SweepConfig("sw", "XAUUSD", WIN, min_rr=1.5), "M15")
    # ADR = 20 → ไส้ขึ้นไป 2006 (เลยขอบ 2005) แล้วปิดกลับ 2002
    # SELL, SL = 2006 + 0.03*20 = 2006.6, TP = 1995 → rr = 7 / 4.6 = 1.52
    sigs = run(s, warmup() + asian_range_then(date(2025, 1, 20), [(2004, 2006, 2001, 2002)]))
    enters = [x for x in sigs if isinstance(x, EnterSignal)]
    assert len(enters) == 1
    e = enters[0]
    assert e.side is Side.SELL and e.tp == 1995 and e.sl > 2006


def test_sweep_dead_after_real_breakout() -> None:
    s = SweepReversal(SweepConfig("sw", "XAUUSD", WIN, max_bars_outside=2, min_rr=1.0), "M15")
    after = [(2004, 2008, 2004, 2007), (2007, 2010, 2006, 2009), (2009, 2009, 2002, 2003)]
    sigs = run(s, warmup() + asian_range_then(date(2025, 1, 20), after))
    assert not [x for x in sigs if isinstance(x, EnterSignal)]


def test_flat_time_emits_exit_when_in_position() -> None:
    class _HasPos:
        def positions(self, symbol=None, tag=None):
            return [object()]
    s = SessionBreakout(BreakoutConfig("bo", "XAUUSD", WIN), "M15")
    t = datetime(2025, 1, 20, 19, 45, tzinfo=UTC)
    assert isinstance(s.on_bar(Bar(t, 2000, 2001, 1999, 2000), _HasPos())[0], ExitSignal)


# ------------------------------------------------------------------ Engine ครบวงจร
def _engine(plan: ManagementPlan) -> tuple[Engine, SimBroker]:
    broker = SimBroker({"XAUUSD": XAU}, 10_000.0, M15)
    strat = SessionBreakout(BreakoutConfig("bo", "XAUUSD", WIN, buffer_adr=0.0, plan=plan), "M15")
    eng = Engine(broker, broker, broker, [strat], RiskManager(RiskConfig(), broker), OrderGuard(),
                 broker.last_bar_close)
    return eng, broker


def _feed(eng: Engine, broker: SimBroker, bars: list[Bar]) -> None:
    for b in bars:
        broker.on_bar("XAUUSD", b)
        eng.on_bar("XAUUSD", b)


def test_engine_one_trade_per_day_and_partial_then_breakeven() -> None:
    eng, broker = _engine(ManagementPlan(partial_r=1.0, partial_frac=0.5, be_offset_r=0.0))
    day = date(2025, 1, 20)
    after = [
        (2000, 2006, 1999, 2006),    # breakout → เข้า BUY ~2006.2, SL 2000 (R ≈ 6.2)
        (2006, 2013, 2005, 2012),    # ถึง 1R → partial TP
        (2012, 2013, 2011, 2012),    # PositionManager เลื่อน runner ไป BE
        (2012, 2012, 2005, 2006),    # ย้อนลงมาโดน BE
        (2006, 2007, 2005, 2007),    # ยังทะลุกรอบอยู่ แต่ key วันนี้ใช้ไปแล้ว → ห้ามเข้าซ้ำ
    ]
    _feed(eng, broker, warmup() + asian_range_then(day, after))
    roles = sorted((t.role, t.exit_reason) for t in broker.closed)
    assert roles == [("partial", "tp"), ("runner", "be")]
    assert not broker.positions()
    assert len({t.group_id for t in broker.closed}) == 1
    sent = [s for s in eng.stats.signals if s.outcome == "sent"]
    assert len(sent) == 1
    assert broker.balance() > 10_000   # partial กำไร, runner เสมอตัว (ลบสเปรดนิดหน่อย)


def test_engine_size_matches_risk() -> None:
    eng, broker = _engine(ManagementPlan(partial_r=None))
    _feed(eng, broker, warmup() + asian_range_then(date(2025, 1, 20), [(2000, 2006, 1999, 2006)]))
    pos = broker.positions()[0]
    loss_if_sl = (pos.entry_price - pos.sl) * pos.volume * XAU.contract_size
    assert loss_if_sl <= 100.0 + 1e-6          # ไม่เกิน 1% ของ 10,000
    assert loss_if_sl > 100.0 - 6.2 * 1 * 1   # และไม่ต่ำกว่านั้นเกิน 1 step (0.01 lot)


def test_engine_send_exception_is_failsafe() -> None:
    """ส่งออเดอร์แล้วเจอ exception (สถานะไม่แน่นอน) → ต้องจดสัญญาณว่าใช้แล้ว ไม่ส่งซ้ำแท่งถัดไป"""
    eng, broker = _engine(ManagementPlan(partial_r=None))
    calls = {"n": 0}
    real_send = broker.send

    def boom(req):  # noqa: ANN001, ANN202
        calls["n"] += 1
        real_send(req)                      # ออเดอร์ถึง server แล้ว แต่ฝั่งเราเจอ error ก่อนได้คำตอบ
        raise ConnectionError("timeout after send")
    eng.executor = type("X", (), {"send": staticmethod(boom), "close": broker.close, "modify": broker.modify})()
    day = date(2025, 1, 20)
    _feed(eng, broker, warmup() + asian_range_then(day, [(2000, 2006, 1999, 2006), (2006, 2008, 2005, 2007)]))
    assert calls["n"] == 1
    assert any(s.outcome == "send_error" for s in eng.stats.signals)


def test_position_manager_locks_profit_trails_and_extends_tp() -> None:
    """M1 สายซิ่ง: +0.5R → ล็อกกำไร, จากนั้น trail SL, ราคาถึง 70% ของ TP → ขยับ TP ออก 0.5R"""
    from bot.execution.position_manager import PositionManager
    from bot.core.types import OrderRequest
    broker = SimBroker({"XAUUSD": XAU}, 10_000.0, M15)
    plan = ManagementPlan(partial_r=0.5, partial_frac=1.0, be_offset_r=0.1, trail_atr_mult=1.0, trail_start_r=0.5,
                          tp_extend_at=0.7, tp_extend_r=0.5)
    pm = PositionManager({"t": plan}, broker, broker, atr_period=2)
    t = datetime(2025, 1, 6, 8, tzinfo=UTC)
    for i in range(3):
        b = Bar(t + i * M15, 2000, 2000.5, 1999.5, 2000)
        broker.on_bar("XAUUSD", b)
        pm.on_bar("XAUUSD", b)
    broker.send(OrderRequest("XAUUSD", Side.BUY, 0.1, 1990.0, 2010.0, "t", "g", "single"))   # entry 2000.2, R ≈ 10.2
    pos = broker.positions()[0]
    b = Bar(t + 3 * M15, 2000, 2008, 2000, 2008)          # +0.76R และถึง 76% ของ TP
    broker.on_bar("XAUUSD", b)
    pm.on_bar("XAUUSD", b)
    assert pos.sl > pos.entry_price                         # ล็อกกำไรแล้ว (BE + trail)
    assert pos.tp > 2010.0                                  # TP ถูกขยับออก


def test_engine_tp_usd_sets_dollar_take_profit_by_actual_volume() -> None:
    """TP = กำไร $0.6 ต่อไม้ ไม่ว่าขนาดไม้เท่าไร"""
    eng, broker = _engine(ManagementPlan(partial_r=None, tp_usd=0.6))
    _feed(eng, broker, warmup() + asian_range_then(date(2025, 1, 20), [(2000, 2006, 1999, 2006)]))
    pos = broker.positions()[0]
    gross_at_tp = (pos.tp - pos.entry_price) * pos.volume * XAU.contract_size
    assert gross_at_tp == pytest.approx(0.6, abs=1e-6)
