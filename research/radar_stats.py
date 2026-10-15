"""
radar_stats.py — สถิติ "ถ้าเงื่อนไขนี้เข้า อดีตเป็นอย่างไร" สำหรับหน้า Signal Radar

    python research/radar_stats.py        (หรือ python -m bot radar-stats)

backtest ทุกกลยุทธ์ที่บอท live ใช้อยู่ (ทุก instance) ด้วยข้อมูลล่าสุด แล้วนับเฉพาะไม้ใน 12 เดือนหลัง
(M1 มีข้อมูลแค่ ~3 เดือน → ใช้ทั้งหมด) · ผลลัพธ์: research/out/radar_stats.json
ไม่ใช่การหา edge ใหม่ — เป็นการ "บอกความน่าจะเป็นตามจริง" ให้คนดูหน้าจอ
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.backtest.report import group_trades  # noqa: E402
from bot.backtest.runner import run_backtest  # noqa: E402
from bot.core.timeutil import UTC  # noqa: E402
from bot.data.store import bars_path, load_bars, load_spec  # noqa: E402
from bot.risk.news import NewsCalendar  # noqa: E402
from bot.risk.risk_manager import RiskConfig  # noqa: E402

OUT = ROOT / "research" / "out" / "radar_stats.json"
BINS = [-np.inf, -1.25, -0.75, -0.25, 0.25, 0.75, 1.25, 1.75, 2.5, np.inf]
BIN_LABELS = ["≤−1.25", "−1", "−0.5", "0", "+0.5", "+1", "+1.5", "+2", "≥+2.5"]
RISK = RiskConfig(risk_per_trade=0.01, daily_loss_limit=1.0, max_drawdown=1.0, max_open_groups=50,
                  max_groups_per_symbol=50)

VERDICT = {   # ผลจากงานวิจัย (research/*.md) — แสดงบนหน้าจอคู่กับสถิติ
    "xau_momo": "ไม่ผ่าน OOS (+0.037R) · กำลัง forward test",
    "lr_xau": "ตก (S1 −0.016R)", "lr_nas": "ตก (S2 −0.021R)", "ec_xau": "ตก (S3 −0.033R)", "bb_xau": "ตก (S4 −0.205R)",
    "g_bo_x": "ตก (รอบ 1)", "g_sw_x": "ตก (รอบ 1 −0.21R)", "g_bo_n": "ตก (รอบ 1)", "g_sw_n": "ตก (รอบ 1)",
    "g_vx_x": "ตก (H5: กำไรมาจาก drift)", "g_vx_n": "ตก (H6)", "g_ls_x": "ตก (H7 −0.24R)", "g_ls_n": "ตก (H8)",
    "g_pb_x": "ตก (pullback 2/48)", "g_pb_n": "ตก (pullback 2/48)",
    "b_rnd_x": "กลุ่มควบคุม (สุ่มทิศ)", "b_rnd_n": "กลุ่มควบคุม (สุ่มทิศ)",
    "b_lr5_x": "กลุ่มควบคุม (จังหวะ lr_xau สุ่มทิศ)", "b_lr5_n": "กลุ่มควบคุม (จังหวะ lr_nas สุ่มทิศ)",
    "b_ec5_x": "กลุ่มควบคุม (จังหวะ ec_xau สุ่มทิศ)",
    "b_lng_x": "กลุ่มควบคุม (ซื้ออย่างเดียว)", "b_lng_n": "กลุ่มควบคุม (ซื้ออย่างเดียว)",
}


def _job(args: tuple[str, str, str]) -> dict:
    instance, tag, tf = args
    strat = C.build_strategies([tag], timeframe=tf)[0]
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, strat.symbol, tf))
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    res = run_backtest({strat.symbol: bars}, {strat.symbol: load_spec(data, strat.symbol, C.DEFAULT_SPECS[strat.symbol])},
                       [strat], RISK, tf, 1_000_000.0, news)
    since = bars[-1].time - timedelta(days=365)
    groups = [g for g in group_trades(res.trades) if datetime.fromisoformat(g.open_time) >= since]
    r = np.array([g.r for g in groups])
    spec = C.LIVE_INSTANCES[instance]
    prof = C.RISK_PROFILES[spec["profile"]]
    out = {"tag": tag, "instance": instance, "tf": tf, "symbol": strat.symbol, "verdict": VERDICT.get(tag, ""),
           "allocation": spec.get("allocation"), "risk_pct": prof.risk_per_trade, "n": int(len(r))}
    if len(r) == 0:
        return out
    first = max(since, bars[0].time)
    weeks = max((bars[-1].time - first).days / 7, 1)
    w, l = r[r > 0], r[r <= 0]
    hist, _ = np.histogram(np.clip(r, -5, 10), bins=BINS)
    longs = np.array([g.r for g in groups if g.side == "BUY"])
    shorts = np.array([g.r for g in groups if g.side == "SELL"])
    out.update({
        "period": [first.date().isoformat(), bars[-1].time.date().isoformat()],
        "per_week": round(len(r) / weeks, 1), "win_rate": round(len(w) / len(r), 3),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0, "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "exp_r": round(float(r.mean()), 3), "pf": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None,
        "ci95": [round(float(r.mean() - 1.96 * r.std(ddof=1) / np.sqrt(len(r))), 3),
                 round(float(r.mean() + 1.96 * r.std(ddof=1) / np.sqrt(len(r))), 3)] if len(r) > 2 else None,
        "hist": [int(x) for x in hist], "hist_labels": BIN_LABELS,
        "long": {"n": int(len(longs)), "exp_r": round(float(longs.mean()), 3) if len(longs) else None},
        "short": {"n": int(len(shorts)), "exp_r": round(float(shorts.mean()), 3) if len(shorts) else None},
    })
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    jobs = [(name, tag, spec["tf"]) for name, spec in C.LIVE_INSTANCES.items() for tag in spec["strategies"]]
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(_job, jobs))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "strategies": {r["tag"]: r for r in rows}},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        if r["n"]:
            print(f"{r['tag']:9s} {r['tf']:4s} n={r['n']:4d} {r['per_week']:5.1f}/สัปดาห์  ชนะ {r['win_rate']:.0%}  "
                  f"exp {r['exp_r']:+.3f}R  CI95 {r['ci95']}")
        else:
            print(f"{r['tag']:9s} {r['tf']:4s} ไม่มีเทรดใน 12 เดือน")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
