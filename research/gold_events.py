"""
gold_events.py — ทดสอบสมมติฐาน v5 แบบ event study บนทอง M15 (กติกาตายตัวใน research/hypotheses_v5_gold.md)

    python research/gold_events.py <โฟลเดอร์ที่มี XAUUSD_M15.csv และ XAGUSD_M15.csv>
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SPLIT = pd.Timestamp("2025-11-01", tz="UTC")
COST, SWAP_YEAR, REF, OZ = 0.42, 0.06, 4000.0, 4.0    # ต้นทุนต่อออนซ์ · swap long · ราคาอ้างอิง · ออนซ์ที่ lot 0.04


def load(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["time"] = pd.to_datetime(d["time"], utc=True)
    return d.set_index("time").sort_index()


def daily(d: pd.DataFrame) -> pd.DataFrame:
    x = d.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    x = x[x.index.dayofweek < 5]
    x["adr"] = (x["high"] - x["low"]).rolling(20).mean().shift(1)
    return x


def price_at(d: pd.DataFrame, t: pd.Timestamp) -> float | None:
    i = d.index.searchsorted(t)
    return float(d["open"].iloc[i]) if i < len(d) and d.index[i] - t < pd.Timedelta(minutes=30) else None


def pnl(side: int, a: float, b: float, nights: float = 0.0) -> float:
    gross = (b - a) * side / a * REF
    swap = nights * SWAP_YEAR / 365 * REF if side > 0 else 0.0
    return (gross - COST - swap) * OZ


def at(day: pd.Timestamp, hhmm: str, tz: str = "America/New_York") -> pd.Timestamp:
    return pd.Timestamp(f"{day.date()} {hhmm}", tz=tz).tz_convert("UTC")


def g1(d, x):
    for day, r in x.iterrows():
        a, b, c = (price_at(d, at(day, h, "Europe/London")) for h in ("14:00", "15:00", "17:00"))
        if a and b and c and r.adr == r.adr and abs(b - a) > 0.25 * r.adr:
            yield day, pnl(-int(np.sign(b - a)), b, c)


def g2(d, x):
    fri = x[x.index.dayofweek == 4]
    for _, g in fri.groupby([fri.index.year, fri.index.month]):
        day = g.index[0]
        a, b, c = (price_at(d, at(day, h)) for h in ("08:30", "08:45", "12:00"))
        if a and b and c and a != b:
            yield day, pnl(int(np.sign(b - a)), b, c)


def g3(d, x):
    with open(ROOT / "config" / "news.csv", encoding="utf-8") as f:
        days = [r[0][:10] for r in csv.reader(f) if len(r) > 1 and r[0][:2] == "20" and r[1] == "FOMC"]
    for s in days:
        day = pd.Timestamp(s, tz="UTC")
        a, b, c = (price_at(d, at(day, h)) for h in ("14:00", "14:30", "16:00"))
        if a and b and c and a != b:
            yield day, pnl(int(np.sign(b - a)), b, c)


def g4(d, x):
    days = x.index
    for i in range(1, len(days)):
        day = days[i]
        if day.dayofweek != 0 or x.adr.iloc[i] != x.adr.iloc[i]:
            continue
        w = d[d.index >= day - pd.Timedelta(hours=4)]
        if w.empty or w.index[0] > day + pd.Timedelta(hours=12):
            continue
        o, fc = float(w.open.iloc[0]), float(x.close.iloc[i - 1])
        if abs(o - fc) <= 0.1 * x.adr.iloc[i]:
            continue
        side = -int(np.sign(o - fc))
        seg = w[w.index < w.index[0] + pd.Timedelta(hours=8)]
        hit = seg[(seg.high >= fc) & (seg.low <= fc)]
        yield day, pnl(side, o, fc if len(hit) else float(seg.close.iloc[-1]))


def g5(d, x):
    for day, r in x.iterrows():
        seg = d[(d.index >= day) & (d.index < day + pd.Timedelta(hours=14))]
        e = price_at(d, day + pd.Timedelta(hours=14))
        c = price_at(d, day + pd.Timedelta(hours=20))
        if len(seg) and e and c and r.adr == r.adr and seg.high.max() - seg.low.min() > 1.3 * r.adr:
            yield day, pnl(-int(np.sign(e - float(seg.open.iloc[0]))), e, c)


def g6(d, x):
    rng = x.high - x.low
    nr4 = rng == rng.rolling(4).min()
    days = x.index
    for i in range(4, len(days)):
        if not nr4.iloc[i - 1]:
            continue
        day, hi, lo = days[i], x.high.iloc[i - 1], x.low.iloc[i - 1]
        seg = d[(d.index >= day + pd.Timedelta(hours=7)) & (d.index < day + pd.Timedelta(hours=20))]
        if seg.empty:
            continue
        up, dn = seg[seg.high > hi], seg[seg.low < lo]
        if up.empty and dn.empty:
            continue
        side = 1 if dn.empty or (len(up) and up.index[0] < dn.index[0]) else -1
        t0, entry = (up.index[0], hi) if side > 0 else (dn.index[0], lo)
        stop = (hi + lo) / 2
        rest = seg[seg.index > t0]
        hit = rest[rest.low <= stop] if side > 0 else rest[rest.high >= stop]
        yield day, pnl(side, entry, stop if len(hit) else float(seg.close.iloc[-1]))


def g7(d, x, s):
    h = pd.DataFrame({"g": d.close.resample("1h").last(), "s": s.close.resample("1h").last()}).dropna()
    ratio = np.log(h.g / h.s)
    z = (ratio - ratio.rolling(120).mean()) / ratio.rolling(120).std()
    nxt = pd.Timestamp("2000-01-01", tz="UTC")
    for t, v in z.dropna().items():
        if abs(v) > 2 and t >= nxt:
            a, b = float(h.g.loc[t]), price_at(d, t + pd.Timedelta(hours=24))
            if b:
                nxt = t + pd.Timedelta(hours=24)
                yield t, pnl(-int(np.sign(v)), a, b, 1)


def g8(d, x):
    ema = x.close.ewm(span=50, adjust=False).mean()
    c = x.close.values
    for i in range(52, len(x) - 3):
        down2, up2 = c[i] < c[i - 1] < c[i - 2], c[i] > c[i - 1] > c[i - 2]
        if c[i] > ema.iloc[i] and down2:
            yield x.index[i], pnl(1, c[i], c[i + 2], 2)
        elif c[i] < ema.iloc[i] and up2:
            yield x.index[i], pnl(-1, c[i], c[i + 2], 2)


def g9(d, x):
    for day in x.index:
        a, b, c = (price_at(d, at(day, h)) for h in ("09:30", "15:00", "16:30"))
        if a and b and c and a != b:
            yield day, pnl(int(np.sign(b - a)), b, c)


def stats(v: list[float]) -> str:
    n = len(v)
    if n < 3:
        return f"n={n}"
    a = np.array(v)
    return f"n={n:4} ${a.mean():+7.2f}/ไม้ รวม ${a.sum():+8.0f} ชนะ {100 * (a > 0).mean():3.0f}% t={a.mean() / (a.std(ddof=1) / np.sqrt(n)):+.2f}"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    src = Path(sys.argv[1])
    d, s = load(src / "XAUUSD_M15.csv"), load(src / "XAGUSD_M15.csv")
    x = daily(d)
    tests = {"G1 fix": g1(d, x), "G2 NFP": g2(d, x), "G3 FOMC": g3(d, x), "G4 gap": g4(d, x), "G5 exhaust": g5(d, x),
             "G6 NR4": g6(d, x), "G7 silver": g7(d, x, s), "G8 dip2": g8(d, x), "G9 late": g9(d, x)}
    for name, it in tests.items():
        rows = list(it)
        print(f"{name:11} IS  {stats([p for t, p in rows if t < SPLIT])}\n{'':11} OOS {stats([p for t, p in rows if t >= SPLIT])}")


if __name__ == "__main__":
    main()
