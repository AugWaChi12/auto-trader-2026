"""
sweep.py — ขั้นที่ 2: ทดสอบ grid ของพารามิเตอร์บน IN-SAMPLE

    python research/sweep.py

กติกา (ดู README / prompt):
  - grid ≤ 50 ชุดต่อกลยุทธ์, พารามิเตอร์ ≤ 4 ตัว
  - วัดผลเป็น R ด้วยทุนใหญ่ (lot ขั้นต่ำไม่บังคับ) — ปัญหาทุน $1,000 ดูแยกทีหลัง
  - เลือกจาก "ที่ราบ": ทุกค่าข้างเคียง (ขยับพารามิเตอร์ทีละตัว 1 ขั้น) ต้องเป็นบวก
  - รายงาน long/short แยก, ครึ่งแรก/ครึ่งหลัง, เทียบ benchmark
ผลทั้งหมดบันทึกที่ research/out/sweep_<strategy>.csv
"""
from __future__ import annotations

import itertools
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.report import group_trades  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.core.timeutil import UTC  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.risk.risk_manager import RiskConfig  # noqa: E402
from bot.strategy.base import SessionWindow  # noqa: E402
from bot.strategy.random_entry import RandomConfig, RandomEntry  # noqa: E402
from bot.strategy.session_momentum import MomentumConfig, SessionMomentum  # noqa: E402
from bot.strategy.trend_pullback import PullbackConfig, TrendPullback  # noqa: E402

IS_START = datetime(2023, 10, 16, tzinfo=UTC)
IS_END = datetime(2025, 7, 1, tzinfo=UTC)
IS_MID = IS_START + (IS_END - IS_START) / 2
DATA = ROOT / "data" / "mt5"
OUT = ROOT / "research" / "out"

RESEARCH_RISK = RiskConfig(risk_per_trade=0.01, daily_loss_limit=1.0, max_drawdown=1.0,
                           max_open_groups=50, max_groups_per_symbol=50)

XAU_WIN = SessionWindow("UTC", "06:45", "07:00", "17:00", "20:00")
NAS_WIN = SessionWindow("America/New_York", "09:30", "09:45", "15:00", "15:45")

_BARS: dict = {}


def _bars(symbol: str, start: datetime, end: datetime) -> list:
    key = (symbol, start, end)
    if key not in _BARS:
        _BARS[key] = [b for b in load_bars(bars_path(DATA, symbol, "M15")) if start <= b.time < end]
    return _BARS[key]


def backtest(strategy, start: datetime = IS_START, end: datetime = IS_END, balance: float = 1_000_000.0):
    sym = strategy.symbol
    bars = _bars(sym, start, end)
    spec = load_spec(DATA, sym, C.DEFAULT_SPECS[sym])
    news = NewsCalendar.default(C.NEWS_CSV, (start.year, end.year), C.NEWS_BLACKOUT_MIN)
    return run_backtest({sym: bars}, {sym: spec}, [strategy], RESEARCH_RISK, "M15", balance, news)


def summarize(res, mid: datetime = IS_MID) -> dict:
    g = pd.DataFrame([asdict(x) for x in group_trades(res.trades)])
    if g.empty:
        return {"n": 0}
    r = g["r"]
    t = pd.to_datetime(g["open_time"])
    longs, shorts = g[g["side"] == "BUY"]["r"], g[g["side"] == "SELL"]["r"]
    win = r[r > 0].sum()
    loss = -r[r <= 0].sum()
    return {
        "n": len(g), "exp_r": round(r.mean(), 3), "t": round(r.mean() / (r.std(ddof=1) / np.sqrt(len(r))), 2) if len(r) > 2 else 0,
        "pf": round(win / loss, 2) if loss > 0 else np.inf, "win%": round((r > 0).mean() * 100, 1),
        "n_long": len(longs), "exp_long": round(longs.mean(), 3) if len(longs) else np.nan,
        "n_short": len(shorts), "exp_short": round(shorts.mean(), 3) if len(shorts) else np.nan,
        "exp_h1": round(r[t < mid].mean(), 3), "exp_h2": round(r[t >= mid].mean(), 3),
    }


# ---------------------------------------------------------------------------- grids
def momentum_grid() -> list[dict]:
    return [dict(entry_time=e, min_move_adr=m, sl_adr=s)
            for e, m, s in itertools.product(["11:00", "12:00", "13:00"], [0.0, 0.2, 0.35, 0.5], [0.3, 0.5, 0.8])]


def pullback_grid() -> list[dict]:
    return [dict(trend_ema=a, pullback_ema=b, sl_atr=c, tp_r=d)
            for a, b, c, d in itertools.product([20, 50, 100], [20, 50], [1.0, 2.0], [1.5, 2.5])]


def make(kind: str, symbol: str, p: dict):
    if kind == "momo":
        return SessionMomentum(MomentumConfig("momo", symbol, **p), "M15")
    win = XAU_WIN if symbol == "XAUUSD" else NAS_WIN
    if kind == "pullback":
        return TrendPullback(PullbackConfig("pb", symbol, win, **p), "M15")
    if kind == "random":
        return RandomEntry(RandomConfig("rnd", symbol, win, **p), "M15")
    raise ValueError(kind)


def _job(args: tuple[str, str, dict]) -> dict:
    kind, symbol, p = args
    return {"symbol": symbol, **p, **summarize(backtest(make(kind, symbol, p)))}


