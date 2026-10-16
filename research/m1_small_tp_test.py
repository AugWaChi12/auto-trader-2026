"""
m1_small_tp_test.py — บอท M1: ปิดทันทีเมื่อกำไร +$0.5 / +$0.6 (SL ตาม ATR เท่าเดิม) ดีกว่าแบบปัจจุบันไหม

    python research/m1_small_tp_test.py

ข้อมูล M1 ~3 เดือน · ทองไม้ 0.01 lot (ขนาดจริงบน demo: $1 ต่อราคา $1) · ต้นทุนจริง
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.strategy.base import ManagementPlan  # noqa: E402
from bot.risk.risk_manager import RiskConfig  # noqa: E402

# ไม้ 0.01 lot ตายตัวเหมือนบอทจริง (ความเสี่ยงที่ขอเล็กมาก → ใช้ lot ขั้นต่ำเสมอ) — จำเป็นสำหรับ SL/TP เป็นดอลลาร์
FIXED_MIN_LOT = RiskConfig(risk_per_trade=1e-9, min_lot_max_risk=1.0, daily_loss_limit=1.0, max_drawdown=1.0,
                           max_open_groups=50, max_groups_per_symbol=50)

LOT = 0.01
TAGS = ["bb_xau", "lr1_x", "ec1_x", "c_lr1_x"]          # ทอง M1 (ฝั่งที่ขาดทุนเป็นเงินมากที่สุด)
NO_EXTEND = ManagementPlan(partial_r=None, trail_atr_mult=None)
VARIANTS = [("ล็อก+ขยับ (เดิม)", None), ("TP +$0.6 / SL −$0.9", "0.6/0.9"),
            # ล็อกกำไรแบบไต่ (ไม่มี TP ตายตัว) · SL −$0.9
            ("ไต่ A: 0.6→50% 1.5→65% 3→80%", ManagementPlan(partial_r=None, trail_atr_mult=None, sl_usd=0.9,
                                                       lock_tiers=((0.6, 0.5), (1.5, 0.65), (3.0, 0.8)))),
            ("ไต่ B: 0.4→40% 1→60% 2.5→80%", ManagementPlan(partial_r=None, trail_atr_mult=None, sl_usd=0.9,
                                                       lock_tiers=((0.4, 0.4), (1.0, 0.6), (2.5, 0.8)))),
            ("ไต่ A + SL ATR", ManagementPlan(partial_r=None, trail_atr_mult=None,
                                              lock_tiers=((0.6, 0.5), (1.5, 0.65), (3.0, 0.8))))]


def job(args: tuple) -> dict:
    tag, label, tp_usd = args
    cfg = next(c for c in C.LAB_CONFIGS + C.M1_CONFIGS if c.tag == tag)
    if tp_usd is None:                       # แบบเดิมก่อนเปลี่ยน: ล็อก +0.5R → trail → ขยับ TP
        cfg = replace(cfg, plan=C.M1_TRAIL_PLAN)
    elif isinstance(tp_usd, ManagementPlan):
        cfg = replace(cfg, plan=tp_usd)
    elif tp_usd == "0.6/0.9":
        cfg = replace(cfg, plan=ManagementPlan(partial_r=None, trail_atr_mult=None, tp_usd=0.6, sl_usd=0.9))  # Engine ทับ TP/SL เอง
    elif tp_usd is not None:
        cfg = replace(cfg, tp_price=tp_usd / (LOT * 100), tp_r=None, plan=NO_EXTEND)
    strat = C.build_strategies.__globals__["BollingerFade" if "bb" in tag else
                                           "EmaCross" if "ec" in tag else "RsiReversion"](cfg, "M1")
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, "XAUUSD", "M1"))
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({"XAUUSD": bars}, {"XAUUSD": load_spec(data, "XAUUSD", C.DEFAULT_SPECS["XAUUSD"])},
                       [strat], FIXED_MIN_LOT, "M1", 1_000_000.0, news)
    usd = np.array([t.pnl / t.volume * LOT for t in res.trades])
    w, l = usd[usd > 0], usd[usd <= 0]
    days = max((bars[-1].time - bars[0].time).days * 5 / 7, 1)
    need = -l.mean() / (w.mean() - l.mean()) * 100 if len(w) and len(l) else float("nan")
    return {"บอท": tag, "แบบ": label, "ไม้/วัน": round(len(usd) / days), "ชนะ%": round(len(w) / len(usd) * 100, 1),
            "ต้องชนะ% ถึงเท่าทุน": round(need, 1), "ได้ตอนชนะ $": round(w.mean(), 2), "เสียตอนแพ้ $": round(l.mean(), 2),
            "เฉลี่ย/ไม้ $": round(usd.mean(), 3), "ต่อวัน $": round(usd.sum() / days, 2)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 220)
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(job, [(t, lab, v) for t in TAGS for lab, v in VARIANTS]))
    df = pd.DataFrame(rows)
    for tag, g in df.groupby("บอท", sort=False):
        print(f"\n### {tag}")
        print(g.drop(columns="บอท").to_string(index=False))


if __name__ == "__main__":
    main()
