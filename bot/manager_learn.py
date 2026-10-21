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


LATE_MAX_H = 12           # ไม้ที่ไม่เคยเห็นตอนเปิด แต่เปิดภายใน N ชม. → คำนวณบริบทย้อนให้ระหว่างทำงาน (เก่ากว่านั้น = งาน backfill)
CTX_MIN_BARS = 125        # แท่ง M1 ก่อนเปิดไม้ที่ต้องมี (สภาพตลาด = M5 25 แท่ง · ทิศ = 120 นาที)
_TIMES: dict = {}         # เวลาแท่งที่แปลงแล้วของชุดแท่งล่าสุด (backfill เรียกซ้ำหลายร้อยไม้บนชุดเดียวกัน)


def _ts(iso: str) -> float:
    from datetime import datetime, timezone
    d = datetime.fromisoformat(iso)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def context_at(bars: list[list], open_time: str, side: str | None) -> dict:
    """สภาพตลาดตอนเปิดไม้ คำนวณย้อนจากแท่ง M1 [เวลา, เปิด, สูง, ต่ำ, ปิด] ด้วยสูตรเดียวกับรอบสด (manager.tick)
    แท่งไม่พอ/ไม่ครอบเวลาเปิด → regime None (ยังได้ session เพราะรู้จากเวลาเปิด)"""
    from datetime import datetime
    from .strategy.gate import session_of
    from .manager import market_regime
    out: dict = {"regime": None, "er": None, "trend": None,
                 "session": session_of(datetime.fromisoformat(open_time).hour)}
    t = _ts(open_time)
    key = (id(bars), len(bars), bars[0][0] if bars else None, bars[-1][0] if bars else None)
    if _TIMES.get("key") != key:
        _TIMES.update(key=key, times=[_ts(b[0]) for b in bars])
    from bisect import bisect_right
    cut = bars[:bisect_right(_TIMES["times"], t)]
    if len(cut) < CTX_MIN_BARS or t - _ts(cut[-1][0]) > 300:
        return out
    rg, er = market_regime(cut)
    if not rg:
        return out
    move = cut[-1][4] - cut[-121][4]
    td = 0 if rg != "trend" else (1 if move > 0 else -1)
    sd = 1 if side == "BUY" else -1
    out.update(regime=rg, er=er, trend="flat" if td == 0 else "with" if sd == td else "against")
    return out


