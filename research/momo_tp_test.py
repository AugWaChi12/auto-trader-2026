"""
momo_tp_test.py — xau_momo: ถือถึงปิดสิ้นวัน (เดิม) vs ปิดกำไรเร็ว

    python research/momo_tp_test.py

ข้อมูล M15 3 ปี · ไม้ 0.01 lot (ขนาดจริงบน demo) · ต้นทุนจริง
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
from bot.strategy.session_momentum import SessionMomentum  # noqa: E402
from research.sweep import RESEARCH_RISK  # noqa: E402

LOT = 0.01
VARIANTS = [("เดิม: ถือถึง 03:00 น.", {}), ("TP +0.5R", {"tp_r": 0.5}), ("TP +0.6R", {"tp_r": 0.6}),
            ("TP +1R", {"tp_r": 1.0}), ("TP +$0.5 (ราคา)", {"tp_price": 0.5}), ("TP +$0.6 (ราคา)", {"tp_price": 0.6})]


def job(args: tuple) -> dict:
    label, ch = args
    base = next(c for c in C.STRATEGY_CONFIGS if c.tag == "xau_momo")
    cfg = replace(base, **ch)
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, "XAUUSD", "M15"))
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({"XAUUSD": bars}, {"XAUUSD": load_spec(data, "XAUUSD", C.DEFAULT_SPECS["XAUUSD"])},
                       [SessionMomentum(cfg, "M15")], RESEARCH_RISK, "M15", 1_000_000.0, news)
    usd = np.array([t.pnl / t.volume * LOT for t in res.trades])
    r = np.array([g.r for g in group_trades(res.trades)])
    w, l = usd[usd > 0], usd[usd <= 0]
    years = (bars[-1].time - bars[0].time).days / 365
    return {"แบบ": label, "ไม้": len(usd), "ชนะ%": round(len(w) / len(usd) * 100, 1),
            "ได้ตอนชนะ $": round(w.mean(), 2), "เสียตอนแพ้ $": round(l.mean(), 2),
            "เฉลี่ย/ไม้": f"{r.mean():+.3f}R", "รวม $ (3 ปี)": round(usd.sum(), 1), "ต่อปี $": round(usd.sum() / years, 1)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 200)
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(job, VARIANTS))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
