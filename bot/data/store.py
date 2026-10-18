"""
store.py — อ่าน/เขียนแท่งราคาเป็น CSV และสเปคสัญญาเป็น JSON

    data/<source>/<SYMBOL>_<TF>.csv    time(UTC ISO),open,high,low,close,spread,volume
    data/<source>/<SYMBOL>.spec.json   SymbolSpec (ดึงจาก mt5.symbol_info)
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

import pandas as pd

from ..core.types import Bar, SymbolSpec


def bars_path(folder: Path, symbol: str, tf: str) -> Path:
    return folder / f"{symbol}_{tf}.csv"


def save_bars(path: Path, df: pd.DataFrame) -> None:
    """df ต้องมีคอลัมน์ time (tz-aware UTC), open, high, low, close, spread, volume"""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[["time", "open", "high", "low", "close", "spread", "volume"]].copy()
    out["time"] = out["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(path, index=False)


def load_bars(path: Path) -> list[Bar]:
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").drop_duplicates("time")
    if "spread" not in df:
        df["spread"] = 0.0
    if "volume" not in df:
        df["volume"] = 0.0
    times = df["time"].dt.to_pydatetime()
    cols = [df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "spread", "volume")]
    return [Bar(t, *(float(c[i]) for c in cols)) for i, t in enumerate(times)]


def save_spec(folder: Path, spec: SymbolSpec) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{spec.name}.spec.json").write_text(json.dumps(asdict(spec), indent=2), encoding="utf-8")


def load_spec(folder: Path, symbol: str, default: SymbolSpec) -> SymbolSpec:
    """อ่านสเปคจากไฟล์ (ถ้ามี) — ค่า slippage/min_spread/commission ใน config ยังมีผลถ้าไฟล์ไม่ได้ระบุ"""
    path = folder / f"{symbol}.spec.json"
    if not path.exists():
        return default
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(SymbolSpec)}
    merged = asdict(default) | {k: v for k, v in raw.items() if k in known}
    merged["name"] = symbol
    return SymbolSpec(**merged)
