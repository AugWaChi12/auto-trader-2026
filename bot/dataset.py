"""
dataset.py — ชุดข้อมูลวิจัยจากไม้จริง (state/research_dataset.csv · สำรองทุกคืนพร้อม state/)

1 ไม้ที่ปิด = 1 แถว พร้อมบริบทครบ: บอท สาย พารามิเตอร์ ตัวกรอง (โหมด + "จะบล็อกไหม") · สภาพตลาด/ช่วงเวลา/ทิศเทรนด์ตอนเปิด ·
คำทำนาย "ไม่ควรเข้า" ที่ค้างอยู่ตอนเปิด · ผลจริง · ผลที่งานวิจัยคาดไว้ต่อไม้ (wf $/ไม้) ไว้เทียบว่าตลาดจริงต่างจากที่ทดสอบแค่ไหน
ใช้โดย: รายงานวิจัยรายสัปดาห์ (live_report.py) · หลักฐานเลื่อนขั้นตัวกรอง (gate_evidence) · ดาวน์โหลด CSV (หน้าส่วนตัว)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from typing import Any

from . import config as C

PATH = C.STATE_DIR / "research_dataset.csv"
COLUMNS = ["ticket", "bot", "symbol", "tf", "island", "family", "side", "volume", "open_time", "close_time", "hold_min",
           "open_price", "close_price", "profit", "profit_std", "exit_reason", "regime", "er", "session", "trend", "ctx_src", "plan", "params",
           "gate_sig", "gate_mode", "gate_flag", "prediction_by", "research_usd_trade"]
# profit_std = กำไร/ขาดทุนเมื่อแปลงเป็น lot มาตรฐานของงานวิจัย (C.STD_LOTS) — ใช้คิดสถิติทุกอย่าง
# ctx_src = บริบทตลาดมาจากไหน: live (เห็นตอนไม้เปิด) · late (ไม้เปิด-ปิดระหว่างรอบ/ผู้จัดการรีสตาร์ท → คำนวณย้อนจากแท่งตอนเปิด)
#           backfill (เติมย้อนหลังตอนเริ่มงาน) · none (หาแท่งตอนเปิดไม่เจอ — มีผลจริงแต่ไม่มีสภาพตลาด)


def island_of(symbol: str | None) -> int | None:
    """เกาะในหน้า /sandbox: 1 = BTC · 2 = ทอง/NAS100"""
    return None if not symbol else 1 if symbol == "BTCUSD" else 2


def _flags(bot: str) -> list[dict]:
    try:
        return [json.loads(x) for x in (C.instance_state_dir(bot) / "gate_flags.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def row(rec: dict, bots: dict[str, dict], episodes: list[dict]) -> dict[str, Any]:
    """rec = แถวที่ Learner เพิ่งนับ (บริบทตอนเปิด + ข้อมูลไม้จริง)"""
    from .strategy.gate import signature
    b = bots.get(rec["bot"], {})
    params = dict(b.get("params") or {})
    sig = signature(params.get("skip_regimes") or [], params.get("skip_sessions") or []) if (
        params.get("skip_regimes") or params.get("skip_sessions")) else ""
    opened = datetime.fromisoformat(rec["open_time"])
    tf_min = {"M1": 1, "M5": 5, "M15": 15}.get(C.LIVE_INSTANCES.get(rec["bot"], {}).get("tf", "M5"), 5)
    flag = any(f.get("gate") == sig and timedelta(0) <= opened - datetime.fromisoformat(f["t"]) <= timedelta(minutes=3 * tf_min)
               for f in _flags(rec["bot"])) if sig else False
    ot = opened.timestamp()
    by = sorted({e["src"] for e in episodes if e.get("paper") and e["bot"] == rec["bot"] and e["start"] <= ot < e["end"]})
    close = rec.get("close_time")
    hold = round((datetime.fromisoformat(close) - opened).total_seconds() / 60, 1) if close else None
    wf = b.get("wf_usd_day")
    symbol = rec.get("symbol") or b.get("symbol")
    return {
        "ticket": rec.get("ticket"), "bot": rec["bot"], "symbol": symbol,
        "tf": rec.get("tf") or C.LIVE_INSTANCES.get(rec["bot"], {}).get("tf"), "island": island_of(symbol),
        "family": rec.get("family"), "side": rec.get("side"),
        "volume": rec.get("volume"), "open_time": rec["open_time"], "close_time": close, "hold_min": hold,
        "open_price": rec.get("open_price"), "close_price": rec.get("close_price"), "profit": rec.get("profit"),
        "profit_std": C.std_profit(symbol, rec.get("volume"), rec.get("profit") or 0.0),
        "exit_reason": rec.get("reason"), "regime": rec.get("regime"), "er": rec.get("er"), "session": rec.get("session"),
        "trend": rec.get("trend"), "ctx_src": rec.get("ctx_src") or "live", "plan": b.get("plan"),
        "params": json.dumps({k: v for k, v in params.items() if k not in ("skip_regimes", "skip_sessions", "gate_mode")},
                             ensure_ascii=False, sort_keys=True),
        "gate_sig": sig, "gate_mode": params.get("gate_mode", "active") if sig else "", "gate_flag": int(flag),
        "prediction_by": ",".join(by), "research_usd_trade": b.get("wf_usd_trade", wf),
    }


def _read_raw() -> tuple[list[str], list[dict]]:
    try:
        with PATH.open(newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            return list(r.fieldnames or []), list(r)
    except OSError:
        return [], []


def append(rows: list[dict]) -> int:
    """เพิ่มแถว (ข้ามตั๋วที่มีแล้ว) · ไฟล์หัวตารางเก่า → เขียนใหม่ด้วยคอลัมน์ปัจจุบัน (คอลัมน์ใหม่ว่างไว้) · คืนจำนวนแถวที่เพิ่ม"""
    if not rows:
        return 0
    header, old = _read_raw()
    seen = {str(r.get("ticket")) for r in old}
    fresh, keys = [], set()
    for r in rows:
        k = str(r.get("ticket"))
        if k in seen or k in keys:
            continue
        keys.add(k)
        fresh.append(r)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    if header and header != COLUMNS:
        from .live.fileio import atomic_write
        import io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(old + fresh)
        atomic_write(PATH, buf.getvalue())
        return len(fresh)
    if not fresh:
        return 0
    with PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if not header:
            w.writeheader()
        w.writerows(fresh)
    return len(fresh)


def load() -> list[dict]:
    try:
        with PATH.open(newline="", encoding="utf-8") as f:
            out = list(csv.DictReader(f))
    except OSError:
        return []
    for r in out:
        r["profit"] = float(r["profit"] or 0)
        r["profit_std"] = float(r["profit_std"]) if r.get("profit_std") not in (None, "") else \
            C.std_profit(r.get("symbol"), r.get("volume"), r["profit"])
        r["gate_flag"] = int(r["gate_flag"] or 0)
    return out


def gate_evidence(bot: str, sig: str) -> dict:
    """หลักฐานจากไม้จริงในโหมดสังเกต: ไม้ที่ตัวกรอง "จะบล็อก" กำไร/ขาดทุนเท่าไหร่ (เทียบกับไม้ที่ไม่ถูกจด)"""
    rows = [r for r in load() if r["bot"] == bot and r["gate_sig"] == sig and r["gate_mode"] == "observe"]
    flagged = [r["profit_std"] for r in rows if r["gate_flag"]]
    other = [r["profit_std"] for r in rows if not r["gate_flag"]]
    return {"n": len(flagged), "avg": round(sum(flagged) / len(flagged), 2) if flagged else None,
            "t": round(_t(flagged), 2) if len(flagged) > 2 else None,
            "n_other": len(other), "avg_other": round(sum(other) / len(other), 2) if other else None}


def _t(x: list[float]) -> float:
    n = len(x)
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / (n - 1)
    return m / (var ** 0.5 / n ** 0.5) if var > 0 else 0.0
