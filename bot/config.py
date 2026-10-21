"""
config.py — ค่าตั้งค่าทั้งหมด (แก้ที่นี่ที่เดียว)

ทุกอย่างเป็น frozen dataclass เหมือน config.py ของ loot_sandbox → อ่านอย่างเดียว แชร์ข้ามเธรดได้

!! ตัวเลขกลยุทธ์ทั้งหมดเป็น "สมมติฐานเริ่มต้น" ไม่ใช่ค่าที่พิสูจน์แล้ว !!
ปรับทีละตัว และระวัง overfit: ถ้าต้องจูนละเอียดมากถึงจะกำไร = ไม่มี edge จริง
"""
from __future__ import annotations

from pathlib import Path

from . import settings as _settings
from .core.types import SymbolSpec
from .risk.risk_manager import RiskConfig
from .strategy.base import ManagementPlan, SessionWindow, Strategy
from .strategy.session_breakout import BreakoutConfig, SessionBreakout
from .strategy.session_momentum import MomentumConfig, SessionMomentum
from .strategy.level_sweep import LevelSweep, LevelSweepConfig
from .strategy.trend_pullback import PullbackConfig, TrendPullback
from .strategy.vol_expansion import VolExpansion, VolExpansionConfig
from .strategy.random_entry import RandomConfig, RandomEntry
from .strategy.daily_trend import DailyTrend, DailyTrendConfig
from .strategy.scalp import (BollingerFade, BollingerFadeConfig, EmaCross, EmaCrossConfig, RsiReversion,
                             RsiReversionConfig, ScalpWindow)
from .strategy.sweep_reversal import SweepConfig, SweepReversal

ROOT = Path(__file__).resolve().parent.parent
_S = _settings.load()          # ค่าที่ผู้ใช้ตั้งจากหน้าเว็บ (บอททดสอบเท่านั้น)
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
    # FxPro BITCOIN (ทดสอบ 2026-10-17): ค่าคอม $10/BTC · margin 0.5% · SL ต้องห่าง ≥ $200 · swap long −30%/ปี short −10%/ปี
    # สเปรดใน demo = 0 (ไม่สมจริง) → backtest ใส่ $5 + slippage $5 เผื่อไว้ · เทรดได้ 7 วัน
    "BTCUSD": SymbolSpec(name="BTCUSD", point=0.01, contract_size=1, volume_min=0.01, volume_step=0.01,
                         min_spread=5.0, slippage=5.0, commission_per_lot=10.0, margin_rate=0.005,
                         swap_long_rate=-0.30, swap_short_rate=-0.10, min_stop=200.0),
    # รอบ 4 (รายวัน) — สเปรดตั้งกว้างกว่าที่เห็นตอนตลาดปกติ เพื่อเผื่อช่วงข่าว/เปิดตลาด · swap ดึงจาก MT5 ตอน download
    "XAGUSD": SymbolSpec(name="XAGUSD", point=0.001, contract_size=5000, min_spread=0.03, slippage=0.005,
                         commission_per_lot=7.0, margin_rate=0.01),
    "XPTUSD": SymbolSpec(name="XPTUSD", point=0.01, contract_size=100, min_spread=4.6, slippage=0.5,
                         commission_per_lot=0.0, margin_rate=0.01),
    "WTI":    SymbolSpec(name="WTI", point=0.001, contract_size=1000, min_spread=0.05, slippage=0.01,
                         commission_per_lot=0.0, margin_rate=0.01),
    "BRENT":  SymbolSpec(name="BRENT", point=0.001, contract_size=1000, min_spread=0.05, slippage=0.01,
                         commission_per_lot=0.0, margin_rate=0.01),
    "NATGAS": SymbolSpec(name="NATGAS", point=0.001, contract_size=10000, min_spread=0.021, slippage=0.003,
                         commission_per_lot=0.0, margin_rate=0.01),
    "US30":   SymbolSpec(name="US30", point=0.01, contract_size=1, min_spread=3.0, slippage=1.0,
                         commission_per_lot=0.0, margin_rate=0.01),
    "SPX500": SymbolSpec(name="SPX500", point=0.01, contract_size=1, min_spread=0.6, slippage=0.2,
                         commission_per_lot=0.0, margin_rate=0.01),
}
DAILY_MARKETS = ["XAUUSD", "XAGUSD", "XPTUSD", "WTI", "BRENT", "NATGAS", "NAS100", "US30", "SPX500"]

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
    # บอททดสอบ "ไม่มี limit": ไม่หยุดรายวัน ไม่มี kill switch ไม้ 0.01 lot เสมอ (risk 0.25% < lot ขั้นต่ำ → ใช้ขั้นต่ำ)
    # เหลือด่านเดียว: equity จริงของบัญชีต่ำกว่า $600 → หยุดเปิดไม้ใหม่ เพื่อเหลือเงินให้บอทหลัก
    "test_free":  RiskConfig(risk_per_trade=0.0025, min_lot_max_risk=0.15, daily_loss_limit=1.0, max_drawdown=1.0,
                             max_open_groups=20, max_groups_per_symbol=10, close_on_daily_halt=False,
                             account_floor=600.0),
    # บอท M5 ที่วิจัยตัวเอง (2026-10-17 ตามคำขอ): ไม้ตายตัว ทอง 0.04 lot · NAS 0.73 · BTC 0.17 (แกว่งเป็นเงินเท่าทอง 0.04)
    # ได้ +$120 ในวันนั้น → ปิดทุกไม้ หยุดถึงวันใหม่ (UTC) · ไม่มีเพดานขาดทุนรายวัน · equity จริง < $600 → หยุดเปิดไม้
    # lot BTC / เป้ารายวัน / การขยาย SL ปรับได้จากหน้าเว็บ /settings (state/bot_settings.json)
    "m5_fixed":   RiskConfig(fixed_lots=(("XAUUSD", 0.04), ("NAS100", 0.73), ("BTCUSD", _S["btc_lot"])),
                             daily_profit_target=_S["daily_target"], widen_stops=_S["widen_stops"],
                             daily_loss_limit=1.0, max_drawdown=1.0, max_open_groups=20, max_groups_per_symbol=10,
                             close_on_daily_halt=False, account_floor=600.0),
}
NEWS_BLACKOUT_MIN = 30