class Learner:
    def __init__(self) -> None:
        self.tape: dict[str, dict] = _load(TAPE_PATH, {})
        d = _load(LEARN_PATH, {})
        self.table: dict[str, dict[str, dict]] = d.get("table", {})
        self.detail: dict[str, dict[str, dict]] = d.get("detail", {})      # สาย → "sess:asia" / "trend:against" → ผล
        self.counted: list[str] = d.get("counted", [])
        self.unit: str = d.get("unit", "usd")              # "std" = ตารางคิดเป็น lot มาตรฐานแล้ว

    def observe(self, bots: dict[str, dict], regime: str | None, er: float | None, t: float, trend_dir: int = 0,
                by_symbol: dict[str, tuple] | None = None, bars: dict[str, list] | None = None) -> list[dict]:
        """จดไม้ใหม่ + นับไม้ที่ปิดแล้ว · trend_dir = ทิศราคา 2 ชม. (+1/-1/0)
        by_symbol = ตลาด → (สภาพ, ER, ทิศ) ของแต่ละตลาด (ไม่ส่ง = ใช้ค่าเดียวกันทุกบอท) · คืนรายการไม้ที่เพิ่งนับ
        bars = ตลาด → แท่ง M1 ล่าสุด: ไม้ที่ไม่เคยเห็นตอนเปิด (เปิด-ปิดภายในรอบเดียว / ผู้จัดการรีสตาร์ท) → คำนวณบริบทย้อนจากแท่ง"""
        from datetime import datetime
        from .strategy.gate import session_of
        changed, learned = False, []
        for name, b in bots.items():
            if by_symbol is not None:
                regime, er, trend_dir = by_symbol.get(b.get("symbol"), (None, None, 0))
            for o in b.get("open", []):
                k = str(o["ticket"])
                if k not in self.tape and k not in self.counted and regime:
                    side = 1 if o.get("side") == "BUY" else -1
                    self.tape[k] = {"bot": name, "family": b.get("family"), "symbol": o.get("symbol") or b.get("symbol"),
                                    "regime": regime, "er": er, "t": t, "ctx_src": "live",
                                    "session": session_of(datetime.fromisoformat(o["open_time"]).hour) if o.get("open_time") else None,
                                    "trend": "flat" if trend_dir == 0 else "with" if side == trend_dir else "against"}
                    changed = True
            for r in b.get("recent", []):
                k = str(r["ticket"])
                if k in self.counted:
                    continue
                rec = self.tape.get(k)
                if rec is None:
                    if not r.get("open_time") or t - _ts(r["open_time"]) > LATE_MAX_H * 3600:
                        continue                # ไม้เก่า (ก่อนเริ่มงาน) → ให้ backfill ตัดสิน ไม่เดาบริบท
                    sym = r.get("symbol") or b.get("symbol")
                    rec = {"bot": name, "family": b.get("family"), "symbol": sym,
                           **context_at((bars or {}).get(sym) or [], r["open_time"], r.get("side")), "t": t}
                    rec["ctx_src"] = "late" if rec["regime"] else "none"
                self._count(k, rec, r, learned)
                changed = True
        stale = [k for k, v in self.tape.items() if t - v["t"] > 7 * 86400]     # ไม้ที่หาผลไม่เจอเกิน 7 วัน
        for k in stale:
            del self.tape[k]
            changed = True
        if changed:
            self._save_all()
        return learned

    def _count(self, k: str, rec: dict, r: dict, learned: list[dict]) -> None:
        """นับไม้ที่ปิดแล้ว 1 ไม้ · ไม่รู้สภาพตลาดตอนเปิด → ไม่เข้าตาราง (แต่ยังส่งออกไปเป็นแถวใน dataset)"""
        p = C.std_profit(rec.get("symbol") or r.get("symbol"), r.get("volume"), r["profit"])
        if rec.get("regime"):
            cell = self.table.setdefault(rec["family"] or "?", {}).setdefault(rec["regime"], {"n": 0, "pnl": 0.0, "wins": 0})
            cell["n"] += 1
            cell["pnl"] = round(cell["pnl"] + p, 2)
            cell["wins"] += p > 0
            for key in (f"sess:{rec.get('session')}", f"trend:{rec.get('trend')}"):
                if "None" in key:
                    continue
                c2 = self.detail.setdefault(rec["family"] or "?", {}).setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
                c2["n"] += 1
                c2["pnl"] = round(c2["pnl"] + p, 2)
                c2["wins"] += p > 0
        self.counted = (self.counted + [k])[-5000:]
        self.tape.pop(k, None)
        learned.append({**r, **rec, "profit": r["profit"]})

    def _save_all(self) -> None:
        _save(TAPE_PATH, self.tape)
        _save(LEARN_PATH, {"table": self.table, "detail": self.detail, "counted": self.counted, "unit": self.unit})

    def rebuild(self, rows: list[dict]) -> int:
        """สร้างตาราง สาย × สภาพตลาด ใหม่จาก dataset (หน่วย lot มาตรฐาน) — ใช้ครั้งเดียวตอนเปลี่ยนหน่วย
        ไม้เก่าที่เทรด lot ใหญ่จะไม่ถ่วงผลเกินจริง"""
        self.table, self.detail = {}, {}
        for r in rows:
            fam, p = r.get("family") or "?", r["profit_std"]
            if r.get("regime"):
                cell = self.table.setdefault(fam, {}).setdefault(r["regime"], {"n": 0, "pnl": 0.0, "wins": 0})
                cell["n"] += 1
                cell["pnl"] = round(cell["pnl"] + p, 2)
                cell["wins"] += p > 0
                for key in (f"sess:{r.get('session')}", f"trend:{r.get('trend')}"):
                    if key.endswith(":") or "None" in key:
                        continue
                    c2 = self.detail.setdefault(fam, {}).setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
                    c2["n"] += 1
                    c2["pnl"] = round(c2["pnl"] + p, 2)
                    c2["wins"] += p > 0
        self.unit = "std"
        self._save_all()
        return len(rows)

    def backfill(self, rows: dict[str, list[dict]], bots: dict[str, dict], bars: dict[str, list], since: str | None) -> list[dict]:
        """เติมไม้ที่ปิดแล้วแต่ยังไม่เคยนับ (ตอนเริ่มงาน) · rows = บอท → ไม้จริงจาก MT5 · bots = บอท → {family, symbol}
        since = เวลาที่ชุดกลยุทธ์ปัจจุบันเริ่มใช้ (ไม้ก่อนหน้านั้นเป็นกลยุทธ์อื่น → ไม่นับ กันข้อมูลปนกัน)"""
        cut = _ts(since) if since else 0.0
        learned: list[dict] = []
        for name, rs in rows.items():
            meta = bots.get(name) or {}
            for r in sorted(rs, key=lambda x: x.get("close_time") or ""):
                k = str(r.get("ticket"))
                if k in self.counted or k in self.tape or not r.get("open_time") or _ts(r["open_time"]) < cut:
                    continue
                sym = r.get("symbol") or meta.get("symbol")
                rec = {"bot": name, "family": meta.get("family"), "symbol": sym,
                       **context_at(bars.get(sym) or [], r["open_time"], r.get("side")), "t": _ts(r["open_time"])}
                rec["ctx_src"] = "backfill" if rec["regime"] else "none"
                self._count(k, rec, r, learned)
        if learned:
            self._save_all()
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
