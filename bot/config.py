"""
config.py — ค่าตั้งค่าทั้งหมด (แก้ที่นี่ที่เดียว)

ทุกอย่างเป็น frozen dataclass เหมือน config.py ของ loot_sandbox → อ่านอย่างเดียว แชร์ข้ามเธรดได้

!! ตัวเลขกลยุทธ์ทั้งหมดเป็น "สมมติฐานเริ่มต้น" ไม่ใช่ค่าที่พิสูจน์แล้ว !!
ปรับทีละตัว และระวัง overfit: ถ้าต้องจูนละเอียดมากถึงจะกำไร = ไม่มี edge จริง
"""
from __future__ import annotations

from pathlib import Path

from .core.types import SymbolSpec
from .risk.risk_manager import RiskConfig
from .strategy.base import ManagementPlan, SessionWindow, Strategy
from .strategy.session_breakout import BreakoutConfig, SessionBreakout
from .strategy.session_momentum import MomentumConfig, SessionMomentum
from .strategy.level_sweep import LevelSweep, LevelSweepConfig
from .strategy.trend_pullback import PullbackConfig, TrendPullback
from .strategy.vol_expansion import VolExpansion, VolExpansionConfig
from .strategy.random_entry import RandomConfig, RandomEntry
from .strategy.scalp import (BollingerFade, BollingerFadeConfig, EmaCross, EmaCrossConfig, RsiReversion,
                             RsiReversionConfig)
from .strategy.sweep_reversal import SweepConfig, SweepReversal

ROOT = Path(__file__).resolve().parent.parent
NEWS_CSV = ROOT / "config" / "news.csv"

TIMEFRAME = "M15"
INITIAL_BALANCE = 1_000.0   # เท่ากับบัญชี demo FxPro

# โบรก XAUUSD ส่วนใหญ่ใช้ GMT+2/+3 ตาม DST อเมริกา — คำสั่ง download จะตรวจให้ว่าตรงไหม
SERVER_TIME_MODE = "ny_close"

# ---------------------------------------------------------------------------- สเปคสัญญา
# point / contract_size / volume_* จะถูกทับด้วยค่าจริงจาก MT5 ตอน download
# min_spread / slippage / commission / margin_rate ต้องตั้งเองให้ตรงบัญชี (ตั้งแย่ไว้ก่อนปลอดภัยกว่า)
# ค่าปัจจุบัน: FxPro Raw+ (ค่าคอม $3.5/lot/ด้าน = $7 ไป-กลับ) — ตรวจใน MT5: คลิกขวา symbol → Specification
DEFAULT_SPECS: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec(name="XAUUSD", point=0.01, contract_size=100, volume_min=0.01, volume_step=0.01,
                         min_spread=0.25, slippage=0.05, commission_per_lot=7.0, margin_rate=0.01),
    "NAS100": SymbolSpec(name="NAS100", point=0.01, contract_size=1, volume_min=0.01, volume_step=0.01,
                         min_spread=1.5, slippage=0.5, commission_per_lot=0.0, margin_rate=0.01),
}

# ---------------------------------------------------------------------------- ความเสี่ยง
RISK_PROFILES: dict[str, RiskConfig] = {
    "normal":     RiskConfig(risk_per_trade=0.01, daily_loss_limit=0.03, max_drawdown=0.15),
    "aggressive": RiskConfig(risk_per_trade=0.02, daily_loss_limit=0.05, max_drawdown=0.20),
    # เผื่อสอบ prop firm: ต้องไม่ชนเพดาน 5%/วัน 10% รวม → ตั้งให้หยุดก่อนถึง
    "prop":       RiskConfig(risk_per_trade=0.0075, daily_loss_limit=0.035, max_drawdown=0.08,
                             day_tz="Europe/Prague"),
    # demo ทุน $1,000: SL ทองของ xau_momo ~$45 ต่อ 0.01 lot (4.5% ของทุน) → ถ้าไม่ยอม lot ขั้นต่ำจะเทรดไม่ได้เลย
    # เป้าหมายคือเก็บผลเป็น R บน demo ห้ามใช้โปรไฟล์นี้กับเงินจริง
    "demo_1k":    RiskConfig(risk_per_trade=0.01, min_lot_max_risk=0.06, daily_loss_limit=0.10, max_drawdown=0.40),
    # บอทวัดใจ: เข้าทุกเงื่อนไข ไม้ละ 0.5% หลายไม้พร้อมกัน — ทุกกลยุทธ์ในนี้ตก backtest แล้ว
    "gamble":     RiskConfig(risk_per_trade=0.005, min_lot_max_risk=0.06, daily_loss_limit=0.15, max_drawdown=0.50,
                             max_open_groups=10, max_groups_per_symbol=4),
}
NEWS_BLACKOUT_MIN = 30

