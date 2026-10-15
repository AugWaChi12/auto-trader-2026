"""
autopsy.py — ชันสูตรไม้แพ้: กลยุทธ์ที่ตก แพ้เพราะอะไร

    python research/autopsy.py            → research/AUTOPSY.md + research/out/autopsy.json

ต่อไม้ (backtest 12 เดือนล่าสุด, ต้นทุนจริง) คำนวณ:
  MFE  = กำไรสูงสุดที่ไม้เคยไปถึงก่อนปิด (หน่วย R)
  MAE  = ขาดทุนลึกสุดระหว่างถือ (หน่วย R)
  ต้นทุน/R = สเปรดของแท่งที่เข้า ÷ ระยะ SL

แปลผล (ต่อกลยุทธ์):
  "แพ้ทั้งที่เคยกำไร ≥ 0.5R"  สูง → ปัญหาที่จุดออก (คืนกำไร)
  "แพ้แบบไม่เคยไปทางเราเลย" สูง → ปัญหาที่จุดเข้า/ทิศ (สัญญาณไม่มีข้อมูล)
  "ต้นทุน/R" สูง               → SL แคบเกินเมื่อเทียบต้นทุน (ไม้สั้นเกิน)
"""
from __future__ import annotations

import bisect
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.core.types import Side, timeframe_delta  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.risk.risk_manager import RiskConfig  # noqa: E402

RISK = RiskConfig(risk_per_trade=0.01, daily_loss_limit=1.0, max_drawdown=1.0, max_open_groups=50,
                  max_groups_per_symbol=50)
OUT_MD = ROOT / "research" / "AUTOPSY.md"
OUT_JSON = ROOT / "research" / "out" / "autopsy.json"