def plateau(df: pd.DataFrame, params: list[str], grid: dict[str, list]) -> pd.Series:
    """จำนวนเพื่อนบ้าน (ขยับพารามิเตอร์ทีละตัว 1 ขั้น) และค่าต่ำสุดของ expectancy ของเพื่อนบ้าน"""
    worst = []
    for _, row in df.iterrows():
        vals = []
        for p in params:
            opts = grid[p]
            i = opts.index(row[p])
            for j in (i - 1, i + 1):
                if 0 <= j < len(opts):
                    m = df[(df[p] == opts[j]) & np.logical_and.reduce([df[q] == row[q] for q in params if q != p])]
                    vals += m["exp_r"].tolist()
        worst.append(min(vals) if vals else np.nan)
    return pd.Series(worst, index=df.index)


def run_sweep(kind: str, symbols: list[str], grid: list[dict], workers: int = 6) -> pd.DataFrame:
    jobs = [(kind, s, p) for s in symbols for p in grid]
    with ProcessPoolExecutor(workers) as ex:
        rows = list(ex.map(_job, jobs))
    df = pd.DataFrame(rows)
    params = list(grid[0])
    gvals = {p: sorted({g[p] for g in grid}) for p in params}
    df["worst_neighbor"] = pd.concat([plateau(d, params, gvals) for _, d in df.groupby("symbol")])
    return df


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    OUT.mkdir(parents=True, exist_ok=True)
    which = sys.argv[1:] or ["momo", "pullback", "bench"]
    if "oos" in which:
        oos()
        return

    if "momo" in which:
        df = run_sweep("momo", ["XAUUSD", "NAS100"], momentum_grid())
        df.to_csv(OUT / "sweep_momo.csv", index=False)
        print("\n##### SESSION MOMENTUM (in-sample) — เรียงตาม expectancy")
        print(df.sort_values("exp_r", ascending=False).head(15).to_string(index=False))
        print(f"\nบวก {(df['exp_r'] > 0).sum()}/{len(df)} ชุด")

    if "pullback" in which:
        df = run_sweep("pullback", ["XAUUSD", "NAS100"], pullback_grid())
        df.to_csv(OUT / "sweep_pullback.csv", index=False)
        print("\n##### TREND PULLBACK (in-sample) — เรียงตาม expectancy")
        print(df.sort_values("exp_r", ascending=False).head(15).to_string(index=False))
        print(f"\nบวก {(df['exp_r'] > 0).sum()}/{len(df)} ชุด")

    if "bench" in which:
        rows = []
        for sym in ("XAUUSD", "NAS100"):
            for mode in ("long", "random"):
                p = dict(entry_time="12:00", min_move_adr=0.0, sl_adr=0.5, side_mode=mode)
                rows.append({"bench": f"momo-mechanics {mode}", "symbol": sym,
                             **summarize(backtest(SessionMomentum(MomentumConfig("b", sym, **p), "M15")))})
                for sl, tp in ((1.0, 1.5), (2.0, 2.5)):
                    p = dict(prob_per_bar=0.05, side_mode=mode, sl_atr=sl, tp_r=tp)
                    rows.append({"bench": f"random-time {mode} sl{sl} tp{tp}", "symbol": sym,
                                 **summarize(backtest(make("random", sym, p)))})
        df = pd.DataFrame(rows)
        df.to_csv(OUT / "benchmarks.csv", index=False)
        print("\n##### BENCHMARKS (in-sample)")
        print(df.to_string(index=False))



# ---------------------------------------------------------------------------- out-of-sample (รันหลังล็อกเท่านั้น)
OOS_START = IS_END
OOS_END = datetime(2026, 10, 16, tzinfo=UTC)


def oos() -> None:
    """เปิด out-of-sample: ชุดที่ล็อก (ตัวเลขทางการ) + ทั้ง grid (ข้อมูลประกอบ) + benchmark ช่วงเดียวกัน"""
    import json
    locked = json.loads((OUT / "locked.json").read_text(encoding="utf-8"))["xau_momo"]
    mid = OOS_START + (OOS_END - OOS_START) / 2
    p = {k: locked[k] for k in ("entry_time", "min_move_adr", "sl_adr", "flat_time")}
    print("\n##### OUT-OF-SAMPLE: ชุดที่ล็อก", p)
    res = backtest(SessionMomentum(MomentumConfig("momo", "XAUUSD", **p), "M15"), OOS_START, OOS_END)
    print(summarize(res, mid))

    rows = []
    for g in momentum_grid():
        s = summarize(backtest(SessionMomentum(MomentumConfig("momo", "XAUUSD", **g), "M15"), OOS_START, OOS_END), mid)
        rows.append({**g, **s})
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "oos_momo_grid.csv", index=False)
    print(f"\nทั้ง grid ใน OOS: บวก {(df['exp_r'] > 0).sum()}/{len(df)}  เฉลี่ย {df['exp_r'].mean():+.3f}R")
    print(df.pivot_table(index="entry_time", columns="min_move_adr", values="exp_r", aggfunc="mean").round(3))

    print("\nbenchmark OOS (กลไกเดียวกัน):")
    for mode in ("long", "random"):
        b = dict(p, min_move_adr=locked["min_move_adr"], side_mode=mode)
        print(f"  {mode:6s}", summarize(backtest(SessionMomentum(MomentumConfig("b", "XAUUSD", **b), "M15"),
                                                 OOS_START, OOS_END), mid))


if __name__ == "__main__":
    main()
