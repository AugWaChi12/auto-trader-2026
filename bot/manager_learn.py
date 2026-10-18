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
LEARN_MIN = 8             # ไม้ขั้นต่ำต่อช่อง (สาย × สภาพตลาด) ก่อนเชื่อผลจริงแทนสมมติฐาน
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
        self.counted: list[str] = d.get("counted", [])

    def observe(self, bots: dict[str, dict], regime: str | None, er: float | None, t: float) -> list[dict]:
        """จดไม้ใหม่ + นับไม้ที่ปิดแล้ว · คืนรายการไม้ที่เพิ่งนับ"""
        changed, learned = False, []
        for name, b in bots.items():
            for o in b.get("open", []):
                k = str(o["ticket"])
                if k not in self.tape and k not in self.counted and regime:
                    self.tape[k] = {"bot": name, "family": b.get("family"), "regime": regime, "er": er, "t": t}
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
                self.counted = (self.counted + [k])[-5000:]
                del self.tape[k]
                learned.append({**rec, "profit": r["profit"]})
                changed = True
        stale = [k for k, v in self.tape.items() if t - v["t"] > 7 * 86400]     # ไม้ที่หาผลไม่เจอเกิน 7 วัน
        for k in stale:
            del self.tape[k]
            changed = True
        if changed:
            _save(TAPE_PATH, self.tape)
            _save(LEARN_PATH, {"table": self.table, "counted": self.counted})
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


def load_summary() -> dict:
    return Learner().summary()
