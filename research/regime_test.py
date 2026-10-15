"""
regime_test.py — ด่าน 0: Regime classifier มีความหมายจริงไหม (ช่วงวิจัย ไม่แตะ hold-out)

    python research/regime_test.py

เกณฑ์ (ลงทะเบียนล่วงหน้าใน research/hypotheses.md):
  a) TREND มี efficiency ratio 24h ข้างหน้าสูงกว่า RANGE (t ≥ 2.7)
  b) ผลตอบแทน 24h ตามทิศ TREND ดีกว่า drift ทั้งฝั่งขึ้นและลง
  c) RANGE: fade เข้าหา EMA20 ได้บวก และดีกว่าใน TREND
ตัวอย่างไม่ทับซ้อน: ทุก 24 แท่ง H1 → t-stat ไม่บวมจากข้อมูลซ้ำ
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.core.regime import Regime, RegimeClassifier  # noqa: E402
from bot.core.timeutil import UTC  # noqa: E402
from bot.data.store import bars_path, load_bars  # noqa: E402

RESEARCH_END = datetime(2026, 4, 1, tzinfo=UTC)   # hold-out เริ่มตรงนี้ — ห้ามแตะ
H = 24          # ขอบฟ้าข้างหน้า (แท่ง H1)
FADE_H = 6
T_CRIT = 2.7


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    return float((a.mean() - b.mean()) / np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))


def one_t(a: np.ndarray) -> float:
    return float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if len(a) > 2 and a.std() > 0 else float("nan")


def classify(symbol: str) -> pd.DataFrame:
    bars = [b for b in load_bars(bars_path(ROOT / "data" / "mt5", symbol, "M15")) if b.time < RESEARCH_END]
    clf = RegimeClassifier()
    rows = []
    for b in bars:
        st = clf.update(b)
        if st is not None:
            rows.append((st.time, st.h1.close, st.regime.value, st.adx, st.slope, st.vol_pct, st.h1.high, st.h1.low))
    df = pd.DataFrame(rows, columns=["time", "close", "regime", "adx", "slope", "vol_pct", "high", "low"])
    df = df[df["regime"] != Regime.UNKNOWN.value].reset_index(drop=True)
    c = df["close"].to_numpy()
    n = len(c)
    absmove = np.abs(np.diff(c, prepend=c[0]))
    csum = np.cumsum(absmove)
    fwd = np.full(n, np.nan)
    er = np.full(n, np.nan)
    fade = np.full(n, np.nan)
    fwd_range = np.full(n, np.nan)
    ema20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(n - H):
        fwd[i] = (c[i + H] / c[i] - 1) * 100
        path = csum[i + H] - csum[i]
        er[i] = abs(c[i + H] - c[i]) / path if path > 0 else np.nan
        fade[i] = -np.sign(c[i] - ema20[i]) * (c[i + FADE_H] / c[i] - 1) * 100
        fwd_range[i] = (hi[i + 1:i + H + 1].max() - lo[i + 1:i + H + 1].min()) / c[i] * 100
    df["fwd"], df["er"], df["fade"], df["fwd_range"] = fwd, er, fade, fwd_range
    df["regime_4"] = df["regime"].shift(-4)
    df["regime_24"] = df["regime"].shift(-H)
    return df


def report(symbol: str, df: pd.DataFrame) -> dict:
    s = df.iloc[::H].dropna(subset=["fwd", "regime_24"])          # ไม่ทับซ้อน
    print(f"\n######## {symbol}  {df['time'].iloc[0]:%Y-%m-%d} → {df['time'].iloc[-1]:%Y-%m-%d}  "
          f"H1={len(df)}  samples(24h, non-overlap)={len(s)}")

    rows = []
    base = df["regime"].value_counts(normalize=True)
    for r in [x.value for x in Regime if x is not Regime.UNKNOWN]:
        g = s[s["regime"] == r]
        if g.empty:
            continue
        rows.append({
            "regime": r, "n": len(g), "freq%": round(base.get(r, 0) * 100, 1),
            "stay_4h%": round((df.loc[df["regime"] == r, "regime_4"] == r).mean() * 100, 1),
            "stay_24h%": round((g["regime_24"] == r).mean() * 100, 1),
            "fwd_ER": round(g["er"].mean(), 3), "fwd_ret%": round(g["fwd"].mean(), 3),
            "fwd_range%": round(g["fwd_range"].mean(), 2), "fade6h%": round(g["fade"].mean(), 4),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    tr = s[s["regime"].isin(["TREND_UP", "TREND_DOWN"])]
    rg = s[s["regime"] == "RANGE"]
    up, dn = s[s["regime"] == "TREND_UP"], s[s["regime"] == "TREND_DOWN"]
    drift = s["fwd"].to_numpy()

    ta = welch_t(tr["er"].to_numpy(), rg["er"].to_numpy())
    tb_up = welch_t(up["fwd"].to_numpy(), drift)
    tb_dn = welch_t(-dn["fwd"].to_numpy(), -drift)
    tc1 = one_t(rg["fade"].to_numpy())
    tc2 = welch_t(rg["fade"].to_numpy(), tr["fade"].to_numpy())
    hv = s[s["regime"] == "HIGH_VOL"]["fwd_range"].to_numpy()
    dd = s[s["regime"] == "DEAD"]["fwd_range"].to_numpy()
    tv = welch_t(hv, dd)

    res = {
        "a": (ta >= T_CRIT, f"ER ข้างหน้า TREND {tr['er'].mean():.3f} vs RANGE {rg['er'].mean():.3f}  t={ta:.2f}"),
        "b_up": (tb_up >= T_CRIT, f"TREND_UP ret {up['fwd'].mean():+.3f}% vs drift {drift.mean():+.3f}%  t={tb_up:.2f} (n={len(up)})"),
        "b_dn": (tb_dn >= T_CRIT, f"TREND_DOWN short {-dn['fwd'].mean():+.3f}% vs -drift {-drift.mean():+.3f}%  t={tb_dn:.2f} (n={len(dn)})"),
        "c_pos": (tc1 >= T_CRIT, f"RANGE fade {rg['fade'].mean():+.4f}%  t={tc1:.2f}"),
        "c_vs": (tc2 >= T_CRIT, f"fade RANGE − TREND  t={tc2:.2f}"),
        "vol": (tv >= T_CRIT, f"ช่วงแกว่ง 24h หลัง HIGH_VOL {hv.mean():.2f}% vs DEAD {dd.mean():.2f}%  t={tv:.2f} (ข้อมูลประกอบ)"),
    }
    print()
    for k, (ok, msg) in res.items():
        print(f"  [{'PASS' if ok else 'fail'}] {k:6s} {msg}")
    return {k: v[0] for k, v in res.items()}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    pd.set_option("display.width", 200)
    out = {}
    for sym in ("XAUUSD", "NAS100"):
        out[sym] = report(sym, classify(sym))
    print("\nสรุปด่าน 0 (ต้องผ่านทั้ง a และ b_up และ b_dn สำหรับ H1/H2, c_pos และ c_vs สำหรับ H3/H4):")
    for sym, r in out.items():
        trend_ok = r["a"] and r["b_up"] and r["b_dn"]
        range_ok = r["c_pos"] and r["c_vs"]
        print(f"  {sym}: trend setups {'เปิด' if trend_ok else 'ปิด'} · range setups {'เปิด' if range_ok else 'ปิด'}")


if __name__ == "__main__":
    main()
