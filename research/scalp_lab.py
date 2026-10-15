"""
scalp_lab.py — ทดสอบ S1–S4 (research/hypotheses_v3_scalp.md)

    python research/scalp_lab.py

M5: walk-forward optimize 6 เดือน → test 2 เดือน · M1: tune ถึง 2026-09-15 → test ครั้งเดียว
เลือกพารามิเตอร์ด้วยคะแนนที่ราบ (expectancy ต่ำสุดของตัวมันและเพื่อนบ้าน, ต้องมี ≥ 30 เทรดในช่วงเลือก)
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
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
from bot.strategy.scalp import (BollingerFade, BollingerFadeConfig, EmaCross, EmaCrossConfig,  # noqa: E402
                                RsiReversion, RsiReversionConfig)
from research.sweep import RESEARCH_RISK  # noqa: E402
from research.walkforward import neighbors, stats  # noqa: E402

OUT = ROOT / "research" / "out"
DATA = ROOT / "data" / "mt5"
T_CRIT = 2.5
MIN_OPT = 30

HYP = {
    "S1": dict(kind="rsi", symbol="XAUUSD", tf="M5", min_n=150,
               grid={"rsi_th": [5.0, 10.0, 15.0], "sl_atr": [1.0, 1.5, 2.0], "max_hold": [6, 12, 24]}),
    "S2": dict(kind="rsi", symbol="NAS100", tf="M5", min_n=150,
               grid={"rsi_th": [5.0, 10.0, 15.0], "sl_atr": [1.0, 1.5, 2.0], "max_hold": [6, 12, 24]}),
    "S3": dict(kind="cross", symbol="XAUUSD", tf="M5", min_n=150,
               grid={"pair": [(5, 13), (9, 21), (12, 26)], "sl_atr": [1.0, 1.5, 2.0], "tp_r": [1.0, 1.5, 2.0]}),
    "S4": dict(kind="bb", symbol="XAUUSD", tf="M1", min_n=80,
               grid={"k": [2.0, 2.5, 3.0], "sl_atr": [1.0, 1.5, 2.0], "max_hold": [15, 30, 60]}),
}

_BARS: dict = {}


def bars(symbol: str, tf: str) -> list:
    if (symbol, tf) not in _BARS:
        _BARS[(symbol, tf)] = load_bars(bars_path(DATA, symbol, tf))
    return _BARS[(symbol, tf)]


def make(kind: str, symbol: str, tf: str, p: dict, mode: str):
    if kind == "rsi":
        return RsiReversion(RsiReversionConfig("rsi", symbol, side_mode=mode, **p), tf)
    if kind == "cross":
        f, s = p["pair"]
        return EmaCross(EmaCrossConfig("cross", symbol, fast=f, slow=s, sl_atr=p["sl_atr"], tp_r=p["tp_r"],
                                       side_mode=mode), tf)
    return BollingerFade(BollingerFadeConfig("bb", symbol, side_mode=mode, **p), tf)


def _job(args: tuple) -> tuple:
    hid, p, mode = args
    h = HYP[hid]
    b = bars(h["symbol"], h["tf"])
    spec = load_spec(DATA, h["symbol"], C.DEFAULT_SPECS[h["symbol"]])
    news = NewsCalendar.default(C.NEWS_CSV, (b[0].time.year, b[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({h["symbol"]: b}, {h["symbol"]: spec}, [make(h["kind"], h["symbol"], h["tf"], p, mode)],
                       RESEARCH_RISK, h["tf"], 1_000_000.0, news)
    g = pd.DataFrame([asdict(x) for x in group_trades(res.trades)])
    if g.empty:
        g = pd.DataFrame(columns=["open_time", "r", "side"])
    g["open_time"] = pd.to_datetime(g["open_time"], utc=True)
    return (hid, json.dumps(p, sort_keys=True, default=list), mode), g[["open_time", "r", "side"]]


def windows(hid: str) -> list[tuple]:
    if HYP[hid]["tf"] == "M1":
        return [(pd.Timestamp("2026-07-06", tz="UTC"), pd.Timestamp("2026-09-15", tz="UTC"),
                 pd.Timestamp("2026-09-15", tz="UTC"), pd.Timestamp("2026-10-16", tz="UTC"))]
    out, t, end = [], pd.Timestamp("2025-11-19", tz="UTC"), pd.Timestamp("2026-10-16", tz="UTC")
    while t < end:
        out.append((t - pd.DateOffset(months=6), t, t, min(t + pd.DateOffset(months=2), end)))
        t += pd.DateOffset(months=2)
    return out


def evaluate(hid: str, results: dict) -> dict:
    h = HYP[hid]
    grid = h["grid"]
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    k = lambda p, m: (hid, json.dumps(p, sort_keys=True, default=list), m)  # noqa: E731
    rows, picked = [], {"rule": [], "random": [], "long": []}
    for o0, o1, t0, t1 in windows(hid):
        def opt(p: dict) -> float:
            g = results[k(p, "rule")]
            r = g[(g.open_time >= o0) & (g.open_time < o1)]["r"]
            return r.mean() if len(r) >= MIN_OPT else -np.inf
        exps = {json.dumps(p, sort_keys=True, default=list): opt(p) for p in combos}
        score = {}
        for p in combos:
            kk = json.dumps(p, sort_keys=True, default=list)
            nb = [exps[json.dumps(n, sort_keys=True, default=list)] for n in neighbors(p, grid)]
            score[kk] = min([exps[kk]] + nb)
        best_key = max(score, key=score.get)
        best = json.loads(best_key)
        if "pair" in best:
            best["pair"] = tuple(best["pair"])
        for m in picked:
            g = results[k(best, m)]
            picked[m].append(g[(g.open_time >= t0) & (g.open_time < t1)])
        tr = picked["rule"][-1]["r"]
        rows.append({"test": f"{t0:%Y-%m-%d}→{t1:%Y-%m-%d}", "params": best, "opt": round(score[best_key], 3),
                     "n": len(tr), "exp": round(tr.mean(), 3) if len(tr) else np.nan})
    rule, rnd, lng = (pd.concat(picked[m]) for m in ("rule", "random", "long"))
    s, s_r, s_l = (stats(x["r"].to_numpy(dtype=float)) for x in (rule, rnd, lng))
    longs, shorts = rule[rule.side == "BUY"]["r"], rule[rule.side == "SELL"]["r"]
    checks = {
        f"n>={h['min_n']}": s["n"] >= h["min_n"],
        "exp>=+0.05R": s["n"] > 0 and s["exp"] >= 0.05,
        "PF>=1.15": s["n"] > 0 and s["pf"] >= 1.15,
        "t>=2.5": s["n"] > 2 and s["t"] >= T_CRIT,
        "beat random": s["n"] > 0 and s_r["n"] > 0 and s["exp"] > s_r["exp"],
        "beat long-only": s["n"] > 0 and s_l["n"] > 0 and s["exp"] > s_l["exp"],
    }
    # ต่อวันกี่ไม้ / ต้นทุนรวมคิดเป็นกี่ R (ภาพของ "เข้าๆ ออกๆ")
    days = max((rule["open_time"].max() - rule["open_time"].min()).days, 1) if len(rule) else 1
    print(f"\n######## {hid}  {h['kind']} {h['symbol']} {h['tf']}")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n  รวมช่วงทดสอบ: {s}  ≈ {len(rule) / days * 7 / 5:.1f} ไม้/วันทำการ"
          + (f" | long {longs.mean():+.3f}R short {shorts.mean():+.3f}R" if len(rule) else ""))
    print(f"  benchmark สุ่มทิศ {s_r} | long-only {s_l}")
    for kk, v in checks.items():
        print(f"  [{'PASS' if v else 'fail'}] {kk}")
    passed = all(checks.values())
    print(f"  ==> {hid}: {'ผ่าน' if passed else 'ตก'}")
    return {"hid": hid, **{x: h[x] for x in ("kind", "symbol", "tf")}, "passed": passed, "test": s, "random": s_r,
            "long": s_l, "windows": rows, "latest_params": rows[-1]["params"] if rows else None,
            "trades_per_day": round(len(rule) / days * 7 / 5, 1)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 70)
    jobs = [(hid, dict(zip(h["grid"], v)), m) for hid, h in HYP.items()
            for v in itertools.product(*h["grid"].values()) for m in ("rule", "random", "long")]
    print(f"backtests: {len(jobs)}")
    with ProcessPoolExecutor(6) as ex:
        results = dict(ex.map(_job, jobs, chunksize=3))
    summary = [evaluate(hid, results) for hid in HYP]
    (OUT / "scalp_lab.json").write_text(json.dumps(summary, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print("\nสรุป:", {s["hid"]: ("ผ่าน" if s["passed"] else "ตก") for s in summary})


if __name__ == "__main__":
    main()