# ---------------------------------------------------------------------------- live (เฟส 2: demo เท่านั้น)
LIVE_STRATEGIES = ["xau_momo"]
LIVE_PROFILE = "demo_1k"
MAGIC = 26101501                     # ติดทุกออเดอร์ของบอท — ไม่แตะไม้ที่คนเปิดเอง
POLL_SECONDS = 5
WARMUP_BARS = 3000                   # ~1 เดือนของ M15 ให้ ADR/EMA พร้อมก่อนเทรด
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"

# หลายบอทบนบัญชีเดียว: แต่ละตัวมี magic / state / log ของตัวเอง ไม่แตะไม้ของกัน
# (เพดานขาดทุนรายวันของ RiskManager คิดจาก equity ทั้งบัญชี — แล็บขาดทุนหนักอาจหยุดบอทหลักวันนั้นด้วย)
# allocation = ทุนเสมือนของบอทนั้น (sub-account): ขนาดไม้ เพดานขาดทุน kill switch คิดจากกำไรขาดทุนของบอทตัวเอง
# → บอทหนึ่งขาดทุนหนักไม่ทำให้บอทอื่นหยุด · ผลรวม allocation เกินทุนจริงได้บน demo — บัญชีจริงห้ามทำแบบนี้
# MAGIC + 3 = บอท "gamble" เดิม (เลิกใช้ 2026-10-15 แยกเป็น g_* ตามหมวด) — ห้ามนำเลขนี้กลับมาใช้
_G = {"tf": "M15", "profile": "gamble", "allocation": 1000.0}
LIVE_INSTANCES: dict[str, dict] = {
    "main":   {"strategies": LIVE_STRATEGIES, "tf": "M15", "profile": LIVE_PROFILE, "magic": MAGIC, "allocation": 1000.0},
    "lab_m5": {"strategies": ["lr_xau", "lr_nas", "ec_xau"], "tf": "M5", "profile": "demo_1k", "magic": MAGIC + 1,
               "allocation": 1000.0},
    "lab_m1": {"strategies": ["bb_xau"], "tf": "M1", "profile": "demo_1k", "magic": MAGIC + 2, "allocation": 1000.0},
    # บอทวัดใจ แยกตามหมวดเทคนิค → เห็นกำไรขาดทุนแต่ละแนวแยกกัน และมี kill switch ของตัวเอง
    # warmup 12,000 แท่ง M15 ≈ 125 วัน: regime classifier ต้องใช้ ~100 วัน · EMA50(H4) ต้องใช้ ~35 วัน
    "g_session": {**_G, "strategies": ["g_bo_x", "g_bo_n"], "magic": MAGIC + 4},
    "g_sweep":   {**_G, "strategies": ["g_sw_x", "g_sw_n", "g_ls_x", "g_ls_n"], "magic": MAGIC + 5, "warmup": 12_000},
    "g_vol":     {**_G, "strategies": ["g_vx_x", "g_vx_n"], "magic": MAGIC + 6, "warmup": 12_000},
    "g_trend":   {**_G, "strategies": ["g_pb_x", "g_pb_n"], "magic": MAGIC + 7, "warmup": 6_000},
    # กลุ่มควบคุม: เข้าแบบไม่มีสมอง ต้นทุนเดียวกัน — กลยุทธ์ที่ไม่ชนะกลุ่มนี้บน demo = ไม่มี edge
    "bench_rand": {**_G, "strategies": ["b_rnd_x", "b_rnd_n"], "magic": MAGIC + 8},
    "bench_long": {**_G, "strategies": ["b_lng_x", "b_lng_n"], "magic": MAGIC + 9},
    "bench_m5":   {**_G, "tf": "M5", "strategies": ["b_rnd5_x"], "magic": MAGIC + 10},
}


