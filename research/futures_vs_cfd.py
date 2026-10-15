"""
futures_vs_cfd.py — T4: trend following รายวันล้มเพราะไอเดีย หรือเพราะเครื่องมือ CFD?

    python research/futures_vs_cfd.py            ช่วงวิจัย (walk-forward 2013–2022) 3 โมเดลต้นทุน
    python research/futures_vs_cfd.py holdout    hold-out 2023 → (เมื่อ T4 ผ่านเท่านั้น)

ใช้ราคา CFD ของ FxPro เป็นตัวแทนการเคลื่อนไหวของราคา แล้วเปลี่ยนแค่ "ต้นทุน" ตามเครื่องมือ:
  futures : micro futures (MGC, SIL, MNQ, MYM, MES) — สเปรด 1 tick, คอม micro, ถือ: long จ่าย r − ปันผล / short ได้ r − ปันผล
  cfd     : ต้นทุน FxPro (สเปรด/คอมเดิม) — ถือ: long จ่าย r + 2.5% − ปันผล / short ได้ max(r − 2.5% − ปันผล, 0)
  zero    : ไม่มีต้นทุนถือเลย (เพดานบน ไม่ใช่ของจริง)
r(ปี) = ดอกเบี้ยนโยบายสหรัฐเฉลี่ยรายปี (ค่าประมาณ) · ปันผล = ค่าเฉลี่ยโดยประมาณ — ทั้งสองเป็นสมมติฐานที่ระบุไว้
ไม่คิดดอกเบี้ยที่เงินสดในบัญชี futures ได้รับ (ด้านระวัง)
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.report import group_trades  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.core.timeutil import UTC  # noqa: E402
from bot.core.types import SymbolSpec  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.strategy.daily_trend import DailyTrend, DailyTrendConfig  # noqa: E402
from research.daily_trend_wf import GRID, RISK, walk_forward, report  # noqa: E402
from research.walkforward import stats  # noqa: E402

DATA = ROOT / "data" / "mt5"
OUT = ROOT / "research" / "out"
MARKETS = ["XAUUSD", "XAGUSD", "NAS100", "US30", "SPX500"]
DATA_START = {"NAS100": datetime(2016, 1, 1, tzinfo=UTC)}
RESEARCH_END = datetime(2023, 1, 1, tzinfo=UTC)
HOLDOUT_END = datetime(2026, 10, 16, tzinfo=UTC)

# ดอกเบี้ยนโยบายสหรัฐเฉลี่ยรายปี (%, ค่าประมาณ) — 2025–2026 เป็นค่าประมาณคร่าวๆ
FED_FUNDS = {2006: 4.97, 2007: 5.02, 2008: 1.93, 2009: 0.16, 2010: 0.18, 2011: 0.10, 2012: 0.14, 2013: 0.11,
             2014: 0.09, 2015: 0.13, 2016: 0.40, 2017: 1.00, 2018: 1.83, 2019: 2.16, 2020: 0.38, 2021: 0.08,
             2022: 1.68, 2023: 5.02, 2024: 5.14, 2025: 4.20, 2026: 3.75}
DIVIDEND = {"NAS100": 0.8, "US30": 2.0, "SPX500": 1.8}   # %/ปี โดยประมาณ (โลหะ = 0)
CFD_MARKUP = 2.5                                         # %/ปี — ส่วนต่างของโบรกเหนือดอกเบี้ยตลาด (FxPro วันนี้ ≈ 6.1–6.6% vs r ≈ 3.75%)

# micro futures: (contract multiplier, tick, ค่าคอมไป-กลับต่อสัญญา USD)
MICRO = {"XAUUSD": (10, 0.10, 2.0), "XAGUSD": (1000, 0.005, 2.0), "NAS100": (2, 0.25, 1.5),
         "US30": (0.5, 1.0, 1.5), "SPX500": (5, 0.25, 1.5)}


def carry(symbol: str, model: str) -> tuple:
    d = DIVIDEND.get(symbol, 0.0)
    rows = []
    for y, r in FED_FUNDS.items():
        if model == "futures":
            long_r, short_r = -(r - d), (r - d)
        elif model == "cfd":
            long_r, short_r = -(r + CFD_MARKUP - d), max(r - CFD_MARKUP - d, 0.0)
        else:
            long_r = short_r = 0.0
        rows.append((y, long_r / 100, short_r / 100))
    return tuple(rows)


def spec_for(symbol: str, model: str) -> SymbolSpec:
    if model == "futures":
        mult, tick, comm = MICRO[symbol]
        base = SymbolSpec(name=symbol, point=tick, contract_size=mult, volume_min=1, volume_step=1, volume_max=10_000,
                          min_spread=tick, slippage=tick, commission_per_lot=comm, margin_rate=0.05)
    else:
        base = load_spec(DATA, symbol, C.DEFAULT_SPECS[symbol])
    return replace(base, swap_long_rate=0.0, swap_short_rate=0.0, carry=carry(symbol, model))


def _job(args: tuple) -> tuple:
    p, mode, model, end = args
    bars = {}
    for s in MARKETS:
        start = DATA_START.get(s, datetime(1990, 1, 1, tzinfo=UTC))
        bars[s] = [b for b in load_bars(bars_path(DATA, s, "D1")) if start <= b.time < end]
    specs = {s: spec_for(s, model) for s in MARKETS}
    strats = [DailyTrend(DailyTrendConfig(f"dt_{s}", s, side_mode=mode, seed=sum(map(ord, s)), **p), "D1")
              for s in MARKETS]
    res = run_backtest(bars, specs, strats, RISK, "D1", 10_000_000.0, None)
    g = pd.DataFrame([asdict(x) for x in group_trades(res.trades)])
    g["open_time"] = pd.to_datetime(g["open_time"], utc=True)
    return (json.dumps(p, sort_keys=True), mode), g[["open_time", "r", "side", "symbol"]]


def run_model(model: str, end: datetime) -> dict:
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    jobs = [(p, m, model, end) for p in combos for m in ("rule", "random", "long")]
    with ProcessPoolExecutor(6) as ex:
        return dict(ex.map(_job, jobs))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 200)
    if "holdout" in sys.argv:
        locked = json.loads((OUT / "locked_t4.json").read_text(encoding="utf-8"))
        p = locked["params"]
        print("HOLD-OUT 2023-01 → (futures)", p)
        for m in ("rule", "random", "long"):
            g = _job((p, m, "futures", HOLDOUT_END))[1]
            g = g[g.open_time >= pd.Timestamp(RESEARCH_END)]
            print(f"  {m:6s} {stats(g['r'].to_numpy(dtype=float))}  ต่อปี:",
                  {y: round(v.sum(), 1) for y, v in g.groupby(g.open_time.dt.year)["r"]})
        return
    out = {}
    for model in ("futures", "cfd", "zero"):
        print(f"\n################ โมเดลต้นทุน: {model}")
        out[model] = report(walk_forward(run_model(model, RESEARCH_END)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "futures_vs_cfd.json").write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print("\nสรุป (expectancy walk-forward):", {k: v["wf"]["exp"] for k, v in out.items()},
          "· T4 (futures):", "ผ่าน" if out["futures"]["passed"] else "ตก")


if __name__ == "__main__":
    main()