# ---------------------------------------------------------------------------- live (เฟส 2: demo เท่านั้น)
# main ย้ายไป M5 (2026-10-21 ตามคำขอ — เดิม M15 เข้าได้วันละ ≤ 1 ไม้):
#   xau_momo เดิม (ดูแลไม้ที่ถืออยู่ต่อได้) + ema ทอง M5 = สายเดียวในวิจัย M5 ทองที่บวกทั้ง walk-forward (+$8.56/วัน ที่ 0.04 lot)
#   และช่วงทดสอบซ่อน 2 เดือน (+$15.69/วัน) · พารามิเตอร์ตายตัว (ไม่ปรับตามงานวิจัยรายคืน — main คือตัวทดสอบไปข้างหน้า)
MAIN_M5: dict[str, dict] = {
    "main_ema": {"family": "ema", "symbol": "XAUUSD", "plan": "native",
                 "params": {"fast": 20, "slow": 50, "sl_atr": 3.0, "tp_r": 2.0, "max_hold": 96}},
}
LIVE_STRATEGIES = ["xau_momo", *MAIN_M5]
MAIN_FAMILY = "momo+ema"             # ชื่อสายของ main ในตารางเรียนรู้/dataset (2 กลยุทธ์รวมกัน)
MAIN_FAMILY = "momo+ema"             # ชื่อสายของ main ในตารางเรียนรู้/dataset (2 กลยุทธ์รวมกัน)
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
#
# magic ที่เลิกใช้แล้ว ห้ามนำกลับมา: +1 lab_m5, +3 gamble, +4..+7 g_session/g_sweep/g_vol/g_trend,
#                                   +8..+10 bench_rand/bench_long/bench_m5  (2026-10-15)
#                                   +2, +11..+18 บอท M1 ชุดเดิม (2026-10-17 → ย้ายไป M5 ใช้ +21..+29)
_T = {"tf": "M1", "profile": "test_free", "allocation": 1000.0}
N_TEST = 19
LIVE_INSTANCES: dict[str, dict] = {
    # ทดสอบไปข้างหน้า — ห้ามแตะ
    # 2026-10-21 ตามคำขอ: main อยู่ใต้ผู้จัดการเหมือนบอททดสอบ (ไม้ตายตัวทอง 0.04 + ตัวคูณขนาดไม้ · ทำนาย/ปิดไม้/ล็อกกำไร)
    #   ยกเว้นสลับสาย — main มี 2 กลยุทธ์ตายตัว (MAIN_M5) ไม่ได้อยู่ใน m5_bots.json
    "main":    {"strategies": LIVE_STRATEGIES, "tf": "M5", "profile": "m5_fixed", "magic": MAGIC, "allocation": 1000.0,
                "warmup": 30000},
    # บอททดสอบ 19 ตัว (2 เกาะ): m5_1–9 = BTC · m5_10–19 = ทอง + NAS100 (เพิ่ม 2026-10-18 · magic +30..+39)
    # งานของแต่ละตัวอยู่ใน bot/m5_bots.json (วิจัยเต็ม) / state/m5_bots.json (ปรับใหม่ทุกคืน)
    # timeframe ปรับได้จากหน้าเว็บ /settings · warmup: ตัวแยกสภาวะตลาด (H1) ต้องใช้ ~100 วัน แต่ MT5 ให้ได้ไม่เกิน ~99,990 แท่ง
    **{f"m5_{i}": {"strategies": [f"m5_{i}"], "tf": _S["tf"], "profile": "m5_fixed", "magic": MAGIC + 20 + i,
                   "allocation": 1000.0, "warmup": {"M1": 99000, "M5": 30000}.get(_S["tf"], 10000)} for i in range(1, N_TEST + 1)},
}


