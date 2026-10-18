"""
manager_learn.py — ผู้จัดการพอร์ตเรียนรู้จากผลจริง: สายกลยุทธ์ไหน ได้/เสียเท่าไหร่ ในสภาพตลาดแบบไหน

  · ทุกนาที: ไม้ใหม่ที่เห็นครั้งแรก → จด "สภาพตลาดตอนเปิดไม้" (state/manager_tape.json)
  · ไม้ที่ปิดแล้ว → นับเข้าตาราง สาย × สภาพตลาด (state/manager_learn.json)
  · ตารางมีไม้พอ (LEARN_MIN) → ใช้ตัดสินว่าสายนั้นเหมาะกับตลาดตอนนี้ไหม แทนสมมติฐานตั้งต้น (manager.suits)
"""
from __future__ import annotations

import json
from typing import Any

from . import config as C

TAPE_PATH = C.STATE_DIR / "manager_tape.json"
LEARN_PATH = C.STATE_DIR / "manager_learn.json"
LEARN_MIN = 30            # ไม้ขั้นต่ำต่อช่อง (สาย × สภาพตลาด) ก่อนเชื่อผลจริงแทนสมมติฐาน (น้อยกว่านี้ = ดวงยังมีผลมาก)
LEARN_BAD = -1.0          # $/ไม้ เฉลี่ยต่ำกว่านี้ = ไม่เหมาะ
LEARN_GOOD = 0.0          # $/ไม้ เฉลี่ยตั้งแต่นี้ = เหมาะ (ระหว่างนั้น = ยังไม่ตัดสิน)


def _load(path, default: Any) -> Any:  # noqa: ANN001
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data: Any) -> None:  # noqa: ANN001
    from .live.fileio import atomic_write
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1))


class Learner:
    def __init__(self) -> None:
        self.tape: dict[str, dict] = _load(TAPE_PATH, {})
        d = _load(LEARN_PATH, {})
        self.table: dict[str, dict[str, dict]] = d.get("table", {})
        self.detail: dict[str, dict[str, dict]] = d.get("detail", {})      # สาย → "sess:asia" / "trend:against" → ผล
        self.counted: list[str] = d.get("counted", [])

    def observe(self, bots: dict[str, dict], regime: str | None, er: float | None, t: float, trend_dir: int = 0) -> list[dict]:
        """จดไม้ใหม่ + นับไม้ที่ปิดแล้ว · trend_dir = ทิศราคา BTC 2 ชม. (+1/-1/0) · คืนรายการไม้ที่เพิ่งนับ"""
        from datetime import datetime
        from .strategy.gate import session_of
        changed, learned = False, []
        for name, b in bots.items():
            for o in b.get("open", []):
                k = str(o["ticket"])
                if k not in self.tape and k not in self.counted and regime:
                    side = 1 if o.get("side") == "BUY" else -1
                    self.tape[k] = {"bot": name, "family": b.get("family"), "regime": regime, "er": er, "t": t,
                                    "session": session_of(datetime.fromisoformat(o["open_time"]).hour) if o.get("open_time") else None,
                                    "trend": "flat" if trend_dir == 0 else "with" if side == trend_dir else "against"}
                    changed = True
            for r in b.get("recent", []):
                k = str(r["ticket"])
                rec = self.tape.get(k)
                if rec is None or k in self.counted:
                    continue
                cell = self.table.setdefault(rec["family"] or "?", {}).setdefault(rec["regime"], {"n": 0, "pnl": 0.0, "wins": 0})
                cell["n"] += 1
                cell["pnl"] = round(cell["pnl"] + r["profit"], 2)
                cell["wins"] += r["profit"] > 0
                for key in (f"sess:{rec.get('session')}", f"trend:{rec.get('trend')}"):
                    if "None" in key:
                        continue
                    c2 = self.detail.setdefault(rec["family"] or "?", {}).setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
                    c2["n"] += 1
                    c2["pnl"] = round(c2["pnl"] + r["profit"], 2)
                    c2["wins"] += r["profit"] > 0
                self.counted = (self.counted + [k])[-5000:]
                del self.tape[k]
                learned.append({**r, **rec, "profit": r["profit"]})
                changed = True
        stale = [k for k, v in self.tape.items() if t - v["t"] > 7 * 86400]     # ไม้ที่หาผลไม่เจอเกิน 7 วัน
        for k in stale:
            del self.tape[k]
            changed = True
        if changed:
            _save(TAPE_PATH, self.tape)
            _save(LEARN_PATH, {"table": self.table, "detail": self.detail, "counted": self.counted})
        return learned

    def cell(self, family: str | None, regime: str) -> dict | None:
        c = self.table.get(family or "?", {}).get(regime)
        return c if c and c["n"] >= LEARN_MIN else None

    def fit(self, family: str | None, regime: str) -> bool | None:
        """ผลจริงพอ → True/False · ยังไม่พอ/กลางๆ → None (ใช้สมมติฐานตั้งต้นแทน)"""
        c = self.cell(family, regime)
        if c is None:
            return None
        avg = c["pnl"] / c["n"]
        return False if avg < LEARN_BAD else True if avg >= LEARN_GOOD else None

    def summary(self) -> dict[str, dict[str, dict]]:
        """ตารางสำหรับหน้าเว็บ/AI: สาย → สภาพตลาด → {n, avg, win_rate}"""
        return {f: {rg: {"n": c["n"], "avg": round(c["pnl"] / c["n"], 2), "win_rate": round(c["wins"] / c["n"] * 100)}
                    for rg, c in cells.items() if c["n"]} for f, cells in self.table.items()}


    def lessons(self, min_n: int = 5, limit: int = 5) -> list[dict]:
        """จุดอ่อนที่ชัดที่สุด: (สาย, เงื่อนไข) ที่ไม้พอและเฉลี่ยขาดทุนมากสุด — ให้ AI/บันทึกท้ายวัน/งานวิจัยใช้"""
        rows = []
        for fam, cells in list(self.table.items()) + [(f, c) for f, c in self.detail.items()]:
            for key, c in cells.items():
                if c["n"] >= min_n and c["pnl"] < 0:
                    rows.append({"family": fam, "when": key if ":" in key else f"regime:{key}", "n": c["n"],
                                 "avg": round(c["pnl"] / c["n"], 2), "total": c["pnl"]})
        return sorted(rows, key=lambda r: r["total"])[:limit]

    def detail_summary(self) -> dict[str, dict[str, dict]]:
        return {f: {k: {"n": c["n"], "avg": round(c["pnl"] / c["n"], 2), "win_rate": round(c["wins"] / c["n"] * 100)}
                    for k, c in cells.items() if c["n"]} for f, cells in self.detail.items()}


def load_summary() -> dict:
    return Learner().summary()