def analyse(args: tuple[str, str, str]) -> dict:
    instance, tag, tf = args
    strat = C.build_strategies([tag], timeframe=tf)[0]
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, strat.symbol, tf))
    spec = load_spec(data, strat.symbol, C.DEFAULT_SPECS[strat.symbol])
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({strat.symbol: bars}, {strat.symbol: spec}, [strat], RISK, tf, 1_000_000.0, news)
    since = bars[-1].time - timedelta(days=365)
    times = [b.time for b in bars]
    dur = timeframe_delta(tf)

    groups: dict[str, list] = defaultdict(list)
    for t in res.trades:
        if t.open_time >= since:
            groups[t.group_id].append(t)

    rows = []
    for ts in groups.values():
        t0 = min(ts, key=lambda t: t.open_time)
        vol = sum(t.volume for t in ts)
        pnl = sum(t.pnl for t in ts)
        risk = sum(t.risk_amount for t in ts)
        r_unit = (t0.risk_amount - spec.commission_per_lot * t0.volume) / (t0.volume * spec.contract_size) - spec.slippage
        if r_unit <= 0 or risk <= 0:
            continue
        open_bar = t0.open_time - dur                       # แท่งที่ส่งสัญญาณ (เข้าตอนแท่งนี้ปิด)
        i0 = bisect.bisect_left(times, t0.open_time)
        i1 = bisect.bisect_right(times, max(t.close_time for t in ts) - dur)
        seg = bars[i0:i1 + 1] or bars[i0:i0 + 1]
        hi, lo = max(b.high for b in seg), min(b.low for b in seg)
        long = t0.side is Side.BUY
        mfe = ((hi - t0.entry_price) if long else (t0.entry_price - lo)) / r_unit
        mae = ((t0.entry_price - lo) if long else (hi - t0.entry_price)) / r_unit
        sb = bars[max(bisect.bisect_left(times, open_bar), 0)]
        spread = max(sb.spread, spec.min_spread)
        rows.append({
            "r": pnl / risk, "mfe": mfe, "mae": mae, "cost_r": (spread + 2 * spec.slippage) / r_unit
            + spec.commission_per_lot * vol / risk, "long": long,
            "hour_th": (t0.open_time.hour + 7) % 24, "bars": max(len(seg), 1),
            "exit": "+".join(sorted({t.exit_reason for t in ts})),
        })
    if not rows:
        return {"tag": tag, "instance": instance, "tf": tf, "n": 0}

    r = np.array([x["r"] for x in rows])
    losers = [x for x in rows if x["r"] <= 0]
    winners = [x for x in rows if x["r"] > 0]
    by_hour = defaultdict(list)
    for x in rows:
        by_hour[x["hour_th"]].append(x["r"])
    hours = sorted(((h, float(np.sum(v)), len(v)) for h, v in by_hour.items() if len(v) >= 5), key=lambda z: z[1])
    by_exit = defaultdict(list)
    for x in rows:
        by_exit[x["exit"]].append(x["r"])
    longs = [x["r"] for x in rows if x["long"]]
    shorts = [x["r"] for x in rows if not x["long"]]
    gave_back = sum(1 for x in losers if x["mfe"] >= 0.5) / max(len(losers), 1)
    never = sum(1 for x in losers if x["mfe"] < 0.1) / max(len(losers), 1)
    cost = float(np.mean([x["cost_r"] for x in rows]))
    out = {
        "tag": tag, "instance": instance, "tf": tf, "symbol": strat.symbol, "n": len(rows),
        "exp_r": round(float(r.mean()), 3), "win_rate": round(len(winners) / len(rows), 3),
        "losers_gave_back_0_5R": round(gave_back, 3), "losers_never_in_profit": round(never, 3),
        "avg_mfe_losers": round(float(np.mean([x["mfe"] for x in losers])), 2) if losers else None,
        "avg_mae_winners": round(float(np.mean([x["mae"] for x in winners])), 2) if winners else None,
        "cost_per_trade_r": round(cost, 3),
        "exp_r_before_cost": round(float(r.mean()) + cost, 3),
        "long": {"n": len(longs), "exp_r": round(float(np.mean(longs)), 3) if longs else None},
        "short": {"n": len(shorts), "exp_r": round(float(np.mean(shorts)), 3) if shorts else None},
        "worst_hours_th": [{"hour": h, "total_r": round(s, 1), "n": n} for h, s, n in hours[:3]],
        "best_hours_th": [{"hour": h, "total_r": round(s, 1), "n": n} for h, s, n in hours[-3:][::-1]],
        "exits": {k: {"n": len(v), "avg_r": round(float(np.mean(v)), 2)} for k, v in
                  sorted(by_exit.items(), key=lambda kv: -len(kv[1]))[:5]},
    }
    out["diagnosis"] = diagnose(out)
    return out


def diagnose(a: dict) -> list[str]:
    d = []
    if a["exp_r_before_cost"] > 0.02 and a["exp_r"] <= 0:
        d.append(f"💸 ต้นทุนคือตัวการ: ก่อนหักต้นทุน {a['exp_r_before_cost']:+.3f}R → หลังหัก {a['exp_r']:+.3f}R")
    elif a["exp_r_before_cost"] <= 0.02:
        d.append(f"🎯 สัญญาณไม่มีข้อมูล: แม้ไม่มีต้นทุนก็ได้แค่ {a['exp_r_before_cost']:+.3f}R ต่อไม้")
    if a["cost_per_trade_r"] >= 0.1:
        d.append(f"📏 SL แคบเกินเทียบต้นทุน: ต้นทุนกิน {a['cost_per_trade_r']:.0%} ของ SL ทุกไม้")
    if a["losers_gave_back_0_5R"] >= 0.35:
        d.append(f"↩️ คืนกำไรบ่อย: {a['losers_gave_back_0_5R']:.0%} ของไม้แพ้เคยกำไร ≥ 0.5R มาก่อน → ลองศึกษาจุดออก")
    if a["losers_never_in_profit"] >= 0.4:
        d.append(f"⛔ เข้าผิดจังหวะ: {a['losers_never_in_profit']:.0%} ของไม้แพ้ไม่เคยไปทางเราเลย")
    lo, sh = a["long"]["exp_r"], a["short"]["exp_r"]
    if lo is not None and sh is not None and abs(lo - sh) >= 0.15:
        better = "BUY" if lo > sh else "SELL"
        d.append(f"↕️ สองฝั่งต่างกันมาก: BUY {lo:+.3f}R vs SELL {sh:+.3f}R — ฝั่ง {better} ดีกว่า (ระวัง: อาจเป็นแค่ทิศตลาดช่วงนี้)")
    if a["worst_hours_th"] and a["worst_hours_th"][0]["total_r"] < -5:
        w = a["worst_hours_th"][0]
        d.append(f"🕐 แพ้หนักช่วง {w['hour']:02d}:00 น. (รวม {w['total_r']:+.1f}R จาก {w['n']} ไม้)")
    return d or ["— ไม่มีจุดเด่นชัดเจน (แพ้แบบกระจาย)"]


