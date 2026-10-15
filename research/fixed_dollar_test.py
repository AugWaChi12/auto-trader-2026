"""
fixed_dollar_test.py — "กำไร $5 ปิด / ขาดทุน $2 ตัด" ดีกว่าแบบเดิมไหม?

    python research/fixed_dollar_test.py

ทุกไม้คิดเป็นเงินที่ขนาดไม้คงที่ (ทอง: 0.01 lot = $1 ต่อราคา $1, 0.03 lot = $3 ต่อ $1) รวมต้นทุนจริง
แบบเดิม: SL/TP ตาม ATR (config.LAB_CONFIGS) · แบบใหม่: SL $2 / TP $5 ที่ขนาดไม้เดียวกัน
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
from research.sweep import RESEARCH_RISK  # noqa: E402
import bot.strategy.scalp as S  # noqa: E402

# (tag, tf, lot ที่บอทใช้จริงบน demo)
CASES = [("lr_xau", "M5", 0.01), ("ec_xau", "M5", 0.01), ("bb_xau", "M1", 0.03)]
BUILD = {"RsiReversionConfig": S.RsiReversion, "EmaCrossConfig": S.EmaCross, "BollingerFadeConfig": S.BollingerFade}


def job(args: tuple) -> dict:
    tag, tf, lot, label, sl_usd, tp_usd = args
    base = next(c for c in C.LAB_CONFIGS if c.tag == tag)
    cfg = base if sl_usd is None else replace(base, sl_price=sl_usd / (lot * 100), tp_price=tp_usd / (lot * 100))
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, cfg.symbol, tf))
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({cfg.symbol: bars}, {cfg.symbol: load_spec(data, cfg.symbol, C.DEFAULT_SPECS[cfg.symbol])},
                       [BUILD[type(cfg).__name__](cfg, tf)], RESEARCH_RISK, tf, 1_000_000.0, news)
    usd = np.array([t.pnl / t.volume * lot for t in res.trades])     # แปลงเป็นเงินที่ขนาดไม้จริงบน demo
    w, l = usd[usd > 0], usd[usd <= 0]
    days = max((bars[-1].time - bars[0].time).days * 5 / 7, 1)
    return {"บอท": f"{tag} {tf} ({lot} lot)", "แบบ": label, "ไม้": len(usd), "ชนะ%": round(len(w) / len(usd) * 100, 1),
            "ชนะได้เฉลี่ย $": round(w.mean(), 2), "แพ้เสียเฉลี่ย $": round(l.mean(), 2),
            "เฉลี่ย/ไม้ $": round(usd.mean(), 3), "รวม $": round(usd.sum(), 1), "ต่อวันทำการ $": round(usd.sum() / days, 2)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 200)
    jobs = []
    for tag, tf, lot in CASES:
        jobs += [(tag, tf, lot, "แบบเดิม (ATR)", None, None), (tag, tf, lot, "TP $5 / SL $2", 2.0, 5.0),
                 (tag, tf, lot, "TP $5 / SL $5", 5.0, 5.0), (tag, tf, lot, "TP $2 / SL $5", 5.0, 2.0)]
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(job, jobs))
    df = pd.DataFrame(rows)
    for bot, g in df.groupby("บอท", sort=False):
        print(f"\n### {bot}")
        print(g.drop(columns="บอท").to_string(index=False))
    df.to_csv(ROOT / "research" / "out" / "fixed_dollar_test.csv", index=False)


if __name__ == "__main__":
    main()
