"""
turn_of_month.py — T3: Turn-of-the-month ในดัชนีหุ้นสหรัฐ (research/hypotheses_v4_daily.md)

    python research/turn_of_month.py           ช่วงวิจัย (ถึง 2022)
    python research/turn_of_month.py holdout   hold-out 2023 → (เปิดเมื่อช่วงวิจัยผ่านเท่านั้น)

กฎ (ไม่มีพารามิเตอร์): ซื้อตอนปิดวันทำการที่ 2 นับจากท้ายเดือน → ขายตอนปิดวันทำการที่ 3 ของเดือนถัดไป
ต้นทุน: สเปรด + slippage ไป-กลับ + swap ฝั่ง BUY ตามจำนวนคืนที่ถือ (อัตราปัจจุบันของโบรก — แบบระวัง)
ผลต่อเดือน = ค่าเฉลี่ยของดัชนีที่มีข้อมูล (NAS100 ≥ 2016, US30, SPX500) → 1 ตัวอย่างต่อเดือน (ไม่นับซ้ำ)
benchmark = ผลตอบแทน long 5 วันทำการในช่วงอื่น (ไม่ทับซ้อน ไม่แตะ TOM) หลังหักต้นทุนเดียวกัน
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config as C  # noqa: E402
from bot.data.store import load_spec  # noqa: E402

DATA = ROOT / "data" / "mt5"
INDICES = {"NAS100": "2016-01-01", "US30": "2008-01-01", "SPX500": "2011-07-01"}
RESEARCH_END = pd.Timestamp("2023-01-01", tz="UTC")
T_CRIT = 2.4
SWAP_CAP = 0.065                     # แบบ A (ใช้ตัดสิน) — ดู research/hypotheses_v4_daily.md
RAW_SWAP = "rawswap" in sys.argv     # แบบ B


def load(sym: str, start: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{sym}_D1.csv", parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    # แท่ง D1 ของโบรกเริ่ม 17:00 นิวยอร์ก (= วันทำการถัดไป) → ใช้วันที่เวลา server เป็นวันทำการ
    df["day"] = (df["time"] + pd.Timedelta(hours=7)).dt.date
    return df[df["time"] >= pd.Timestamp(start, tz="UTC")].reset_index(drop=True)


def trade_returns(sym: str, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """คืน (ผล TOM ต่อเดือน, ผล 5 วันช่วงอื่น) หลังหักต้นทุน หน่วย %"""
    spec = load_spec(DATA, sym, C.DEFAULT_SPECS[sym])
    c = df["close"].to_numpy()
    days = pd.to_datetime(df["day"])
    ym = days.dt.to_period("M").to_numpy()

    def net(i0: int, i1: int) -> float:
        gross = c[i1] / c[i0] - 1
        cost = (spec.min_spread + 2 * spec.slippage) / c[i0]
        nights = (days.iloc[i1] - days.iloc[i0]).days
        rate = spec.swap_long_rate if RAW_SWAP else max(spec.swap_long_rate, -SWAP_CAP)   # ≤ 0
        swap = rate / 365 * nights
        return (gross - cost + swap) * 100

    tom, used = {}, set()
    starts = [i for i in range(1, len(df)) if ym[i] != ym[i - 1]]       # วันทำการแรกของเดือน
    for s in starts:
        i0, i1 = s - 2, s + 2                                            # ปิดวันที่ 2 จากท้าย → ปิดวันที่ 3 ของเดือนใหม่
        if i0 < 0 or i1 >= len(df):
            continue
        tom[str(ym[s])] = net(i0, i1)
        used.update(range(i0, i1 + 1))
    other, i = [], 0
    while i + 5 < len(df):
        if not any(j in used for j in range(i, i + 6)):
            other.append((str(ym[i]), net(i, i + 5)))
            i += 5
        else:
            i += 1
    return pd.Series(tom), pd.Series(dict(other)) if False else pd.Series([v for _, v in other],
                                                                            index=[k for k, _ in other])


def evaluate(holdout: bool) -> None:
    per_month, bench = {}, []
    for sym, start in INDICES.items():
        df = load(sym, start)
        tom, other = trade_returns(sym, df)
        per_month[sym] = tom
        bench.append(other)
    tom = pd.DataFrame(per_month)
    tom.index = pd.PeriodIndex(tom.index, freq="M")
    bench_s = pd.concat(bench)
    bench_s.index = pd.PeriodIndex(bench_s.index, freq="M")
    cut = pd.Period("2023-01", freq="M")
    if holdout:
        tom, bench_s = tom[tom.index >= cut], bench_s[bench_s.index >= cut]
        label = "HOLD-OUT 2023-01 →"
    else:
        tom, bench_s = tom[tom.index < cut], bench_s[bench_s.index < cut]
        label = "ช่วงวิจัย → 2022-12"
    basket = tom.mean(axis=1)                       # ค่าเฉลี่ยของดัชนีที่มีข้อมูลในเดือนนั้น
    excess = basket - bench_s.mean()
    t = excess.mean() / (excess.std(ddof=1) / np.sqrt(len(excess)))
    print(f"\n### T3 Turn-of-the-month — {label}")
    print(f"เดือน: {len(basket)} · TOM เฉลี่ย {basket.mean():+.3f}%/ครั้ง (ชนะ {np.mean(basket > 0):.0%})"
          f" · 5 วันช่วงอื่นเฉลี่ย {bench_s.mean():+.3f}% (n={len(bench_s)})")
    print(f"ผลส่วนเกิน {excess.mean():+.3f}%/ครั้ง · t = {t:.2f}")
    for sym in tom.columns:
        x = tom[sym].dropna()
        print(f"  {sym:7s} n={len(x):3d} เฉลี่ย {x.mean():+.3f}% ชนะ {np.mean(x > 0):.0%}")
    yearly = basket.groupby(basket.index.year).sum()
    print("รวมต่อปี (%):", {int(y): round(v, 2) for y, v in yearly.items()})
    if not holdout:
        ok = excess.mean() > 0 and t >= T_CRIT and basket.mean() > 0
        print(f"  [{'PASS' if t >= T_CRIT and excess.mean() > 0 else 'fail'}] ผลส่วนเกิน > 0 ด้วย t ≥ {T_CRIT}")
        print(f"  [{'PASS' if basket.mean() > 0 else 'fail'}] หลังหักต้นทุน+swap ยังบวก")
        print(f"==> T3 ช่วงวิจัย: {'ผ่าน → เปิด hold-out ได้' if ok else 'ตก (ไม่เปิด hold-out)'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    evaluate("holdout" in sys.argv)
