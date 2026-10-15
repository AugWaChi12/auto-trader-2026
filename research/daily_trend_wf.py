"""
daily_trend_wf.py — T1: Trend following รายวัน หลายตลาด (research/hypotheses_v4_daily.md)

    python research/daily_trend_wf.py            ช่วงวิจัย (walk-forward ถึง 2022)
    python research/daily_trend_wf.py holdout    เปิด hold-out 2023-01 → (ครั้งเดียวหลังล็อก)

ทุกชุดพารามิเตอร์ × 3 โหมด (rule / random / long) = backtest พอร์ต 9 ตลาดพร้อมกัน ครั้งเดียวตลอดช่วง
แล้ว walk-forward: เลือกพารามิเตอร์จาก 5 ปีก่อนหน้า (คะแนนที่ราบ) → วัดผลปีถัดไป
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
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
from bot.risk.risk_manager import RiskConfig  # noqa: E402
from bot.strategy.daily_trend import DailyTrend, DailyTrendConfig  # noqa: E402
from research.walkforward import neighbors, stats  # noqa: E402

DATA = ROOT / "data" / "mt5"
OUT = ROOT / "research" / "out"
RESEARCH_END = datetime(2023, 1, 1, tzinfo=UTC)
HOLDOUT_END = datetime(2026, 10, 16, tzinfo=UTC)
DATA_START = {"NAS100": datetime(2016, 1, 1, tzinfo=UTC)}   # NAS100 ก่อน 2016 มีแค่ ~100 แท่ง/ปี (ข้อมูลไม่ครบ)
GRID = {"entry_n": [20, 50, 100], "stop_atr": [2.0, 3.0, 4.0]}
TEST_YEARS = list(range(2013, 2023))
T_CRIT = 2.4
MIN_OPT = 30
# ความเสี่ยงต่อไม้คงที่ (vol targeting ผ่านระยะ SL ตาม ATR) · พอร์ตเปิดได้หลายตลาดพร้อมกัน
RISK = RiskConfig(risk_per_trade=0.005, daily_loss_limit=1.0, max_drawdown=1.0, max_open_groups=50,
                  max_groups_per_symbol=1)


def market_bars(end: datetime) -> dict:
    out = {}
    for s in C.DAILY_MARKETS:
        start = DATA_START.get(s, datetime(1990, 1, 1, tzinfo=UTC))
        out[s] = [b for b in load_bars(bars_path(DATA, s, "D1")) if start <= b.time < end]
    return out


SWAP_CAP = 0.065   # แบบ A: |swap| ≤ 6.5%/ปี (research/hypotheses_v4_daily.md — แก้ไข 2026-10-15)
RAW_SWAP = "rawswap" in sys.argv   # แบบ B: อัตราปัจจุบันของโบรกตรงๆ


def specs_for(symbols) -> dict:  # noqa: ANN001
    out = {}
    for s in symbols:
        sp = load_spec(DATA, s, C.DEFAULT_SPECS[s])
        if not RAW_SWAP:
            sp = replace(sp, swap_long_rate=max(sp.swap_long_rate, -SWAP_CAP),
                         swap_short_rate=max(sp.swap_short_rate, -SWAP_CAP))
        out[s] = sp
    return out


def _job(args: tuple) -> tuple:
    p, mode, end = args
    bars = market_bars(end)
    specs = specs_for(bars)
    strats = [DailyTrend(DailyTrendConfig(f"dt_{s}", s, side_mode=mode, seed=sum(map(ord, s)), **p), "D1") for s in bars]
    res = run_backtest(bars, specs, strats, RISK, "D1", 10_000_000.0, None)
    g = pd.DataFrame([asdict(x) for x in group_trades(res.trades)])
    g["open_time"] = pd.to_datetime(g["open_time"], utc=True)
    return (json.dumps(p, sort_keys=True), mode), g[["open_time", "r", "side", "symbol"]]


def run_all(end: datetime) -> dict:
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    jobs = [(p, m, end) for p in combos for m in ("rule", "random", "long")]
    with ProcessPoolExecutor(6) as ex:
        return dict(ex.map(_job, jobs))


def walk_forward(results: dict) -> dict:
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    key = lambda p, m: (json.dumps(p, sort_keys=True), m)  # noqa: E731
    rows, picked = [], {"rule": [], "random": [], "long": []}
    for y in TEST_YEARS:
        o0, o1 = pd.Timestamp(f"{y - 5}-01-01", tz="UTC"), pd.Timestamp(f"{y}-01-01", tz="UTC")
        t1 = pd.Timestamp(f"{y + 1}-01-01", tz="UTC")

        def opt(p: dict) -> float:
            g = results[key(p, "rule")]
            r = g[(g.open_time >= o0) & (g.open_time < o1)]["r"]
            return r.mean() if len(r) >= MIN_OPT else -np.inf
        exps = {json.dumps(p, sort_keys=True): opt(p) for p in combos}
        score = {k: min([exps[k]] + [exps[json.dumps(n, sort_keys=True)] for n in neighbors(json.loads(k), GRID)])
                 for k in exps}
        best = max(score, key=score.get)
        for m in picked:
            g = results[(best, m)]
            picked[m].append(g[(g.open_time >= o1) & (g.open_time < t1)])
        tr = picked["rule"][-1]["r"]
        rows.append({"test": y, "params": json.loads(best), "opt": round(score[best], 3), "n": len(tr),
                     "exp": round(tr.mean(), 3) if len(tr) else np.nan, "sum_R": round(tr.sum(), 1)})
    rule, rnd, lng = (pd.concat(picked[m]) for m in ("rule", "random", "long"))
    return {"rows": rows, "rule": rule, "random": rnd, "long": lng}


def report(wf: dict) -> dict:
    rule, rnd, lng = wf["rule"], wf["random"], wf["long"]
    s, sr, sl = (stats(x["r"].to_numpy(dtype=float)) for x in (rule, rnd, lng))
    years_pos = np.mean([r["sum_R"] > 0 for r in wf["rows"] if r["n"]])
    print(pd.DataFrame(wf["rows"]).to_string(index=False))
    longs, shorts = rule[rule.side == "BUY"]["r"], rule[rule.side == "SELL"]["r"]
    print(f"\nรวม walk-forward: {s} · ปีที่บวก {years_pos:.0%} · BUY {longs.mean():+.3f}R (n={len(longs)}) "
          f"SELL {shorts.mean():+.3f}R (n={len(shorts)})")
    print(f"benchmark สุ่มทิศ {sr} · long-only {sl}")
    print("ต่อตลาด:", {k: (len(v), round(v.mean(), 3)) for k, v in rule.groupby("symbol")["r"]})
    checks = {
        "n>=150": s["n"] >= 150, "exp>=+0.10R": s["exp"] >= 0.10, "PF>=1.2": s["pf"] >= 1.2,
        "t>=2.4": s["t"] >= T_CRIT, "years+>=60%": years_pos >= 0.6,
        "beat random": s["exp"] > sr["exp"], "beat long-only": s["exp"] > sl["exp"],
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'fail'}] {k}")
    passed = all(checks.values())
    print(f"==> T1: {'ผ่าน' if passed else 'ตก'}")
    return {"passed": passed, "wf": s, "random": sr, "long": sl, "years_positive": float(years_pos),
            "rows": wf["rows"], "checks": checks}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 200)
    OUT.mkdir(parents=True, exist_ok=True)
    if "holdout" in sys.argv:
        locked = json.loads((OUT / "locked_t1.json").read_text(encoding="utf-8"))
        p = locked["params"]
        print("HOLD-OUT 2023-01 →", p)
        res = {m: _job((p, m, HOLDOUT_END))[1] for m in ("rule", "random", "long")}
        h0 = pd.Timestamp(RESEARCH_END)
        for m, g in res.items():
            gg = g[g.open_time >= h0]
            print(f"  {m:6s} {stats(gg['r'].to_numpy(dtype=float))}  ต่อปี:",
                  {y: round(v.sum(), 1) for y, v in gg.groupby(gg.open_time.dt.year)["r"]})
        return
    print(f"swap model: {'B — อัตราปัจจุบันของโบรก' if RAW_SWAP else f'A — จำกัด {SWAP_CAP:.1%}/ปี'}")
    results = run_all(RESEARCH_END)
    summary = report(walk_forward(results))
    (OUT / "daily_trend_wf.json").write_text(json.dumps(summary, indent=1, default=str, ensure_ascii=False),
                                             encoding="utf-8")


if __name__ == "__main__":
    main()
