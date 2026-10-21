"""
allocator.py — ผู้จัดการบริหารพอร์ตแบบ dynamic (บอททดสอบเท่านั้น · ไม่แตะ main)

  1) ขนาดไม้ (state/manager_alloc.json): บอทแต่ละตัวได้ตัวคูณ lot 0.25–2.0 เท่าของ lot มาตรฐาน
     · กฎ (ทุกวันตอนขึ้นวันใหม่เวลาไทย): ดูผลจริงของสายปัจจุบันของบอทนั้น (หน่วย lot มาตรฐาน) → t-stat
         ไม้ < 15 = 1.0 · t ≥ 2 → 2.0 · t ≥ 1 → 1.5 · t ≤ −2 → 0.25 · t ≤ −1 → 0.5 · กลางๆ → 1.0
     · AI (Claude/Gemini) สั่งทับได้ระหว่างวัน (set_lot_scale) — มีผลถึงรอบกฎถัดไป
     · บอทอ่านไฟล์นี้ตอนคำนวณขนาดไม้ (risk_manager.lot_scale)
  2) สลับสาย (state/m5_bots.json): สายปัจจุบันขาดทุนชัดในไม้จริง (≥ 20 ไม้, t ≤ −1.5) และงานวิจัย walk-forward ไม่บวก
     → เปลี่ยนเป็นสายที่หลักฐานดีที่สุดของตลาดเดียวกัน (ช่วงทดสอบซ่อนบวก) · วันละไม่เกิน 1 ตัว · ตัวเดิมต้องห่างจากรอบก่อน ≥ 7 วัน
     supervisor เห็นไฟล์เปลี่ยน → รีสตาร์ทบอทตัวนั้นด้วยสายใหม่ให้เอง
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from . import config as C

ALLOC_PATH = C.STATE_DIR / "manager_alloc.json"
LAB_PATH = C.ROOT / "research" / "out" / "m5_lab.json"
SCALE_MIN, SCALE_MAX = 0.25, 2.0
SCALE_MIN_N = 15
ROTATE_MIN_N = 20
ROTATE_T = -1.5
ROTATE_COOLDOWN_DAYS = 7
LOOKBACK_DAYS = 30


def _now() -> float:
    return time.time()


def load_alloc() -> dict[str, dict]:
    try:
        return json.loads(ALLOC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_alloc(a: dict[str, dict]) -> None:
    from .live.fileio import atomic_write
    atomic_write(ALLOC_PATH, json.dumps(a, ensure_ascii=False, indent=1))


def scale_of(bot: str) -> float:
    """ตัวคูณ lot ปัจจุบันของบอท (ไม่มี/อ่านไม่ได้ = 1.0) · main = 1.0 เสมอ"""
    if bot == "main":
        return 1.0
    try:
        s = float(load_alloc().get(bot, {}).get("scale", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return min(SCALE_MAX, max(SCALE_MIN, s))


def set_scale(bot: str, scale: float, reason: str, by: str) -> float:
    s = round(min(SCALE_MAX, max(SCALE_MIN, float(scale))), 2)
    a = load_alloc()
    a[bot] = {"scale": s, "reason": reason[:200], "by": by, "since": _now()}
    save_alloc(a)
    return s


def _t(x: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / (n - 1)
    return m / math.sqrt(var / n) if var > 0 else 0.0


def evidence(rows: list[dict], bot: str, family: str, since: float | None = None) -> dict:
    """ผลจริงของบอทในสายปัจจุบัน (หน่วย lot มาตรฐาน) ใน LOOKBACK_DAYS วันล่าสุด"""
    cut = max(since or 0.0, _now() - LOOKBACK_DAYS * 86400)
    x = [r["profit_std"] for r in rows if r["bot"] == bot and r.get("family") == family
         and datetime.fromisoformat(r["open_time"]).timestamp() >= cut]
    return {"n": len(x), "avg": round(sum(x) / len(x), 2) if x else None, "t": round(_t(x), 2)}


def rule_scale(ev: dict) -> float:
    if ev["n"] < SCALE_MIN_N:
        return 1.0
    t = ev["t"]
    return 2.0 if t >= 2 else 1.5 if t >= 1 else 0.25 if t <= -2 else 0.5 if t <= -1 else 1.0


def _since(b: dict) -> float | None:
    r = b.get("reassigned") or {}
    try:
        return datetime.strptime(r["at"], "%Y%m%d%H%M").replace(tzinfo=timezone.utc).timestamp() if r.get("at") else None
    except ValueError:
        return None


def rebalance(rows: list[dict], bots: list[dict]) -> list[dict]:
    """รอบกฎรายวัน: ตั้งตัวคูณ lot ทุกบอทจากหลักฐาน · คืนรายการที่เปลี่ยน"""
    a = load_alloc()
    changes = []
    for b in bots:
        ev = evidence(rows, b["bot"], b["family"], _since(b))
        s = rule_scale(ev)
        old = float(a.get(b["bot"], {}).get("scale", 1.0))
        why = (f"{b['family']} ไม้จริง {ev['n']} ไม้ เฉลี่ย {ev['avg']:+.2f}$/ไม้ (lot มาตรฐาน) t={ev['t']:+.2f}"
               if ev["n"] else f"{b['family']} ยังไม่มีไม้จริง")
        if ev["n"] < SCALE_MIN_N:
            why += f" — ไม้ยังไม่ถึง {SCALE_MIN_N} ใช้ขนาดมาตรฐาน"
        a[b["bot"]] = {"scale": s, "reason": why, "by": "rules", "since": _now(), "evidence": ev}
        if abs(s - old) > 1e-9:
            changes.append({"bot": b["bot"], "old": old, "new": s, "reason": why})
    save_alloc(a)
    return changes


def _core(params: dict) -> dict:
    return {k: v for k, v in params.items() if k not in ("skip_regimes", "skip_sessions", "gate_mode")}


def candidates(symbol: str, exclude_family: str, used: list[dict]) -> list[dict]:
    """สายที่หลักฐานดีของตลาดนี้ (ช่วงทดสอบซ่อนบวก + walk-forward รวมช่วงซ่อนไม่ติดลบ) เรียงจากดีสุด
    สายที่ยังไม่มีบอทตัวไหนเล่นในตลาดนี้มาก่อน · ซ้ำสายได้ถ้าใช้ชุดพารามิเตอร์ต่างกัน"""
    try:
        slots = json.loads(LAB_PATH.read_text(encoding="utf-8"))["slots"]
    except (OSError, ValueError, KeyError):
        return []
    fams = {b["family"] for b in used if b["symbol"] == symbol}
    taken = [(b["family"], _core(b["params"])) for b in used if b["symbol"] == symbol]
    out = []
    for z in slots:
        h, w = z.get("holdout") or {}, z.get("wf") or {}
        if z.get("symbol") != symbol or z["family"] == exclude_family or not z.get("live"):
            continue
        if (h.get("usd_day") or 0) <= 0 or (w.get("usd_day") or 0) + (h.get("usd_day") or 0) <= 0:
            continue
        pick = next((p for p in (z["live"], z.get("pre_holdout")) if p and (z["family"], _core(p["params"])) not in taken), None)
        if pick is None:
            continue
        out.append({"family": z["family"], "params": dict(pick["params"]), "plan": pick["plan"],
                    "score": round((w.get("usd_day") or 0) + (h.get("usd_day") or 0), 2),
                    "holdout_usd_day": h.get("usd_day"), "wf_usd_day": w.get("usd_day"), "new_family": z["family"] not in fams})
    return sorted(out, key=lambda c: (not c["new_family"], -c["score"]))


def rotate(rows: list[dict]) -> dict | None:
    """สลับสายบอทที่พิสูจน์แล้วว่าเสีย (วันละไม่เกิน 1 ตัว) · คืนรายละเอียด หรือ None"""
    path = C.M5_BOTS_TUNED if C.M5_BOTS_TUNED.exists() else C.M5_BOTS_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bots = doc["bots"]
    if any((b.get("reassigned") or {}).get("by") == "manager" and _now() - (_since(b) or 0) < 20 * 3600 for b in bots):
        return None                                   # วันละไม่เกิน 1 ตัว (นับข้ามการรีสตาร์ท)
    worst = None
    for b in bots:
        since = _since(b)
        if since and _now() - since < ROTATE_COOLDOWN_DAYS * 86400:
            continue
        ev = evidence(rows, b["bot"], b["family"], since)
        if ev["n"] < ROTATE_MIN_N or ev["t"] > ROTATE_T or (b.get("wf_usd_day") or 0) > 0:
            continue
        if worst is None or ev["t"] < worst[1]["t"]:
            worst = (b, ev)
    if worst is None:
        return None
    b, ev = worst
    cands = candidates(b["symbol"], b["family"], bots)
    if not cands:
        return None
    c = cands[0]
    old = {k: b.get(k) for k in ("family", "params", "plan")}
    b.update(family=c["family"], params=c["params"], plan=c["plan"], wf_usd_day=c["wf_usd_day"])
    b["reassigned"] = {"at": datetime.now(timezone.utc).strftime("%Y%m%d%H%M"), "from": old, "by": "manager",
                       "why": f"ไม้จริง {ev['n']} ไม้ เฉลี่ย {ev['avg']:+.2f}$/ไม้ t={ev['t']:+.2f} · walk-forward {old.get('family')} ไม่บวก"}
    from .live.fileio import atomic_write
    atomic_write(C.M5_BOTS_TUNED, json.dumps(doc, ensure_ascii=False, indent=1))
    a = load_alloc()
    a[b["bot"]] = {"scale": 1.0, "reason": f"เพิ่งเปลี่ยนเป็นสาย {c['family']} — เริ่มที่ขนาดมาตรฐาน", "by": "rules", "since": _now()}
    save_alloc(a)
    return {"bot": b["bot"], "symbol": b["symbol"], "from": old["family"], "to": c["family"], "evidence": ev, "candidate": c}


def summary() -> dict[str, Any]:
    """สำหรับ AI / หน้าเว็บ: บอท → {scale, by, reason}"""
    return {b: {k: v.get(k) for k in ("scale", "by", "reason")} for b, v in load_alloc().items()}