def write_md(rows: list[dict]) -> None:
    L = ["# ชันสูตรไม้แพ้ (Loss Autopsy)", "",
         "สร้างโดย `python research/autopsy.py` · backtest 12 เดือนล่าสุด (M1 ใช้ทั้งหมดที่มี) ต้นทุนจริง", "",
         "| กลยุทธ์ | ไม้ | ชนะ | ค่าคาดหวัง | ก่อนหักต้นทุน | ต้นทุน/SL | แพ้หลังเคยกำไร ≥0.5R | แพ้แบบไม่เคยกำไร |",
         "|---|---|---|---|---|---|---|---|"]
    for a in rows:
        if not a["n"]:
            continue
        L.append(f"| `{a['tag']}` {a['tf']} | {a['n']} | {a['win_rate']:.0%} | {a['exp_r']:+.3f}R | "
                 f"{a['exp_r_before_cost']:+.3f}R | {a['cost_per_trade_r']:.0%} | {a['losers_gave_back_0_5R']:.0%} | "
                 f"{a['losers_never_in_profit']:.0%} |")
    for a in rows:
        if not a["n"]:
            continue
        L += ["", f"## `{a['tag']}` ({a['instance']}, {a['symbol']} {a['tf']})", ""]
        L += [f"- {x}" for x in a["diagnosis"]]
        L.append(f"- ฝั่ง: BUY {a['long']['n']} ไม้ {a['long']['exp_r']}R · SELL {a['short']['n']} ไม้ {a['short']['exp_r']}R")
        L.append("- ชั่วโมงแย่สุด (เวลาไทย): " + ", ".join(f"{h['hour']:02d}:00 ({h['total_r']:+.1f}R/{h['n']})" for h in a["worst_hours_th"]))
        L.append("- ชั่วโมงดีสุด: " + ", ".join(f"{h['hour']:02d}:00 ({h['total_r']:+.1f}R/{h['n']})" for h in a["best_hours_th"]))
        L.append("- จุดออก: " + ", ".join(f"{k} {v['n']} ไม้ เฉลี่ย {v['avg_r']:+.2f}R" for k, v in a["exits"].items()))
    L += ["", "> ⚠️ \"ชั่วโมงดี/ฝั่งดี\" ที่เจอในนี้เป็นการมองย้อนหลังบนข้อมูลชุดเดียว — ถ้าจะใช้ต้องลงทะเบียนเป็นสมมติฐานใหม่",
          "> แล้วทดสอบบนข้อมูลที่ไม่เคยดู (เช่น ผลจริงบน demo หลังจากนี้) ไม่งั้นคือ overfit"]
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    jobs = [(n, tag, s["tf"]) for n, s in C.LIVE_INSTANCES.items() for tag in s["strategies"]]
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(analyse, jobs))
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(rows)
    for a in rows:
        if a["n"]:
            print(f"\n{a['tag']:9s} {a['tf']:4s} n={a['n']:4d} exp {a['exp_r']:+.3f}R (ก่อนต้นทุน {a['exp_r_before_cost']:+.3f}R)")
            for x in a["diagnosis"]:
                print("   ", x)
    print(f"\n→ {OUT_MD}")


if __name__ == "__main__":
    main()
