"""
new_events.py — ทดสอบสมมติฐาน v6 แบบ event study (กติกาตายตัวใน research/hypotheses_v6.md)

    python research/new_events.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "mt5"
SPLIT = pd.Timestamp("2025-11-01", tz="UTC")
SPLIT_M5 = pd.Timestamp("2026-03-01", tz="UTC")
USD = {"NAS100": 0.73, "BTCUSD": 0.17}
COST = {"NAS100": 1.8, "BTCUSD": 4.3}
SWAP = {"NAS100": (-0.06364, 0.0), "BTCUSD": (-0.30, -0.10)}     # อัตราต่อปีของมูลค่าไม้ (long, short)
NY = "America/New_York"


def load(symbol: str, tf: str) -> pd.DataFrame:
    d = pd.read_csv(DATA / f"{symbol}_{tf}.csv")
    d["time"] = pd.to_datetime(d["time"], utc=True)
    return d.set_index("time").sort_index()


def daily_adr(d: pd.DataFrame, tz: str = "UTC") -> pd.Series:
    x = d.tz_convert(tz).resample("1D").agg({"high": "max", "low": "min"}).dropna()
    return (x.high - x.low).rolling(20).mean().shift(1)


def at(day, hhmm: str, tz: str = NY) -> pd.Timestamp:
    return pd.Timestamp(f"{pd.Timestamp(day).date()} {hhmm}", tz=tz).tz_convert("UTC")


def price_at(d: pd.DataFrame, t: pd.Timestamp, tol_min: int = 30) -> float | None:
    i = d.index.searchsorted(t)
    return float(d["open"].iloc[i]) if i < len(d) and d.index[i] - t < pd.Timedelta(minutes=tol_min) else None


def trade(d: pd.DataFrame, sym: str, side: int, t0: pd.Timestamp, t1: pd.Timestamp, sl_dist: float | None,
          tp: float | None = None) -> float | None:
    """เข้า open ของแท่งแรกที่ ≥ t0 · ออกเมื่อชน SL (ก่อน) / TP หรือ open ของแท่งที่ ≥ t1 · คืน $ หักต้นทุน + swap"""
    e = price_at(d, t0)
    x = price_at(d, t1, 240)
    if e is None or x is None:
        return None
    seg = d[(d.index >= t0) & (d.index < t1)]
    exit_ = x
    if sl_dist:
        sl = e - side * sl_dist
        for _, b in seg.iterrows():
            if (side > 0 and b.low <= sl) or (side < 0 and b.high >= sl):
                exit_ = sl
                break
            if tp is not None and ((side > 0 and b.high >= tp) or (side < 0 and b.low <= tp)):
                exit_ = tp
                break
    nights = max(0, (t1.tz_convert(NY).normalize() - t0.tz_convert(NY).normalize()).days)
    rate = SWAP[sym][0] if side > 0 else SWAP[sym][1]
    swap = rate / 365 * e * nights * USD[sym]
    return (exit_ - e) * side * USD[sym] - COST[sym] + swap


def n1(nas):
    adr = daily_adr(nas, NY)
    days = pd.Series(nas.tz_convert(NY).index.normalize().unique())
    main, ctrl = [], []
    for i, day in enumerate(days[:-1]):
        if day.dayofweek > 3:
            continue
        a = adr.get(day)
        if a is None or a != a:
            continue
        nxt = days.iloc[i + 1]
        p = trade(nas, "NAS100", 1, at(day, "15:45"), at(nxt, "09:15"), a)
        c = trade(nas, "NAS100", 1, at(day, "09:45"), at(day, "15:45"), a)
        if p is not None:
            main.append((at(day, "15:45"), p))
        if c is not None:
            ctrl.append((at(day, "09:45"), c))
    return main, ctrl


def fomc_days() -> list[pd.Timestamp]:
    with open(ROOT / "config" / "news.csv", encoding="utf-8") as f:
        return [pd.Timestamp(r[0][:10]) for r in csv.reader(f) if len(r) > 1 and r[0][:2] == "20" and r[1] == "FOMC"]


def n2(nas):
    adr = daily_adr(nas, NY)
    main, ctrl = [], []
    for day in fomc_days():
        prev = day - pd.offsets.BDay(1)
        a = adr.get(prev.tz_localize(NY)) if prev.tz_localize(NY) in adr.index else None
        if a is None or a != a:
            continue
        p = trade(nas, "NAS100", 1, at(prev, "14:00"), at(day, "13:45"), a)
        c = trade(nas, "NAS100", 1, at(prev - pd.Timedelta(days=7), "14:00"), at(day - pd.Timedelta(days=7), "13:45"), a)
        if p is not None:
            main.append((at(prev, "14:00"), p))
        if c is not None:
            ctrl.append((at(prev, "14:00"), c))
    return main, ctrl


def n3(m5):
    adr = daily_adr(m5, NY)
    days = list(m5.tz_convert(NY).index.normalize().unique())
    out = []
    for i in range(1, len(days)):
        day, prev = days[i], days[i - 1]
        if day.dayofweek > 4:
            continue
        a = adr.get(day)
        pc = m5[(m5.index >= at(prev, "15:55")) & (m5.index < at(prev, "16:00"))]
        first = m5[(m5.index >= at(day, "09:30")) & (m5.index < at(day, "09:35"))]
        if a is None or a != a or pc.empty or first.empty:
            continue
        prev_close, o, c = float(pc.close.iloc[-1]), float(first.open.iloc[0]), float(first.close.iloc[0])
        gap = o - prev_close
        if abs(gap) < 0.3 * a or (c - o) * np.sign(gap) >= 0:
            continue                                     # แท่งแรกต้องปิดย้อนเข้าหาราคาปิดเมื่อวาน
        side = -int(np.sign(gap))
        e = price_at(m5, at(day, "09:35"), 10)
        if e is None:
            continue
        stop = (float(first.high.iloc[0]) + 0.05 * a) if side < 0 else (float(first.low.iloc[0]) - 0.05 * a)
        p = trade(m5, "NAS100", side, at(day, "09:35"), at(day, "11:30"), abs(e - stop), prev_close)
        if p is not None:
            out.append((at(day, "09:35"), p))
    return out, []


def n4(btc):
    out = []
    for fri in btc.index.normalize().unique():
        if fri.dayofweek != 4:
            continue
        ref = price_at(btc, fri + pd.Timedelta(hours=21))
        t0 = fri + pd.Timedelta(days=2, hours=23)
        now = price_at(btc, t0)
        if not ref or not now or abs(now / ref - 1) < 0.02:
            continue
        side = 1 if now < ref else -1
        p = trade(btc, "BTCUSD", side, t0, fri + pd.Timedelta(days=3, hours=15), 0.015 * now, ref)
        if p is not None:
            out.append((t0, p))
    return out, []


def n5(nas, btc):
    adr_n, adr_b = daily_adr(nas, NY), daily_adr(btc, NY)
    main, ctrl = [], []
    for day in pd.Series(nas.tz_convert(NY).index.normalize().unique()):
        if day.dayofweek > 4:
            continue
        an, ab = adr_n.get(day), adr_b.get(day)
        a0, a1 = price_at(nas, at(day, "09:30")), price_at(nas, at(day, "10:00"))
        b0, b1 = price_at(btc, at(day, "09:30")), price_at(btc, at(day, "10:00"))
        if None in (an, ab, a0, a1, b0, b1) or an != an or ab != ab or abs(a1 - a0) < 0.25 * an:
            continue
        p = trade(btc, "BTCUSD", int(np.sign(a1 - a0)), at(day, "10:00"), at(day, "12:00"), 0.5 * ab)
        if p is not None:
            main.append((at(day, "10:00"), p))
        if b1 != b0:
            c = trade(btc, "BTCUSD", int(np.sign(b1 - b0)), at(day, "10:00"), at(day, "12:00"), 0.5 * ab)
            if c is not None:
                ctrl.append((at(day, "10:00"), c))
    return main, ctrl


def stats(v: list[float]) -> str:
    n = len(v)
    if n < 3:
        return f"n={n}"
    a = np.array(v)
    return f"n={n:4} ${a.mean():+7.2f}/ไม้ รวม ${a.sum():+8.0f} ชนะ {100 * (a > 0).mean():3.0f}% t={a.mean() / (a.std(ddof=1) / np.sqrt(n)):+.2f}"


def show(name: str, rows: list, split: pd.Timestamp) -> None:
    print(f"{name:14} IS  {stats([p for t, p in rows if t < split])}\n{'':14} OOS {stats([p for t, p in rows if t >= split])}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    nas, btc, m5 = load("NAS100", "M15"), load("BTCUSD", "M15"), load("NAS100", "M5")
    for name, (main_, ctrl), split in (("N1 overnight", n1(nas), SPLIT), ("N2 pre-FOMC", n2(nas), SPLIT),
                                       ("N3 gap fill", n3(m5), SPLIT_M5), ("N4 CME gap", n4(btc), SPLIT),
                                       ("N5 BTC←NAS", n5(nas, btc), SPLIT)):
        show(name, main_, split)
        if ctrl:
            show("  control", ctrl, split)


if __name__ == "__main__":
    main()
