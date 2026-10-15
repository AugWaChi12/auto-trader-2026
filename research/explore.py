"""
explore.py — ขั้นที่ 1: สำรวจพฤติกรรมราคาตามช่วงเวลา (IN-SAMPLE เท่านั้น)

    python research/explore.py

ทุกตัวเลขมีจำนวนตัวอย่าง (n) และ t-stat กำกับ
|t| < 2 = แยกไม่ออกจากความบังเอิญ อย่าเอาไปสร้างกลยุทธ์
ผลตอบแทนเป็น % (ไม่ใช่ดอลลาร์) เพราะราคาทองเปลี่ยนจาก ~1,900 → ~3,300 ในช่วงนี้
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
IS_START, IS_END = "2023-10-16", "2025-07-01"   # out-of-sample เริ่ม 2025-07-01 — ห้ามแตะในไฟล์นี้


def load(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "mt5" / f"{symbol}_M15.csv")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df[(df["time"] >= IS_START) & (df["time"] < IS_END)].set_index("time")
    return df


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0


def section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * (70 - len(title))}")


def hourly(df: pd.DataFrame) -> None:
    h = df.resample("1h").agg({"open": "first", "close": "last"}).dropna()
    r = (h["close"] / h["open"] - 1) * 100
    g = r.groupby(r.index.hour)
    out = pd.DataFrame({"n": g.size(), "mean_%": g.mean().round(4), "abs_%": g.apply(lambda x: x.abs().mean()).round(3),
                        "up_%": g.apply(lambda x: (x > 0).mean() * 100).round(1), "t": g.apply(tstat).round(2)})
    print(out.to_string())


def weekday(df: pd.DataFrame) -> None:
    d = daily(df)
    r = d["ret"]
    g = r.groupby(r.index.dayofweek)
    print(pd.DataFrame({"n": g.size(), "mean_%": g.mean().round(3), "up_%": g.apply(lambda x: (x > 0).mean() * 100).round(1),
                        "t": g.apply(tstat).round(2)}).rename(index=dict(enumerate("Mon Tue Wed Thu Fri Sat Sun".split()))).to_string())


def daily(df: pd.DataFrame) -> pd.DataFrame:
    d = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d = d[d.index.dayofweek < 5]
    d["ret"] = (d["close"] / d["open"] - 1) * 100
    return d


def session_returns(df: pd.DataFrame, sessions: dict[str, tuple[int, int]]) -> pd.DataFrame:
    out = {}
    for name, (a, b) in sessions.items():
        s = df[(df.index.hour >= a) & (df.index.hour < b)]
        g = s.groupby(s.index.date)
        out[name] = (g["close"].last() / g["open"].first() - 1) * 100
    res = pd.DataFrame(out).dropna()
    return res[pd.to_datetime(res.index).dayofweek < 5]


def momentum_table(sr: pd.DataFrame) -> None:
    """ทิศของ session ก่อนหน้า บอกทิศของ session ถัดไปได้ไหม"""
    cols = list(sr.columns)
    rows = []
    for i in range(len(cols) - 1):
        a, b = cols[i], cols[i + 1]
        same = np.sign(sr[a]) * sr[b]       # ผลตอบแทนถ้าเข้าตามทิศ session ก่อนหน้า
        rows.append({"if": a, "then": b, "n": len(same), "follow_mean_%": round(same.mean(), 4),
                     "same_dir_%": round((np.sign(sr[a]) == np.sign(sr[b])).mean() * 100, 1), "t": round(tstat(same), 2)})
    # ทิศของ (เอเชีย+ลอนดอน) → ส่วนที่เหลือของวัน
    if len(cols) >= 3:
        first = sr[cols[0]] + sr[cols[1]]
        rest = sr[cols[2:]].sum(axis=1)
        same = np.sign(first) * rest
        rows.append({"if": f"{cols[0]}+{cols[1]}", "then": "+".join(cols[2:]), "n": len(same),
                     "follow_mean_%": round(same.mean(), 4),
                     "same_dir_%": round((np.sign(first) == np.sign(rest)).mean() * 100, 1), "t": round(tstat(same), 2)})
    print(pd.DataFrame(rows).to_string(index=False))


def momentum_split(sr: pd.DataFrame, first: list[str], rest: list[str]) -> None:
    """แยกเช้าขึ้น/เช้าลง: ถ้าได้ผลแค่ฝั่งเดียว = แค่ drift ของตลาดขาขึ้น ไม่ใช่ momentum"""
    f = sr[first].sum(axis=1)
    r = sr[rest].sum(axis=1)
    rows = []
    for name, mask, sign in [("morning UP → long rest", f > 0, 1), ("morning DOWN → short rest", f < 0, -1)]:
        x = r[mask] * sign
        rows.append({"case": name, "n": len(x), "mean_%": round(x.mean(), 4), "win_%": round((x > 0).mean() * 100, 1),
                     "t": round(tstat(x), 2)})
    x = r
    rows.append({"case": "unconditional long rest (benchmark)", "n": len(x), "mean_%": round(x.mean(), 4),
                 "win_%": round((x > 0).mean() * 100, 1), "t": round(tstat(x), 2)})
    # ครึ่งแรก / ครึ่งหลังของ in-sample
    mid = len(sr) // 2
    for label, sl in (("1st half", slice(0, mid)), ("2nd half", slice(mid, None))):
        ff, rr = f.iloc[sl], r.iloc[sl]
        x = np.sign(ff) * rr
        rows.append({"case": f"follow ({label})", "n": len(x), "mean_%": round(x.mean(), 4),
                     "win_%": round((x > 0).mean() * 100, 1), "t": round(tstat(x), 2)})
    # เช้าแรงเท่าไรถึงมีผล (แบ่ง tercile ตามขนาด)
    q = f.abs().quantile([1 / 3, 2 / 3]).to_list()
    for label, m in (("small morning", f.abs() <= q[0]), ("mid", (f.abs() > q[0]) & (f.abs() <= q[1])), ("big morning", f.abs() > q[1])):
        x = np.sign(f[m]) * r[m]
        rows.append({"case": f"follow, {label}", "n": len(x), "mean_%": round(x.mean(), 4),
                     "win_%": round((x > 0).mean() * 100, 1), "t": round(tstat(x), 2)})
    print(pd.DataFrame(rows).to_string(index=False))


def prev_day_levels(df: pd.DataFrame) -> None:
    """เมื่อราคาเจาะ high/low ของเมื่อวาน: ปิดวันเหนือ/ใต้ระดับนั้นกี่ % และไปต่อเฉลี่ยเท่าไร"""
    d = daily(df)
    d["pdh"], d["pdl"] = d["high"].shift(), d["low"].shift()
    d = d.dropna()
    up = d[d["high"] > d["pdh"]]
    dn = d[d["low"] < d["pdl"]]
    # ผลถ้าซื้อตอนเจาะ PDH แล้วถือถึงปิดวัน (ประมาณ: เข้าที่ PDH พอดี)
    up_ret = (up["close"] / up["pdh"] - 1) * 100
    dn_ret = (dn["pdl"] / dn["close"] - 1) * 100       # short
    print(pd.DataFrame([
        {"event": "break PDH → long to close", "n": len(up), "close_beyond_%": round((up["close"] > up["pdh"]).mean() * 100, 1),
         "mean_%": round(up_ret.mean(), 3), "t": round(tstat(up_ret), 2)},
        {"event": "break PDL → short to close", "n": len(dn), "close_beyond_%": round((dn["close"] < dn["pdl"]).mean() * 100, 1),
         "mean_%": round(dn_ret.mean(), 3), "t": round(tstat(dn_ret), 2)},
    ]).to_string(index=False))


def inside_nr7(df: pd.DataFrame) -> None:
    """หลังวันแคบ (NR7 / inside day) วันถัดไปผันผวนกว่าปกติไหม (ข้อสมมติของ volatility contraction)"""
    d = daily(df)
    d["range"] = (d["high"] - d["low"]) / d["open"] * 100
    d["nr7"] = d["range"] == d["range"].rolling(7).min()
    d["inside"] = (d["high"] < d["high"].shift()) & (d["low"] > d["low"].shift())
    d["next_range"] = d["range"].shift(-1)
    d["next_absret"] = d["ret"].shift(-1).abs()
    base = d["next_range"].mean()
    rows = []
    for k in ("nr7", "inside"):
        s = d[d[k]]
        rows.append({"after": k, "n": len(s), "next_range_%": round(s["next_range"].mean(), 3),
                     "all_days_range_%": round(base, 3), "ratio": round(s["next_range"].mean() / base, 2),
                     "next_|ret|_%": round(s["next_absret"].mean(), 3)})
    print(pd.DataFrame(rows).to_string(index=False))


def trend_persistence(df: pd.DataFrame) -> None:
    """เทรนด์ H4 (ราคาเทียบ EMA50 ของ H4) บอกผลตอบแทน 1 วันข้างหน้าได้ไหม — แยก long/short"""
    h4 = df["close"].resample("4h").last().dropna()
    ema = h4.ewm(span=50, adjust=False).mean()
    fwd = (h4.shift(-6) / h4 - 1) * 100       # 24 ชม. ข้างหน้า
    up = fwd[(h4 > ema)].dropna()
    dn = -fwd[(h4 < ema)].dropna()
    allr = fwd.dropna()
    print(pd.DataFrame([
        {"state": "above EMA50(H4) → long 24h", "n": len(up), "mean_%": round(up.mean(), 3), "t": round(tstat(up), 2)},
        {"state": "below EMA50(H4) → short 24h", "n": len(dn), "mean_%": round(dn.mean(), 3), "t": round(tstat(dn), 2)},
        {"state": "unconditional long 24h (benchmark)", "n": len(allr), "mean_%": round(allr.mean(), 3), "t": round(tstat(allr), 2)},
    ]).to_string(index=False))
    print("  * ช่วง 4 ชม. ซ้อนกัน → t-stat สูงเกินจริงประมาณ √6 เท่า ให้หารด้วย ~2.4 ก่อนตีความ")


def main() -> None:
    pd.set_option("display.width", 200)
    for sym, sessions in [
        ("XAUUSD", {"asia": (0, 7), "london": (7, 12), "ny": (12, 17), "late": (17, 21)}),
        ("NAS100", {"asia": (0, 7), "europe": (7, 13), "ny_open": (13, 16), "ny_pm": (16, 20)}),
    ]:
        df = load(sym)
        print(f"\n\n######## {sym}  IN-SAMPLE {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}  "
              f"({df['close'].iloc[0]:.0f} → {df['close'].iloc[-1]:.0f}, "
              f"{(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:+.0f}%)")
        section("รายชั่วโมง (UTC)")
        hourly(df)
        section("รายวันในสัปดาห์")
        weekday(df)
        section("session momentum: ทิศ session ก่อน → session ถัดไป")
        sr = session_returns(df, sessions)
        momentum_table(sr)
        cols = list(sr.columns)
        section(f"แยกขึ้น/ลง: {'+'.join(cols[:2])} → {'+'.join(cols[2:])}")
        momentum_split(sr, cols[:2], cols[2:])
        section("high/low ของเมื่อวาน")
        prev_day_levels(df)
        section("volatility contraction")
        inside_nr7(df)
        section("เทรนด์ H4")
        trend_persistence(df)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()