# บอทที่ start_bot.ps1 all / restart_bots.ps1 จะเปิด (ไม่รวมตัวที่ผู้ใช้กดปิดในหน้าเว็บ · main เปิดเสมอ)
ENABLED_INSTANCES = [n for n in LIVE_INSTANCES if n not in _S["disabled_bots"]]


def instance_state_dir(name: str) -> Path:
    return STATE_DIR if name == "main" else STATE_DIR / name

# ---------------------------------------------------------------------------- กลยุทธ์
GOLD_SESSION = SessionWindow(tz="UTC", range_start="00:00", range_end="07:00",
                             trade_end="16:00", flat_time="20:00")
NAS_SESSION = SessionWindow(tz="America/New_York", range_start="09:30", range_end="10:00",
                            trade_end="12:00", flat_time="15:45")

ALL_DAY = ScalpWindow(0, 24)
# บอททดสอบทุกตัว (ยกเว้น main): ปิดทันทีเมื่อกำไร +$0.6 ต่อไม้ · SL ตาม ATR เท่าเดิม — ตามคำขอผู้ใช้ 2026-10-15
# ผลทดสอบก่อนเปลี่ยน (research/m1_small_tp_test.py, ทอง M1 0.01 lot): ชนะ 76–81% แต่ต้องชนะ 89–92% ถึงเท่าทุน
#   bb_xau −$20 → −$44/วัน · lr1_x −$21 → −$33/วัน · ec1_x −$11 → −$13/วัน  (ขาดทุนมากกว่าแบบเดิม)
# แบบเดิม (ล็อก +0.5R → trail 1 ATR → ขยับ TP) เก็บไว้ที่ M1_TRAIL_PLAN
M1_TRAIL_PLAN = ManagementPlan(partial_r=0.5, partial_frac=1.0, be_offset_r=0.1, trail_atr_mult=1.0, trail_start_r=0.5,
                               tp_extend_at=0.7, tp_extend_r=0.5)
# 2026-10-16: เพิ่ม SL −$0.9 ต่อไม้ ตามคำขอ (เดิม SL ตาม ATR ≈ $2–4)
M1_FIXED_PLAN = ManagementPlan(partial_r=None, trail_atr_mult=None, tp_usd=0.6, sl_usd=0.9)
# 2026-10-16: ล็อกกำไรแบบไต่ตามคำขอ — ไม่มี TP ตายตัว · เคยถึง +$0.6 ล็อก 50% ของจุดสูงสุด, +$1.5 → 65%, +$3 → 80% · SL −$0.9
# ทดสอบ (research/m1_small_tp_test.py, ทอง M1 0.01 lot): ดีกว่า TP $0.6/SL $0.9 ทุกตัว แต่ยังขาดทุน
#   bb_xau −$70 → −$46/วัน · lr1_x −$48 → −$36 · ec1_x −$14 → −$11 · c_lr1_x −$48 → −$35
M1_PLAN = ManagementPlan(partial_r=None, trail_atr_mult=None, sl_usd=0.9,
                         lock_tiers=((0.6, 0.5), (1.5, 0.65), (3.0, 0.8)))