def instance_state_dir(name: str) -> Path:
    return STATE_DIR if name == "main" else STATE_DIR / name

# ---------------------------------------------------------------------------- กลยุทธ์
GOLD_SESSION = SessionWindow(tz="UTC", range_start="00:00", range_end="07:00",
                             trade_end="16:00", flat_time="20:00")
NAS_SESSION = SessionWindow(tz="America/New_York", range_start="09:30", range_end="10:00",
                            trade_end="12:00", flat_time="15:45")

# ห้องทดลองสแกลป์ (research/hypotheses_v3_scalp.md) — **ตกทั้ง 4 ตัวใน backtest** (−0.016 ถึง −0.205R)
# รันบน demo ตามคำขอเพื่อดูผลจริงกับตา · พารามิเตอร์ = ชุดที่ walk-forward เลือกในหน้าต่างล่าสุด
LAB_CONFIGS: list = [
    RsiReversionConfig(tag="lr_xau", symbol="XAUUSD", rsi_th=5.0, sl_atr=2.0, max_hold=24),
    RsiReversionConfig(tag="lr_nas", symbol="NAS100", rsi_th=5.0, sl_atr=2.0, max_hold=12),
    EmaCrossConfig(tag="ec_xau", symbol="XAUUSD", fast=12, slow=26, sl_atr=1.0, tp_r=2.0),
    BollingerFadeConfig(tag="bb_xau", symbol="XAUUSD", k=2.0, sl_atr=2.0, max_hold=15),
]

# บอทวัดใจ: ทุกกลยุทธ์ M15 ที่เคยสร้าง (research/RESULTS.md, RESULTS_v2.md — ตกทั้งหมด) เข้าทุกครั้งที่เงื่อนไขครบ
_XW = SessionWindow("UTC", "06:45", "07:00", "17:00", "20:00")
_NW = SessionWindow("America/New_York", "09:30", "09:45", "15:00", "15:45")
GAMBLE_CONFIGS: list = [
    BreakoutConfig(tag="g_bo_x", symbol="XAUUSD", window=GOLD_SESSION),
    SweepConfig(tag="g_sw_x", symbol="XAUUSD", window=GOLD_SESSION),
    BreakoutConfig(tag="g_bo_n", symbol="NAS100", window=NAS_SESSION, min_range_adr=0.10, max_range_adr=0.50),
    SweepConfig(tag="g_sw_n", symbol="NAS100", window=NAS_SESSION, min_range_adr=0.10, max_range_adr=0.60),
    VolExpansionConfig(tag="g_vx_x", symbol="XAUUSD"),
    VolExpansionConfig(tag="g_vx_n", symbol="NAS100"),
    LevelSweepConfig(tag="g_ls_x", symbol="XAUUSD", tz="UTC"),
    LevelSweepConfig(tag="g_ls_n", symbol="NAS100", tz="America/New_York"),
    PullbackConfig(tag="g_pb_x", symbol="XAUUSD", window=_XW),
    PullbackConfig(tag="g_pb_n", symbol="NAS100", window=_NW),
]

