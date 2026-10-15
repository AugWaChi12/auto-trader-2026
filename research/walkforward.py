"""
walkforward.py — ทดสอบ H5–H8 แบบ walk-forward (ไม่แตะ hold-out 2026-04-01 →)

    python research/walkforward.py

วิธี:
  1) backtest ทุกชุดพารามิเตอร์ × 3 โหมด (rule / random / long) ตลอดช่วงวิจัย ครั้งเดียว
     (กลยุทธ์เป็น causal → ตัดผลตามหน้าต่างเวลาทีหลังได้โดยไม่ต่างจากรันแยก)
  2) ทุกหน้าต่างทดสอบ 3 เดือน: เลือกพารามิเตอร์จาก 12 เดือนก่อนหน้าด้วย "คะแนนที่ราบ"
     = expectancy ต่ำสุดของตัวมันและเพื่อนบ้านใน grid (ต้องมี ≥ 15 เทรดในช่วงเลือก)
  3) ต่อผลของช่วงทดสอบทั้งหมด → ตัดสินตามเกณฑ์ใน research/hypotheses.md
benchmark ใช้พารามิเตอร์ชุดเดียวกับที่เลือกในหน้าต่างนั้น ต่างแค่ทิศ
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.backtest.report import group_trades  # noqa: E402
from bot.core.timeutil import UTC  # noqa: E402
from bot.strategy.level_sweep import LevelSweep, LevelSweepConfig  # noqa: E402
from bot.strategy.vol_expansion import VolExpansion, VolExpansionConfig  # noqa: E402
from research.sweep import backtest  # noqa: E402

START = datetime(2023, 10, 16, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)          # hold-out เริ่มตรงนี้
FIRST_TEST = pd.Timestamp("2024-10-16", tz="UTC")
OUT = ROOT / "research" / "out"
T_CRIT = 2.7
MIN_OPT_TRADES = 15

HYPOTHESES = {
    "H5": ("vol", "XAUUSD"), "H6": ("vol", "NAS100"),
    "H7": ("sweep", "XAUUSD"), "H8": ("sweep", "NAS100"),
}
GRIDS = {
    "vol": {"box_n": [12, 24, 48], "sl_atr": [1.0, 1.5, 2.0], "tp_r": [1.5, 2.0, 3.0]},
    "sweep": {"max_bars": [1, 2, 3], "buffer_atr": [0.1, 0.25, 0.5], "tp_r": [1.5, 2.0, 3.0]},
}


def make(kind: str, symbol: str, p: dict, mode: str):
    if kind == "vol":
        return VolExpansion(VolExpansionConfig("vx", symbol, side_mode=mode, **p), "M15")
    tz = "UTC" if symbol == "XAUUSD" else "America/New_York"
    return LevelSweep(LevelSweepConfig("sw", symbol, tz=tz, side_mode=mode, **p), "M15")


def _job(args: tuple) -> tuple:
    kind, symbol, p, mode = args
    res = backtest(make(kind, symbol, p, mode), START, END)
    g = pd.DataFrame([asdict(x) for x in group_trades(res.trades)])
    if g.empty:
        g = pd.DataFrame(columns=["open_time", "r", "side"])
    g["open_time"] = pd.to_datetime(g["open_time"], utc=True)
    return (kind, symbol, tuple(sorted(p.items())), mode), g[["open_time", "r", "side"]]


def windows() -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    out, t = [], FIRST_TEST
    end = pd.Timestamp(END)
    while t < end:
        out.append((t - pd.DateOffset(months=12), t, t, min(t + pd.DateOffset(months=3), end)))
        t = t + pd.DateOffset(months=3)
    return out


def neighbors(p: dict, grid: dict) -> list[dict]:
    out = []
    for k, opts in grid.items():
        i = opts.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(opts):
                out.append({**p, k: opts[j]})
    return out


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"n": 0, "exp": np.nan, "t": np.nan, "pf": np.nan}
    win, loss = r[r > 0].sum(), -r[r <= 0].sum()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else np.nan
    return {"n": len(r), "exp": round(float(r.mean()), 3), "t": round(float(t), 2),
            "pf": round(float(win / loss), 2) if loss > 0 else np.inf}


def evaluate(hid: str, kind: str, symbol: str, results: dict) -> dict:
    grid = GRIDS[kind]
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    key = lambda p, m: (kind, symbol, tuple(sorted(p.items())), m)  # noqa: E731

    rows, test_rule, test_rand, test_long = [], [], [], []
    for o0, o1, t0, t1 in windows():
        def opt_exp(p: dict) -> float:
            g = results[key(p, "rule")]
            r = g[(g.open_time >= o0) & (g.open_time < o1)]["r"]
            return r.mean() if len(r) >= MIN_OPT_TRADES else -np.inf
        exps = {tuple(sorted(p.items())): opt_exp(p) for p in combos}
        score = {k: min([exps[k]] + [exps[tuple(sorted(n.items()))] for n in neighbors(dict(k), grid)]) for k in exps}
        best = max(score, key=score.get)
        p = dict(best)
        sel = lambda m: results[key(p, m)].pipe(lambda g: g[(g.open_time >= t0) & (g.open_time < t1)])  # noqa: E731
        rule, rnd, lng = sel("rule"), sel("random"), sel("long")
        test_rule.append(rule)
        test_rand.append(rnd)
        test_long.append(lng)
        rows.append({"test": f"{t0:%Y-%m-%d}→{t1:%Y-%m-%d}", "params": p, "opt_score": round(score[best], 3),
                     "n": len(rule), "exp": round(rule["r"].mean(), 3) if len(rule) else np.nan,
                     "rand_exp": round(rnd["r"].mean(), 3) if len(rnd) else np.nan})

    rule, rnd, lng = (pd.concat(x) if x else pd.DataFrame(columns=["r", "side"]) for x in (test_rule, test_rand, test_long))
    r = rule["r"].to_numpy(dtype=float)
    s = stats(r)
    s_rand, s_long = stats(rnd["r"].to_numpy(dtype=float)), stats(lng["r"].to_numpy(dtype=float))
    wins = [w for w in rows if w["n"] > 0]
    pos_frac = np.mean([w["exp"] > 0 for w in wins]) if wins else 0.0
    longs, shorts = rule[rule.side == "BUY"]["r"], rule[rule.side == "SELL"]["r"]
    checks = {
        "n>=80": s["n"] >= 80,
        "exp>=+0.10R": s["n"] > 0 and s["exp"] >= 0.10,
        "PF>=1.2": s["n"] > 0 and s["pf"] >= 1.2,
        "t>=2.7": s["n"] > 2 and s["t"] >= T_CRIT,
        "windows+>=60%": pos_frac >= 0.6,
        "beat random": s["n"] > 0 and s_rand["n"] > 0 and s["exp"] > s_rand["exp"],
        "beat long-only": s["n"] > 0 and s_long["n"] > 0 and s["exp"] > s_long["exp"],
    }
    print(f"\n######## {hid}  {kind} {symbol}")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n  walk-forward รวม: {s}  | long {longs.mean():+.3f}R (n={len(longs)}) short {shorts.mean():+.3f}R (n={len(shorts)})"
          if len(r) else "\n  walk-forward รวม: ไม่มีเทรด")
    print(f"  benchmark สุ่มทิศ: {s_rand} | long-only: {s_long} | หน้าต่างบวก {pos_frac:.0%}")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'fail'}] {k}")
    passed = all(checks.values())
    print(f"  ==> {hid}: {'ผ่าน' if passed else 'ตก'}")
    return {"hid": hid, "kind": kind, "symbol": symbol, "passed": passed, "wf": s, "random": s_rand, "long": s_long,
            "windows_positive": round(float(pos_frac), 2), "checks": checks, "windows": rows}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 60)
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = []
    for kind, symbol in HYPOTHESES.values():
        grid = GRIDS[kind]
        for v in itertools.product(*grid.values()):
            for mode in ("rule", "random", "long"):
                jobs.append((kind, symbol, dict(zip(grid, v)), mode))
    print(f"backtests: {len(jobs)}  ({START:%Y-%m-%d} → {END:%Y-%m-%d}, ไม่แตะ hold-out)")
    with ProcessPoolExecutor(6) as ex:
        results = dict(ex.map(_job, jobs, chunksize=4))
    summary = [evaluate(hid, kind, sym, results) for hid, (kind, sym) in HYPOTHESES.items()]
    (OUT / "walkforward_v2.json").write_text(json.dumps(summary, indent=1, default=str, ensure_ascii=False),
                                             encoding="utf-8")
    print("\nสรุป:", {s["hid"]: ("ผ่าน" if s["passed"] else "ตก") for s in summary})


if __name__ == "__main__":
    main()