# ห้องทดลองสแกลป์ (research/hypotheses_v3_scalp.md) — **ตกทั้ง 4 ตัวใน backtest** (−0.016 ถึง −0.205R)
# รันบน demo ตามคำขอเพื่อดูผลจริงกับตา · พารามิเตอร์ = ชุดที่ walk-forward เลือกในหน้าต่างล่าสุด
LAB_CONFIGS: list = [
    # 2026-10-15: เปิด 24 ชม. ตามคำขอ (เดิม 07–20 UTC / เอเชียเท่านั้น) เพื่อศึกษาไม้แพ้ให้มากขึ้น
    RsiReversionConfig(tag="lr_xau", symbol="XAUUSD", rsi_th=5.0, sl_atr=2.0, max_hold=24, window=ALL_DAY),
    RsiReversionConfig(tag="lr_nas", symbol="NAS100", rsi_th=5.0, sl_atr=2.0, max_hold=12, window=ALL_DAY),
    EmaCrossConfig(tag="ec_xau", symbol="XAUUSD", fast=12, slow=26, sl_atr=1.0, tp_r=2.0, window=ALL_DAY),
    BollingerFadeConfig(tag="bb_xau", symbol="XAUUSD", k=2.0, sl_atr=2.0, max_hold=15, window=ALL_DAY, plan=M1_PLAN),
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

# M1 สายซิ่ง (2026-10-15 ตามคำขอ): เข้าๆ ออกๆ 24 ชม. · ได้กำไร +0.5R ล็อก → trail 1 ATR → ราคาถึง 70% ของ TP ขยับ TP +0.5R
# ข้อมูลเดิม: M1 ต้นทุน ≈ 16% ของ SL ทุกไม้ (research/hypotheses_v3_scalp.md) — คาดว่าขาดทุน
_M1W = SessionWindow("UTC", "00:00", "00:15", "20:45", "20:50")   # ย่อในเทรนด์: เกือบทั้งวัน ปิดก่อน rollover
M1_CONFIGS: list = []
for _sym, _s in (("XAUUSD", "x"), ("NAS100", "n")):
    for _mode, _pre in (("rule", ""), ("random", "c_")):
        M1_CONFIGS += [
            RsiReversionConfig(tag=f"{_pre}lr1_{_s}", symbol=_sym, rsi_th=10.0, sl_atr=1.5, max_hold=30, tp_r=1.0,
                               window=ALL_DAY, plan=M1_PLAN, side_mode=_mode, seed=21),
            BollingerFadeConfig(tag=f"{_pre}bb1_{_s}", symbol=_sym, k=2.0, sl_atr=1.5, max_hold=30, tp_r=1.0,
                                window=ALL_DAY, plan=M1_PLAN, side_mode=_mode, seed=22),
            DailyTrendConfig(tag=f"{_pre}br1_{_s}", symbol=_sym, entry_n=30, stop_atr=1.5, plan=M1_PLAN,
                             side_mode=_mode, seed=23),
        ]
    M1_CONFIGS += [
        EmaCrossConfig(tag=f"ec1_{_s}", symbol=_sym, fast=9, slow=21, sl_atr=1.5, tp_r=1.0, window=ALL_DAY, plan=M1_PLAN),
        PullbackConfig(tag=f"pb1_{_s}", symbol=_sym, window=_M1W, trend_tf_min=60, trend_ema=50, pullback_ema=20,
                       sl_atr=1.5, tp_r=1.0, plan=M1_PLAN),
    ]

# กลุ่มควบคุม (benchmark) — สุ่มเข้าวันละไม่เกิน 1 ไม้ ในช่วงเวลาที่มีสภาพคล่อง ด้วย SL/TP มาตรฐาน
_M5W = SessionWindow("UTC", "06:45", "07:00", "17:00", "20:00")
BENCH_CONFIGS: list = [
    RandomConfig(tag="b_rnd_x", symbol="XAUUSD", window=_XW, prob_per_bar=0.05, side_mode="random", seed=11),
    RandomConfig(tag="b_rnd_n", symbol="NAS100", window=_NW, prob_per_bar=0.08, side_mode="random", seed=12),
    RandomConfig(tag="b_lng_x", symbol="XAUUSD", window=_XW, prob_per_bar=0.05, side_mode="long", seed=13),
    RandomConfig(tag="b_lng_n", symbol="NAS100", window=_NW, prob_per_bar=0.08, side_mode="long", seed=14),
    # จังหวะเข้าเดียวกับ lr_xau / lr_nas / ec_xau แต่สุ่มทิศ (24 ชม. เหมือนกัน)
    RsiReversionConfig(tag="b_lr5_x", symbol="XAUUSD", rsi_th=5.0, sl_atr=2.0, max_hold=24, window=ALL_DAY,
                       side_mode="random", seed=15),
    RsiReversionConfig(tag="b_lr5_n", symbol="NAS100", rsi_th=5.0, sl_atr=2.0, max_hold=12, window=ALL_DAY,
                       side_mode="random", seed=16),
    EmaCrossConfig(tag="b_ec5_x", symbol="XAUUSD", fast=12, slow=26, sl_atr=1.0, tp_r=2.0, window=ALL_DAY,
                   side_mode="random", seed=17),
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


# lot มาตรฐานที่งานวิจัยใช้ — แปลงกำไร/ขาดทุนทุกไม้เป็นหน่วยนี้ก่อนคิดสถิติ (ไม้ที่เทรด lot ต่างกันจะได้เทียบกันได้)
STD_LOTS = {"XAUUSD": 0.04, "NAS100": 0.73, "BTCUSD": 0.17}


def std_profit(symbol: str | None, volume: float | str | None, profit: float) -> float:
    try:
        v = float(volume or 0)
    except (TypeError, ValueError):
        v = 0.0
    std = STD_LOTS.get(symbol or "")
    return round(profit * std / v, 2) if std and v > 0 else round(profit, 2)


M5_BOTS_FILE = ROOT / "bot" / "m5_bots.json"          # งานจากการวิจัยเต็ม (ติดมากับโค้ด)
M5_BOTS_TUNED = ROOT / "state" / "m5_bots.json"       # งานที่ปรับพารามิเตอร์ใหม่ทุกสัปดาห์บนเซิร์ฟเวอร์ (python -m bot research) — ถ้ามี ใช้อันนี้ก่อน
# ล็อกกำไรแบบไต่ของบอท M5 (ดอลลาร์ ที่ทอง 0.04 lot ≈ 0.5 / 1 / 2 ATR ของ M5)
M5_RATCHET_PLAN = ManagementPlan(partial_r=None, trail_atr_mult=None, lock_tiers=((8.0, 0.5), (17.0, 0.65), (34.0, 0.8)))
M5_PLANS: dict[str, ManagementPlan | None] = {"native": None, "trail": M1_TRAIL_PLAN, "ratchet": M5_RATCHET_PLAN}


def load_m5_bots() -> list[dict]:
    import json
    try:
        path = M5_BOTS_TUNED if M5_BOTS_TUNED.exists() else M5_BOTS_FILE
        return json.loads(path.read_text(encoding="utf-8"))["bots"]
    except (OSError, ValueError, KeyError):
        return []


def build_strategies(tags: list[str] | None = None, symbols: list[str] | None = None,
                     timeframe: str = TIMEFRAME) -> list[Strategy]:
    out: list[Strategy] = []
    builders = {BreakoutConfig: SessionBreakout, SweepConfig: SweepReversal, MomentumConfig: SessionMomentum,
                RsiReversionConfig: RsiReversion, EmaCrossConfig: EmaCross, BollingerFadeConfig: BollingerFade,
                VolExpansionConfig: VolExpansion, LevelSweepConfig: LevelSweep, PullbackConfig: TrendPullback,
                RandomConfig: RandomEntry, DailyTrendConfig: DailyTrend}
    for c in STRATEGY_CONFIGS + LAB_CONFIGS + GAMBLE_CONFIGS + BENCH_CONFIGS + M1_CONFIGS:
        if tags and c.tag not in tags:
            continue
        if symbols and c.symbol not in symbols:
            continue
        if tags is None and (c in LAB_CONFIGS or c in GAMBLE_CONFIGS or c in BENCH_CONFIGS or c in M1_CONFIGS):
            continue                       # แล็บต้องเรียกชื่อตรงๆ เท่านั้น ไม่ติดไปกับ backtest ปกติ
        out.append(builders[type(c)](c, timeframe))
    if tags:
        from .strategy.catalog import build
        for tag, b in MAIN_M5.items():
            if tag in tags:
                out.append(build(b["family"], b["symbol"], b["params"], M5_PLANS[b["plan"]], timeframe, tag))
        for b in load_m5_bots():
            if b["bot"] in tags:
                out.append(build(b["family"], b["symbol"], b["params"], M5_PLANS[b["plan"]], timeframe, b["bot"]))
    return out
