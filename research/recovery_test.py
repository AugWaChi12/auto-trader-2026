"""
recovery_test.py — ไม้เสีย $0.9 แล้ว "สวนกลับ" เพื่อเอาทุนคืน ช่วยได้จริงไหม (คำถามผู้ใช้ 2026-10-16)

    python research/recovery_test.py

เอาไม้จริงของบอท M1 ทอง (M1_PLAN ปัจจุบัน) → ทุกไม้ที่โดน SL เปิดไม้สวนทิศทันทีที่ราคาปิดไม้
    flip x1      : lot เท่าเดิม  TP = เอาที่เสียคืน (+ต้นทุน)  SL −$0.9
    flip x2      : lot 2 เท่า   TP = เอาที่เสียคืน          SL −$1.8 (ระยะราคาเท่าเดิม)
    martingale 4 : สวนซ้ำได้ 4 ชั้น lot 1→2→4→8 จนกว่าจะคืนทุน (สายหาคืนทุนคลาสสิก)
ถ้า SL และ TP โดนในแท่งเดียวกัน → นับ SL ก่อน (มองโลกแง่ร้าย)
"""
from __future__ import annotations

import sys
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
from bot.strategy.scalp import BollingerFade, EmaCross, RsiReversion  # noqa: E402

FIXED_MIN_LOT = RiskConfig(risk_per_trade=1e-9, min_lot_max_risk=1.0, daily_loss_limit=1.0, max_drawdown=1.0,
                           max_open_groups=50, max_groups_per_symbol=50)
LOT, CONTRACT, COMM = 0.01, 100.0, 7.0          # ค่าคอม $7 ต่อ lot ไป-กลับ
SL_USD = 0.9
MAX_HOLD = 60                                    # ไม้สวนถือได้ไม่เกิน 60 แท่ง แล้วปิดตามราคา
STRATS = {"bb_xau": BollingerFade, "lr1_x": RsiReversion, "ec1_x": EmaCross}


def recover(i0: int, side: int, debt: float, mult: float, levels: int, t, o, h, l, c, spread) -> tuple[float, int]:
    """เปิดไม้สวน (side) ที่แท่ง i0 พยายามเอา debt คืน · คืน (กำไรรวมของชุดไม้สวน, แท่งที่จบ)"""
    total, lot, i = 0.0, LOT * mult, i0
    for _ in range(levels):
        if i >= len(c):
            break
        per = lot * CONTRACT
        cost = COMM * lot
        entry = o[i] + (spread[i] if side > 0 else 0.0)
        tp = entry + side * (debt + cost) / per
        sl = entry - side * (SL_USD * lot / LOT) / per            # ระยะราคาเท่าไม้แรก
        out = None
        for j in range(i, min(i + MAX_HOLD, len(c))):
            bid_hi, bid_lo = h[j], l[j]
            hit_sl = (bid_lo <= sl) if side > 0 else (bid_hi + spread[j] >= sl)
            hit_tp = (bid_hi >= tp) if side > 0 else (bid_lo + spread[j] <= tp)
            if hit_sl:
                out = (sl, j, False)
                break
            if hit_tp:
                out = (tp, j, True)
                break
        if out is None:
            j = min(i + MAX_HOLD, len(c)) - 1
            px = c[j] + (spread[j] if side < 0 else 0.0)
            out = (px, j, None)
        px, j, won = out
        pnl = (px - entry) * side * per - cost
        total += pnl
        i = j + 1
        if won or won is None or pnl >= 0:
            break
        debt -= pnl                    # เสียเพิ่ม → หนี้โตขึ้น ไม้ถัดไป lot ×2 สวนอีกที
        side, lot = -side, lot * 2
    return total, i


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    data = ROOT / "data" / "mt5"
    bars = load_bars(bars_path(data, "XAUUSD", "M1"))
    spec = load_spec(data, "XAUUSD", C.DEFAULT_SPECS["XAUUSD"])
    news = NewsCalendar.default(C.NEWS_CSV, (bars[0].time.year, bars[-1].time.year), C.NEWS_BLACKOUT_MIN)
    t = np.array([b.time for b in bars])
    o, h, l, c = (np.array([getattr(b, k) for b in bars]) for k in ("open", "high", "low", "close"))
    spread = np.array([max(b.spread, spec.min_spread) for b in bars])
    idx = {tt: k for k, tt in enumerate(t)}
    days = max((bars[-1].time - bars[0].time).days * 5 / 7, 1)

    rows = []
    for tag, cls in STRATS.items():
        cfg = next(x for x in C.LAB_CONFIGS + C.M1_CONFIGS if x.tag == tag)
        res = run_backtest({"XAUUSD": bars}, {"XAUUSD": spec}, [cls(cfg, "M1")], FIXED_MIN_LOT, "M1", 1_000_000.0, news)
        base = [(tr, tr.pnl / tr.volume * LOT) for tr in res.trades]
        for label, mult, levels in (("ไม่สวน (ตอนนี้)", 0, 0), ("flip x1", 1, 1), ("flip x2", 2, 1), ("martingale 4 ชั้น", 1, 4)):
            pnl, worst_chain, busy_until = [], 0.0, None
            for tr, usd in base:
                k = idx.get(tr.close_time.replace(second=0, microsecond=0))
                if busy_until is not None and k is not None and k < busy_until:
                    continue                                   # กำลังสวนอยู่ → ไม้ใหม่ของกลยุทธ์ข้ามไป (ถือทีละชุด)
                pnl.append(usd)
                if levels and usd < 0 and tr.exit_reason == "sl" and k is not None:
                    rec, end = recover(k + 1, -int(tr.side), -usd, mult, levels, t, o, h, l, c, spread)
                    pnl.append(rec)
                    worst_chain = min(worst_chain, usd + rec)
                    busy_until = end
            arr = np.array(pnl)
            eq = np.cumsum(arr)
            rows.append({"บอท": tag, "แบบ": label, "ต่อวัน $": round(arr.sum() / days, 2),
                         "ชุดที่แย่สุด $": round(worst_chain, 2),
                         "DD สูงสุด $": round(float((np.maximum.accumulate(eq) - eq).max()), 1)})
    df = pd.DataFrame(rows)
    for tag, g in df.groupby("บอท", sort=False):
        print(f"\n### {tag}")
        print(g.drop(columns="บอท").to_string(index=False))


if __name__ == "__main__":
    main()
