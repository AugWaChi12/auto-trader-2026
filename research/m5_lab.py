"""
m5_lab.py — ห้องวิจัยของบอททดสอบ 9 ตัว (M5 · ทอง 0.04 lot · NAS เทียบเท่า · หยุดวันที่ได้ +$120)

    python research/m5_lab.py            # วิจัยเต็ม → research/out/m5_lab.json + research/RESULTS_m5.md
    python research/m5_lab.py --assign   # + เขียนงานให้บอท → bot/m5_bots.json (บอทอ่านตอนเริ่ม)

ทุกบอท "วิจัยตัวเอง" ด้วยกติกาเดียวกัน:
  1) ทุกตระกูลกลยุทธ์ × ทุกพารามิเตอร์ × 3 วิธีออก (ตามกลยุทธ์ / trail ATR / ล็อกกำไรแบบไต่) × 2 ตลาด
  2) walk-forward: เลือกจาก 6 เดือนก่อนหน้า → ทดสอบ 2 เดือนถัดไป (ไม่เคยเห็น) ต่อกันจนถึง hold-out
     คะแนนเลือก = ค่าเฉลี่ย $/ไม้ ของครึ่งที่แย่กว่าในช่วงเลือก (ต้องดีทั้งสองครึ่ง ไม่ใช่ดวงช่วงเดียว)
  3) จัดอันดับ "ช่อง" (ตระกูล × ตลาด) ด้วยผล walk-forward → 9 ช่องที่ดีสุดได้เป็นบอท
  4) hold-out 2 เดือนล่าสุด: ตรวจซ้ำครั้งเดียวด้วยชุดที่เลือกก่อนถึง hold-out
  5) เทียบกลุ่มควบคุมสุ่มทิศ (จังหวะเข้า/SL/TP เดียวกัน) — ชนะสุ่มไม่ได้ = ไม่มี edge
พารามิเตอร์ที่บอทใช้จริง = ชุดที่ชนะใน 6 เดือนล่าสุด (รวม hold-out) ตามกติกาเดียวกัน
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.risk.risk_manager import RiskConfig  # noqa: E402
from bot.strategy.catalog import build  # noqa: E402
from bot.strategy.levels import ALWAYS_OPEN  # noqa: E402

import logging  # noqa: E402
logging.disable(logging.WARNING)   # ข้อความ "ถึงเป้ารายวัน" ของ backtest เป็นพันบรรทัด

TF = "M5"
DATA = ROOT / "data" / "mt5"
OUT = ROOT / "research" / "out"
ASSIGN = ROOT / "bot" / "m5_bots.json"
# ทุกตลาดแกว่งเป็นเงินเท่ากันต่อ 1 ATR ของ M5 (≈ $17): ทอง 4.2 × 100 × 0.04 · NAS 23 × 0.73 · BTC 97 × 0.17
LOTS = {"XAUUSD": 0.04, "NAS100": 0.73, "BTCUSD": 0.17}
TARGET = 120.0
N_BOTS = 9
MIN_SLOTS = {"BTCUSD": 3}       # จำนวนบอทขั้นต่ำต่อตลาด (ตามคำขอผู้ใช้)
TRAIN_M, TEST_M, HOLDOUT_M = 6, 2, 2
MIN_TRAIN_TRADES = 40

RISK = RiskConfig(fixed_lots=tuple(LOTS.items()), daily_profit_target=TARGET, daily_loss_limit=1.0,
                  max_drawdown=1.0, max_open_groups=50, max_groups_per_symbol=50, close_on_daily_halt=False)

# ล็อกกำไรแบบไต่ (ดอลลาร์ ที่ขนาดไม้ข้างบน ≈ 0.5 / 1 / 2 ATR)
PLANS = list(C.M5_PLANS)          # native / trail / ratchet — ตัวเดียวกับที่บอท live ใช้


def _grid(**kw) -> list[dict]:
    return [dict(zip(kw, v)) for v in itertools.product(*kw.values())]


FAMILIES: dict[str, tuple] = {
    # ชื่อ: (grid, รองรับสุ่มทิศ) — วิธีสร้างอยู่ที่ bot/strategy/catalog.py
    "rsi2": (_grid(rsi_th=[5.0, 10.0, 15.0, 20.0], sl_atr=[1.0, 1.5, 2.0, 3.0], max_hold=[6, 12, 24, 48],
                   tp_r=[None, 1.0, 2.0]), True),
    "ema": ([dict(fast=f, slow=s, **g) for (f, s) in [(5, 13), (9, 21), (12, 26), (20, 50)]
             for g in _grid(sl_atr=[1.0, 1.5, 2.0, 3.0], tp_r=[1.0, 1.5, 2.0, 3.0, 4.0], max_hold=[24, 96])], True),
    "bb": (_grid(k=[1.5, 2.0, 2.5, 3.0], sl_atr=[1.0, 1.5, 2.0, 3.0], tp_r=[None, 1.0, 2.0], max_hold=[12, 36]), True),
    "donchian": (_grid(entry_n=[12, 24, 48, 96, 192], stop_atr=[1.0, 1.5, 2.0, 3.0, 4.0]),
                 True),
    "pullback": (_grid(trend_tf_min=[60, 240], trend_ema=[20, 50], pullback_ema=[10, 20], sl_atr=[1.0, 1.5, 2.0],
                       tp_r=[1.5, 2.0, 3.0]), False),
    "volx": (_grid(box_n=[12, 24, 48], sl_atr=[1.0, 1.5, 2.0], tp_r=[1.5, 2.0, 3.0], max_hold_h=[6, 24]), True),
    "levels": (_grid(max_bars=[1, 2, 3], buffer_atr=[0.1, 0.25, 0.5], tp_r=[1.5, 2.0, 3.0]), True),
    "orb": (_grid(sl_mode=["mid", "opposite"], tp_r=[None, 1.0, 2.0, 3.0], buffer_adr=[0.0, 0.02, 0.05]), False),
    "momo": (_grid(entry_time=["11:00", "12:00", "13:00", "14:00"], min_move_adr=[0.2, 0.35, 0.5],
                   sl_adr=[0.3, 0.5, 0.8]), True),
}
SYMBOLS = ["XAUUSD", "NAS100", "BTCUSD"]


def make(family: str, symbol: str, params: dict, plan: str, mode: str = "rule", tag: str = "s"):
    return build(family, symbol, params, C.M5_PLANS[plan], TF, tag, mode)


@lru_cache(maxsize=2)
def _market(symbol: str):
    bars = load_bars(bars_path(DATA, symbol, TF))
    spec = load_spec(DATA, symbol, C.DEFAULT_SPECS[symbol])
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    return bars, spec, news


def run(args: tuple) -> tuple:
    family, symbol, pi, plan, mode = args
    bars, spec, news = _market(symbol)
    params = FAMILIES[family][0][pi]
    res = run_backtest({symbol: bars}, {symbol: spec}, [make(family, symbol, params, plan, mode)], RISK, TF,
                       1_000_000.0, news)
    t = np.array([tr.open_time.timestamp() for tr in res.trades], dtype=float)
    p = np.array([tr.pnl for tr in res.trades], dtype=float)
    return args, t, p


# ---------------------------------------------------------------------------- walk-forward
def windows(start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[tuple], pd.Timestamp]:
    hold = end - pd.DateOffset(months=HOLDOUT_M)
    out, t = [], start + pd.DateOffset(months=TRAIN_M)
    while t < hold:
        out.append((t - pd.DateOffset(months=TRAIN_M), t, t, min(t + pd.DateOffset(months=TEST_M), hold)))
        t += pd.DateOffset(months=TEST_M)
    return out, hold


def _slice(t: np.ndarray, p: np.ndarray, a: pd.Timestamp, b: pd.Timestamp) -> np.ndarray:
    return p[(t >= a.timestamp()) & (t < b.timestamp())]


def score(t: np.ndarray, p: np.ndarray, a: pd.Timestamp, b: pd.Timestamp) -> float:
    x = _slice(t, p, a, b)
    if len(x) < MIN_TRAIN_TRADES:
        return -np.inf
    mid = a + (b - a) / 2
    h1, h2 = _slice(t, p, a, mid), _slice(t, p, mid, b)
    if len(h1) < 10 or len(h2) < 10:
        return -np.inf
    return float(min(h1.mean(), h2.mean()))


def select(results: dict, family: str, symbol: str, a: pd.Timestamp, b: pd.Timestamp) -> tuple:
    best, best_s = None, -np.inf
    for pi in range(len(FAMILIES[family][0])):
        for plan in PLANS:
            t, p = results[(family, symbol, pi, plan, "rule")]
            s = score(t, p, a, b)
            if s > best_s:
                best, best_s = (pi, plan), s
    return best, best_s


def days(a: pd.Timestamp, b: pd.Timestamp, symbol: str = "XAUUSD") -> float:
    return max((b - a).days * (1 if symbol in ALWAYS_OPEN else 5 / 7), 1)   # วันที่ตลาดเปิด (BTC เปิด 7 วัน)


def summarize(x: np.ndarray, n_days: float) -> dict:
    if len(x) == 0:
        return {"n": 0, "usd_day": 0.0, "usd_trade": None, "win": None, "pf": None, "t": None, "ci95_day": None}
    win, loss = x[x > 0].sum(), -x[x <= 0].sum()
    t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else 0.0
    se_day = x.std(ddof=1) * np.sqrt(len(x)) / n_days if len(x) > 2 else 0.0
    return {"n": int(len(x)), "usd_day": round(float(x.sum() / n_days), 2), "usd_trade": round(float(x.mean()), 2),
            "win": round(float((x > 0).mean() * 100), 1), "pf": round(float(win / loss), 2) if loss > 0 else None,
            "t": round(float(t), 2),
            "ci95_day": [round(float(x.sum() / n_days - 1.96 * se_day), 1), round(float(x.sum() / n_days + 1.96 * se_day), 1)]}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    assign = "--assign" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(f, s, pi, plan, "rule") for f in FAMILIES for s in SYMBOLS for pi in range(len(FAMILIES[f][0]))
            for plan in PLANS]
    print(f"backtests: {len(jobs)} ({TF}, lots {LOTS}, เป้า +${TARGET:.0f}/วัน)", flush=True)
    with ProcessPoolExecutor(24) as ex:
        results = {k: (t, p) for k, t, p in ex.map(run, jobs, chunksize=8)}

    span = {}
    for s in SYMBOLS:              # แต่ละตลาดมีข้อมูลยาวไม่เท่ากัน (BTC เริ่ม พ.ย. 2025) → หน้าต่างของตัวเอง
        b = _market(s)[0]
        st, en = pd.Timestamp(b[0].time), pd.Timestamp(b[-1].time)
        w, h = windows(st, en)
        span[s] = {"start": st, "end": en, "wins": w, "hold": h, "wf_days": sum(days(x[2], x[3], s) for x in w),
                   "hold_days": days(h, en, s)}

    slots, rnd_jobs = [], []
    for f in FAMILIES:
        for s in SYMBOLS:
            wins, hold, end = span[s]["wins"], span[s]["hold"], span[s]["end"]
            wf_days, hold_days = span[s]["wf_days"], span[s]["hold_days"]
            oos, picks = [], []
            for o0, o1, t0, t1 in wins:
                sel, sc = select(results, f, s, o0, o1)
                picks.append({"test": f"{t0:%Y-%m-%d}→{t1:%Y-%m-%d}", "pick": sel, "train_score": round(sc, 2)})
                if sel is not None:
                    t, p = results[(f, s, sel[0], sel[1], "rule")]
                    oos.append(_slice(t, p, t0, t1))
                    if FAMILIES[f][1]:
                        rnd_jobs.append((f, s, sel[0], sel[1], "random"))
            pre, _ = select(results, f, s, hold - pd.DateOffset(months=TRAIN_M), hold)   # ชุดที่เลือกก่อน hold-out
            live, live_sc = select(results, f, s, end - pd.DateOffset(months=TRAIN_M), end + pd.Timedelta(days=1))
            ho = _slice(*results[(f, s, pre[0], pre[1], "rule")], hold, end + pd.Timedelta(days=1)) if pre else np.array([])
            if pre and FAMILIES[f][1]:
                rnd_jobs.append((f, s, pre[0], pre[1], "random"))
            x = np.concatenate(oos) if oos else np.array([])
            slots.append({"family": f, "symbol": s, "wf": summarize(x, wf_days), "holdout": summarize(ho, hold_days),
                          "windows_positive": f"{sum(len(o) > 0 and o.sum() > 0 for o in oos)}/{len(wins)}",
                          "picks": picks, "pre_holdout": pre, "live": live, "live_score": round(live_sc, 2),
                          "_oos": oos, "_ho": ho})

    # กลุ่มควบคุมสุ่มทิศ: ใช้ชุดที่ walk-forward เลือก ต่างแค่ทิศ
    print(f"random benchmarks: {len(set(rnd_jobs))}", flush=True)
    with ProcessPoolExecutor(24) as ex:
        rnd = {k: (t, p) for k, t, p in ex.map(run, sorted(set(rnd_jobs)), chunksize=2)}
    for sl in slots:
        f, s = sl["family"], sl["symbol"]
        if not FAMILIES[f][1]:
            sl["wf_random"] = None
            continue
        xs = []
        for (o0, o1, t0, t1), pk in zip(span[s]["wins"], sl["picks"]):
            if pk["pick"] is not None:
                xs.append(_slice(*rnd[(f, s, pk["pick"][0], pk["pick"][1], "random")], t0, t1))
        sl["wf_random"] = summarize(np.concatenate(xs) if xs else np.array([]), span[s]["wf_days"])

    slots.sort(key=lambda z: z["wf"]["usd_day"], reverse=True)
    for z in slots:
        z.pop("_oos"), z.pop("_ho")
        for k in ("pre_holdout", "live"):
            if z[k]:
                z[k] = {"params": FAMILIES[z["family"]][0][z[k][0]], "plan": z[k][1]}
        for pk in z["picks"]:
            if pk["pick"]:
                pk["pick"] = {"params": FAMILIES[z["family"]][0][pk["pick"][0]], "plan": pk["pick"][1]}
    meta = {"tf": TF, "lots": LOTS, "target": TARGET, "backtests": len(jobs) + len(set(rnd_jobs)),
            "markets": {s: {"data": [str(v["start"]), str(v["end"])], "holdout_from": str(v["hold"]),
                            "wf_days": round(v["wf_days"]), "holdout_days": round(v["hold_days"]),
                            "wf_windows": len(v["wins"])} for s, v in span.items()}}
    (OUT / "m5_lab.json").write_text(json.dumps({"meta": meta, "slots": slots}, indent=1, default=str,
                                                ensure_ascii=False), encoding="utf-8")
    report(meta, slots)
    if assign:
        write_assignments(meta, slots)


def report(meta: dict, slots: list[dict]) -> None:
    rows = []
    for z in slots:
        w, h, r = z["wf"], z["holdout"], z.get("wf_random")
        rows.append({"ช่อง": f"{z['family']}·{z['symbol'][:3]}", "WF $/วัน": w["usd_day"], "CI95": w["ci95_day"],
                     "ไม้": w["n"], "ชนะ%": w["win"], "PF": w["pf"], "t": w["t"], "หน้าต่างบวก": z["windows_positive"],
                     "สุ่มทิศ $/วัน": r["usd_day"] if r else "–", "hold-out $/วัน": h["usd_day"], "hold-out ไม้": h["n"]})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(df.to_string(index=False))
    md = ["# M5 lab — บอททดสอบ 9 ตัววิจัยตัวเอง", "",
          f"{meta['tf']} · lot {meta['lots']} · เป้า +${meta['target']:.0f}/วัน · backtest {meta['backtests']} ครั้ง · "
          f"เลือก {TRAIN_M} เดือน → ทดสอบ {TEST_M} เดือน", "",
          *[f"- {s}: ข้อมูล {m['data'][0][:10]} → {m['data'][1][:10]} · walk-forward {m['wf_windows']} หน้าต่าง "
            f"{m['wf_days']} วัน · hold-out {m['holdout_days']} วัน ตั้งแต่ {m['holdout_from'][:10]}"
            for s, m in meta["markets"].items()], "",
          _md(df), ""]
    (ROOT / "research" / "RESULTS_m5.md").write_text("\n".join(md), encoding="utf-8")


def _md(df: pd.DataFrame) -> str:
    head = "| " + " | ".join(df.columns) + " |\n|" + "---|" * len(df.columns)
    return "\n".join([head] + ["| " + " | ".join(str(v) for v in r) + " |" for r in df.itertuples(index=False)])


def write_assignments(meta: dict, slots: list[dict]) -> None:
    bots = []
    ok = [z for z in slots if z["live"] and z["wf"]["n"] > 0]          # เรียงตามผล walk-forward แล้ว
    # ผู้ใช้ขอเล่น BTC (2026-10-17): จองช่อง BTC ที่ดีที่สุดตาม walk-forward ไว้ก่อน แม้ผลจะติดลบ
    reserved = [z for s, k in MIN_SLOTS.items() for z in [z for z in ok if z["symbol"] == s][:k]]
    rest = [z for z in ok if z not in reserved][:N_BOTS - len(reserved)]
    for z in [z for z in ok if z in reserved or z in rest]:
        bots.append({"bot": f"m5_{len(bots) + 1}", "family": z["family"], "symbol": z["symbol"], "lot": LOTS[z["symbol"]],
                     "params": z["live"]["params"], "plan": z["live"]["plan"],
                     "wf_usd_day": z["wf"]["usd_day"], "holdout_usd_day": z["holdout"]["usd_day"],
                     "random_usd_day": z["wf_random"]["usd_day"] if z.get("wf_random") else None})
    ASSIGN.write_text(json.dumps({"generated": pd.Timestamp.now(tz="UTC").isoformat(), "meta": meta, "bots": bots},
                                 indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nเขียนงานให้บอท {len(bots)} ตัว → {ASSIGN}")


if __name__ == "__main__":
    main()