# กลุ่มควบคุม (benchmark) — สุ่มเข้าวันละไม่เกิน 1 ไม้ ในช่วงเวลาที่มีสภาพคล่อง ด้วย SL/TP มาตรฐาน
_M5W = SessionWindow("UTC", "06:45", "07:00", "17:00", "20:00")
BENCH_CONFIGS: list = [
    RandomConfig(tag="b_rnd_x", symbol="XAUUSD", window=_XW, prob_per_bar=0.05, side_mode="random", seed=11),
    RandomConfig(tag="b_rnd_n", symbol="NAS100", window=_NW, prob_per_bar=0.08, side_mode="random", seed=12),
    RandomConfig(tag="b_lng_x", symbol="XAUUSD", window=_XW, prob_per_bar=0.05, side_mode="long", seed=13),
    RandomConfig(tag="b_lng_n", symbol="NAS100", window=_NW, prob_per_bar=0.08, side_mode="long", seed=14),
    RandomConfig(tag="b_rnd5_x", symbol="XAUUSD", window=_M5W, prob_per_bar=0.02, side_mode="random", seed=15),
]

STRATEGY_CONFIGS: list[BreakoutConfig | SweepConfig | MomentumConfig] = [
    # ผลวิจัย (research/out/locked.json): in-sample +0.114R → out-of-sample +0.037R ไม่ผ่านเกณฑ์ +0.10R
    # เก็บไว้เป็นตัวเลือกทดสอบบน demo เท่านั้น — ยังไม่ใช่กลยุทธ์ที่พิสูจน์แล้ว
    MomentumConfig(tag="xau_momo", symbol="XAUUSD", entry_time="12:00", min_move_adr=0.35, sl_adr=0.5,
                   flat_time="20:00"),
    BreakoutConfig(
        tag="xau_bo", symbol="XAUUSD", window=GOLD_SESSION,
        min_range_adr=0.15, max_range_adr=0.60, buffer_adr=0.02, sl_mode="mid", tp_r=None,
        plan=ManagementPlan(partial_r=1.5, partial_frac=0.5, trail_atr_mult=2.5, trail_start_r=1.5),
    ),
    SweepConfig(
        tag="xau_sweep", symbol="XAUUSD", window=GOLD_SESSION,
        min_range_adr=0.15, max_range_adr=0.70, min_sweep_adr=0.02, sl_buffer_adr=0.03,
        max_bars_outside=3, min_rr=2.0,
        plan=ManagementPlan(partial_r=1.5, partial_frac=0.5, trail_atr_mult=None),
    ),
    BreakoutConfig(
        tag="nas_orb", symbol="NAS100", window=NAS_SESSION,
        min_range_adr=0.10, max_range_adr=0.50, buffer_adr=0.02, sl_mode="mid", tp_r=None,
        plan=ManagementPlan(partial_r=1.5, partial_frac=0.5, trail_atr_mult=2.5, trail_start_r=1.5),
    ),
    SweepConfig(
        tag="nas_sweep", symbol="NAS100", window=NAS_SESSION,
        min_range_adr=0.10, max_range_adr=0.60, min_sweep_adr=0.02, sl_buffer_adr=0.03,
        max_bars_outside=3, min_rr=2.0,
        plan=ManagementPlan(partial_r=1.5, partial_frac=0.5, trail_atr_mult=None),
    ),
]


def build_strategies(tags: list[str] | None = None, symbols: list[str] | None = None,
                     timeframe: str = TIMEFRAME) -> list[Strategy]:
    out: list[Strategy] = []
    builders = {BreakoutConfig: SessionBreakout, SweepConfig: SweepReversal, MomentumConfig: SessionMomentum,
                RsiReversionConfig: RsiReversion, EmaCrossConfig: EmaCross, BollingerFadeConfig: BollingerFade,
                VolExpansionConfig: VolExpansion, LevelSweepConfig: LevelSweep, PullbackConfig: TrendPullback,
                RandomConfig: RandomEntry}
    for c in STRATEGY_CONFIGS + LAB_CONFIGS + GAMBLE_CONFIGS + BENCH_CONFIGS:
        if tags and c.tag not in tags:
            continue
        if symbols and c.symbol not in symbols:
            continue
        if tags is None and (c in LAB_CONFIGS or c in GAMBLE_CONFIGS or c in BENCH_CONFIGS):
            continue                       # แล็บต้องเรียกชื่อตรงๆ เท่านั้น ไม่ติดไปกับ backtest ปกติ
        out.append(builders[type(c)](c, timeframe))
    return out
