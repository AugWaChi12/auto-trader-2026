"""
researcher.py — ทีมงาน Researcher: หาจุดอ่อน/จุดแข็งจากไม้จริงทุกวัน (ไม่ใช้เครดิต AI)

แบ่งไม้จริงตามลักษณะตอนเข้า (ตลาด สาย ช่วงเวลา สภาพตลาด ทิศเทียบเทรนด์ with/against/flat) แล้วทดสอบทุกช่อง
ช่องที่ขึ้นรายงานต้องมีไม้พอ (MIN_N) · t ถึงเกณฑ์ · และทิศเดียวกันทั้งครึ่งแรกและครึ่งหลังของข้อมูล
ลองหลายช่องย่อมเจอของบังเอิญ → รายงานจำนวนที่คาดว่าบังเอิญไว้ด้วย · ผลเป็นสมมติฐานให้ส่งเข้าวิจัย ไม่ใช่กฎใช้งาน
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from . import config as C

PATH = C.STATE_DIR / "researcher.json"
SINCE = "2026-10-21"      # ก่อนหน้านี้กติกาไม้ยังต่างจากปัจจุบัน
MIN_N = 15
T_MIN = 2.0
FEATURES = ("symbol", "family", "session", "regime", "trend", "hours")


def _t(x: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / (n - 1)
    return m / math.sqrt(var / n) if var > 0 else 0.0


def _hours(r: dict) -> str:
    h = datetime.fromisoformat(r["open_time"]).astimezone(timezone.utc).hour // 4 * 4
    return f"{h:02d}-{h + 4:02d}UTC"


def features(r: dict) -> dict[str, str]:
    return {"symbol": r.get("symbol") or "?", "family": r.get("family") or "?", "session": r.get("session") or "?",
            "regime": r.get("regime") or "?", "trend": r.get("trend") or "?", "hours": _hours(r)}


def scan(rows: list[dict]) -> dict[str, Any]:
    """คืน {weak, strong, tests, n} · หน่วย $ ที่ lot มาตรฐาน"""
    rows = sorted((r for r in rows if r["open_time"] >= SINCE), key=lambda r: r["open_time"])
    half = len(rows) // 2
    cells: dict[tuple, list[tuple[int, float]]] = {}
    for i, r in enumerate(rows):
        f = features(r)
        keys = [((a, f[a]),) for a in FEATURES] + [((a, f[a]), (b, f[b])) for a, b in combinations(FEATURES, 2)]
        for k in keys:
            cells.setdefault(k, []).append((i, r["profit_std"]))
    found, tests = [], 0
    for k, v in cells.items():
        x = [p for _, p in v]
        if len(x) < MIN_N:
            continue
        tests += 1
        a, b = [p for i, p in v if i < half], [p for i, p in v if i >= half]
        t = _t(x)
        if abs(t) < T_MIN or not a or not b or (sum(a) > 0) != (sum(b) > 0) or (sum(a) > 0) != (t > 0):
            continue
        found.append({"cell": " · ".join(f"{n}={val}" for n, val in k), "n": len(x), "sum": round(sum(x), 2),
                      "avg": round(sum(x) / len(x), 2), "t": round(t, 2), "first": round(sum(a), 2), "second": round(sum(b), 2)})
    return {"n": len(rows), "tests": tests, "by_chance": round(tests * 0.05, 1),
            "weak": sorted((c for c in found if c["t"] < 0), key=lambda c: c["sum"])[:8],
            "strong": sorted((c for c in found if c["t"] > 0), key=lambda c: -c["sum"])[:8]}


def run(rows: list[dict]) -> dict[str, Any]:
    from .live.fileio import atomic_write
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **scan(rows)}
    atomic_write(PATH, json.dumps(out, ensure_ascii=False, indent=1))
    return out


WORDS = {"asia": "ช่วงเอเชีย", "europe": "ช่วงยุโรป", "us": "ช่วงอเมริกา", "range": "ตลาดออกข้าง", "trend": "ตลาดมีเทรนด์",
         "mixed": "ตลาดไม่ชัดทาง", "with": "ตามเทรนด์", "against": "สวนเทรนด์", "flat": "ไม่มีเทรนด์", "XAUUSD": "ทอง",
         "BTCUSD": "BTC", "NAS100": "NAS"}


def plain(cell: str) -> str:
    """family=ema · hours=04-08UTC → EMA ตัดกัน 11–15 น."""
    from .strategy.catalog import LABELS
    out = []
    for part in cell.split(" · "):
        kind, val = part.split("=", 1)
        if kind == "family":
            out.append(LABELS.get(val, val))
        elif kind == "hours":
            a = int(val[:2])
            out.append(f"{(a + 7) % 24:02d}–{(a + 11) % 24:02d} น.")
        else:
            out.append(WORDS.get(val, val))
    return " ".join(out)


def summary_text(r: dict) -> str:
    def line(c: dict) -> str:
        return f"{plain(c['cell'])}: {c['n']} ไม้ {c['sum']:+.0f}$"
    bad = "\n".join(f"👎 {line(c)}" for c in r["weak"][:2]) or "👎 ยังไม่เจอจุดอ่อนชัด"
    good = "\n".join(f"👍 {line(c)}" for c in r["strong"][:2]) or "👍 ยังไม่เจอจุดแข็งชัด"
    return f"🔬 Researcher วันนี้ (ยังเป็นแค่ข้อสังเกต)\n{bad}\n{good}"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
