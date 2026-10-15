"""
take_profit_test.py — "ได้กำไรนิดหน่อยก็ปิดเลย" ดีกว่าไหม?

    python research/take_profit_test.py

เทียบกลยุทธ์แล็บ (พารามิเตอร์ตาม config.LAB_CONFIGS) แบบเดิม vs ปิดที่ 0.3R / 0.5R / 1R vs เลื่อน BE ที่ 0.5R
ข้อมูลทั้งหมดที่มี (ไม่ใช่ walk-forward) — เป็นการตรวจกลไก ไม่ใช่การหา edge ใหม่
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
from bot.backtest.report import group_trades  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.strategy.scalp import BollingerFadeConfig, EmaCrossConfig  # noqa: E402
from research.sweep import RESEARCH_RISK  # noqa: E402

TF = {"lr_xau": "M5", "lr_nas": "M5", "ec_xau": "M5", "bb_xau": "M1"}
VARIANTS = [("แบบเดิม", {}), ("ปิดที่ +0.3R", {"tp_r": 0.3}), ("ปิดที่ +0.5R", {"tp_r": 0.5}),
            ("ปิดที่ +1R", {"tp_r": 1.0}), ("BE เมื่อ +0.5R", {"be_at_r": 0.5})]


def job(args: tuple) -> dict:
    tag, label, change = args
    base = next(c for c in C.LAB_CONFIGS if c.tag == tag)
    cfg = replace(base, **change)
    tf = TF[tag]
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, cfg.symbol, tf))
    strat = C.build_strategies.__globals__["RsiReversion"]  # noqa: F841 — ensure import
    builders = {"RsiReversionConfig": "RsiReversion", "EmaCrossConfig": "EmaCross", "BollingerFadeConfig": "BollingerFade"}
    import bot.strategy.scalp as S
    s = getattr(S, builders[type(cfg).__name__])(cfg, tf)
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({cfg.symbol: bars}, {cfg.symbol: load_spec(data, cfg.symbol, C.DEFAULT_SPECS[cfg.symbol])},
                       [s], RESEARCH_RISK, tf, 1_000_000.0, news)
    r = np.array([g.r for g in group_trades(res.trades)])
    w, l = r[r > 0], r[r <= 0]
    return {"บอท": f"{tag} {tf}", "แบบ": label, "ไม้": len(r), "ชนะ%": round(len(w) / len(r) * 100, 1),
            "กำไรเฉลี่ย/ไม้ที่ชนะ": round(w.mean(), 2), "ขาดทุนเฉลี่ย/ไม้ที่แพ้": round(l.mean(), 2),
            "ผลเฉลี่ย/ไม้ (R)": round(r.mean(), 3), "PF": round(w.sum() / -l.sum(), 2), "รวม (R)": round(r.sum(), 1)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 220)
    jobs = [(tag, label, ch) for tag in TF for label, ch in VARIANTS
            if not (tag == "ec_xau" and "be_at_r" in ch and False)]
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(job, jobs))
    df = pd.DataFrame(rows)
    for bot, g in df.groupby("บอท", sort=False):
        print(f"\n### {bot}")
        print(g.drop(columns="บอท").to_string(index=False))
    df.to_csv(ROOT / "research" / "out" / "take_profit_test.csv", index=False)


if __name__ == "__main__":
    main()
