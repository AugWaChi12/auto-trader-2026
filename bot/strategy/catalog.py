"""
catalog.py — สร้างกลยุทธ์จาก "ชื่อตระกูล + พารามิเตอร์" (ใช้ร่วมกัน: research/m5_lab.py ↔ บอท live จาก bot/m5_bots.json)

ตระกูลเดียวกันต้องได้กลยุทธ์ตัวเดียวกันเป๊ะทั้งตอนวิจัยและตอนเทรดจริง — จึงรวมการตั้งค่าเสริมไว้ที่นี่ที่เดียว
"""
from __future__ import annotations

from dataclasses import replace

from .base import ManagementPlan, SessionWindow, Strategy
from .daily_trend import DailyTrend, DailyTrendConfig
from .level_sweep import LevelSweep, LevelSweepConfig
from .microstructure import (FairValueGap, FairValueGapConfig, RsiDivergence, RsiDivergenceConfig, VwapReversion,
                             VwapReversionConfig)
from .narrow_range import NarrowRange, NarrowRangeConfig, TrendDay, TrendDayConfig
from .scalp import (BollingerFade, BollingerFadeConfig, EmaCross, EmaCrossConfig, RsiReversion, RsiReversionConfig,
                    ScalpWindow)
from .session_breakout import BreakoutConfig, SessionBreakout
from .session_momentum import MomentumConfig, SessionMomentum
from .trend_pullback import PullbackConfig, TrendPullback
from .vol_expansion import VolExpansion, VolExpansionConfig

ALL_DAY = ScalpWindow(0, 24)
GOLD_SESSION = SessionWindow("UTC", "00:00", "07:00", "16:00", "20:00")
NAS_SESSION = SessionWindow("America/New_York", "09:30", "10:00", "12:00", "15:45")
PULLBACK_WINDOW = SessionWindow("UTC", "00:00", "00:15", "20:45", "20:50")   # เกือบทั้งวัน ปิดก่อน rollover
NAS_MOMO_TIME = {"11:00": "10:30", "12:00": "11:00", "13:00": "12:00", "14:00": "13:00"}

# ชื่อตระกูล: (Config, Strategy, รองรับ side_mode สุ่มทิศ)
FAMILIES: dict[str, tuple] = {
    "rsi2": (RsiReversionConfig, RsiReversion, True),
    "ema": (EmaCrossConfig, EmaCross, True),
    "bb": (BollingerFadeConfig, BollingerFade, True),
    "donchian": (DailyTrendConfig, DailyTrend, True),
    "pullback": (PullbackConfig, TrendPullback, False),
    "volx": (VolExpansionConfig, VolExpansion, True),
    "levels": (LevelSweepConfig, LevelSweep, True),
    "orb": (BreakoutConfig, SessionBreakout, False),
    "momo": (MomentumConfig, SessionMomentum, True),
    "vwap": (VwapReversionConfig, VwapReversion, True),
    "fvg": (FairValueGapConfig, FairValueGap, True),
    "div": (RsiDivergenceConfig, RsiDivergence, True),
    "nr": (NarrowRangeConfig, NarrowRange, True),
    "tday": (TrendDayConfig, TrendDay, True),
}
LABELS = {
    "rsi2": "RSI(2) สวนสุดโต่ง", "ema": "EMA ตัดกัน", "bb": "Bollinger สวนขอบ", "donchian": "Donchian ทะลุกรอบ",
    "pullback": "ย่อในเทรนด์", "volx": "ทะลุช่วงเงียบ", "levels": "กวาด high/low สำคัญ", "orb": "ทะลุกรอบเปิดตลาด",
    "momo": "โมเมนตัมช่วงเช้า", "vwap": "VWAP ดึงกลับค่าเฉลี่ย", "fvg": "FVG ย่อแตะช่องว่าง", "div": "RSI Divergence",
}


def build(family: str, symbol: str, params: dict, plan: ManagementPlan | None, timeframe: str, tag: str,
          mode: str = "rule") -> Strategy:
    cfg_cls, strat_cls, _ = FAMILIES[family]
    kw = dict(params)
    skip_regimes, skip_sessions = tuple(kw.pop("skip_regimes", ()) or ()), tuple(kw.pop("skip_sessions", ()) or ())
    gate_mode = kw.pop("gate_mode", "active")
    gold = symbol != "NAS100"          # ทอง / BTC ใช้กรอบเวลา UTC · NAS ใช้เวลานิวยอร์ก
    if family in ("rsi2", "ema", "bb", "vwap", "fvg", "div"):
        kw.update(window=ALL_DAY, side_mode=mode)
    elif family in ("donchian", "volx", "nr", "tday"):
        kw.update(side_mode=mode)
    elif family == "levels":
        kw.update(side_mode=mode, tz="UTC" if gold else "America/New_York")
    elif family == "pullback":
        kw.update(window=PULLBACK_WINDOW)
    elif family == "orb":
        kw.update(window=GOLD_SESSION if gold else NAS_SESSION)
    elif family == "momo":
        kw.update(side_mode="follow" if mode == "rule" else mode, tz="UTC" if gold else "America/New_York",
                  measure_start="00:00" if gold else "09:30", flat_time="20:00" if gold else "15:45")
        if not gold:   # เวลาตัดสินของ NAS นับตามนิวยอร์ก (ช่วงเช้าตลาดหุ้น)
            kw["entry_time"] = NAS_MOMO_TIME.get(kw["entry_time"], kw["entry_time"])
    cfg = cfg_cls(tag=tag, symbol=symbol, **kw)
    if plan is not None:
        cfg = replace(cfg, plan=plan)
    strat = strat_cls(cfg, timeframe)
    if skip_regimes or skip_sessions:          # ตัวกรองที่งานวิจัยยืนยันแล้ว (strategy/gate.py)
        from .gate import Gate
        return Gate(strat, skip_regimes, skip_sessions, gate_mode)
    return strat
