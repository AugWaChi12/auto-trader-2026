"""
leadlag.py — ตลาดไหน "ขยับก่อน" ทองในระดับนาทีไหม (NAS100, EURUSD → XAUUSD M1)

    python research/leadlag.py      (ต้องเปิด MT5 — ดึง M1 ~100k แท่งล่าสุดของแต่ละตลาด)

ผล 2026-10-15 (53,634 นาที 07–20 UTC): สัมพันธ์กันแรงในนาทีเดียวกัน (0.40–0.52) แต่ lag ≥ 1 นาที ≈ 0
เทรดตามตัวนำที่ขยับแรง: ได้ +0.0002–0.0012%/ไม้ vs ต้นทุน 0.0073% → ไม่มี edge
(lead-lag ระดับมิลลิวินาทีมีจริง แต่เข้าถึงไม่ได้จากไทย และการทำกับราคาโบรก CFD ผิดข้อตกลงโบรก)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.core.timeutil import server_to_utc  # noqa: E402
from bot.data import mt5_source as S  # noqa: E402


def m1(mt5, name: str) -> pd.Series:
    mt5.symbol_select(name, True)
    r = pd.DataFrame(mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_M1, 1, S.MAX_POS_BARS))
    r["time"] = pd.to_datetime([server_to_utc(t.to_pydatetime(), C.SERVER_TIME_MODE)
                                for t in pd.to_datetime(r["time"], unit="s")], utc=True)
    return r.set_index("time")["close"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    import MetaTrader5 as mt5
    S.connect()
    series = {"XAU": m1(mt5, S.find_symbol("XAUUSD", mt5)), "NAS": m1(mt5, S.find_symbol("NAS100", mt5))}
    if mt5.symbol_info("EURUSD"):
        series["EURUSD"] = m1(mt5, "EURUSD")
    df = pd.DataFrame(series).dropna()
    ret = np.log(df).diff().dropna()
    ret = ret[(ret.index.hour >= 7) & (ret.index.hour < 20)]
    print(f"M1 ร่วม {len(ret):,} นาที  {ret.index[0]:%Y-%m-%d} → {ret.index[-1]:%Y-%m-%d} · noise ±{2 / np.sqrt(len(ret)):.4f}")
    for lead in [c for c in ret.columns if c != "XAU"]:
        print(lead, {f"k={k}": round(ret[lead].corr(ret["XAU"].shift(-k)), 4) for k in (0, 1, 2, 3, 5)})
    cost = 0.31 / df["XAU"].mean() * 100
    for lead in [c for c in ret.columns if c != "XAU"]:
        sig = np.sign(ret[lead]).where(ret[lead].abs() >= ret[lead].abs().quantile(0.95), 0)
        for hold in (1, 3):
            g = (sig * sum(ret["XAU"].shift(-i) for i in range(1, hold + 1)))[sig != 0].dropna() * 100
            print(f"{lead} top5% → ทองถือ {hold} นาที: {g.mean():+.4f}% vs ต้นทุน {cost:.4f}%")


if __name__ == "__main__":
    main()
