"""
research_weekly.py — วิจัยประจำสัปดาห์บนเซิร์ฟเวอร์ (python -m bot research · ผู้จัดการพอร์ตสั่งเองทุกอาทิตย์ 04:00 เวลาไทย)

  1) ดึงแท่ง BTC M5 ย้อนหลัง 1 ปีจาก MT5 → data/mt5
  2) research/m5_lab.py แบบ BTC อย่างเดียว 1 โปรเซส (nice 19 ไม่แย่ง MT5/บอท)
     walk-forward ทุกพารามิเตอร์ของทุกสาย → เลือกชุดที่ดีสุดใน 6 เดือนล่าสุด
  3) retune: บอทแต่ละตัวคงสายเดิม เปลี่ยนแค่พารามิเตอร์/วิธีออก → state/m5_bots.json
     supervisor เห็นงานเปลี่ยน → รีสตาร์ทบอททดสอบให้ใช้ค่าใหม่
  4) ประกาศผลในแชท + Telegram
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C
from .manager import say

log = logging.getLogger("research")
STATUS_PATH = C.STATE_DIR / "manager_research.json"
DATA_DIR = C.ROOT / "data" / "mt5"


def load_status() -> dict:
    """สถานะวิจัยล่าสุด · "กำลังรัน" นานเกิน 3 ชม. = ถูกขัดจังหวะ (เช่น container บอทรีสตาร์ทกลางคัน)"""
    try:
        d = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if d.get("running") and time.time() - d.get("started", 0) > 3 * 3600:
        d = {**d, "running": False, "error": "ถูกขัดจังหวะกลางคัน (container บอทรีสตาร์ท) — จะรันใหม่รอบถัดไป"}
    return d


def save_status(d: dict) -> None:
    from .live.fileio import atomic_write
    atomic_write(STATUS_PATH, json.dumps(d, ensure_ascii=False, indent=1, default=str))


def _diff(old: dict, new: dict) -> str:
    """อธิบายพารามิเตอร์ที่เปลี่ยนแบบสั้น: fast 9→12 · วิธีออก native→ratchet"""
    parts = [f"{k} {old['params'].get(k)}→{v}" for k, v in new["params"].items() if old["params"].get(k) != v]
    if old["plan"] != new["plan"]:
        parts.append(f"วิธีออก {old['plan']}→{new['plan']}")
    return ", ".join(parts) or "–"


def describe(changes: list[dict], minutes: float) -> str:
    if not changes:
        return f"🔬 วิจัยประจำสัปดาห์เสร็จ ({minutes:.0f} นาที): พารามิเตอร์เดิมยังดีที่สุดทุกตัว — ไม่เปลี่ยนอะไร"
    lines = [f"{c['bot']} ({c['family']}): {_diff(c['old'], c['new'])} · walk-forward {c['wf_usd_day']:+.2f}$/วัน" for c in changes]
    return (f"🔬 วิจัยประจำสัปดาห์เสร็จ ({minutes:.0f} นาที): ปรับพารามิเตอร์ {len(changes)} ตัวด้วยข้อมูลล่าสุด — "
            + " · ".join(lines) + " · บอทที่เปลี่ยนจะรีสตาร์ทใช้ค่าใหม่")


def run_weekly(download: bool = True) -> list[dict]:
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    t0 = time.time()
    save_status({"started": t0, "running": True})
    say("info", "🔬 เริ่มวิจัยประจำสัปดาห์: ดึงข้อมูล BTC ล่าสุด แล้วทดสอบย้อนหลังราว 2,000 แบบ (ใช้เวลาประมาณ 30–60 นาที)")
    try:
        if download:
            from .data import mt5_source
            mt5_source.connect()
            end = datetime.now(timezone.utc) + timedelta(days=1)
            mt5_source.download("BTCUSD", "M5", end - timedelta(days=365), end, DATA_DIR, C.SERVER_TIME_MODE,
                                C.DEFAULT_SPECS["BTCUSD"])
        sys.path.insert(0, str(C.ROOT / "research"))
        import m5_lab  # noqa: PLC0415 — โหลดตอนใช้ (มี numpy/pandas หนัก)
        changes = m5_lab.main(["--symbols", "BTCUSD", "--workers", os.environ.get("RESEARCH_WORKERS", "1"),
                               "--retune", "--assign-to", str(C.M5_BOTS_TUNED)]) or []
    except Exception as e:  # noqa: BLE001
        log.exception("research failed")
        save_status({"started": t0, "finished": time.time(), "running": False, "error": str(e)})
        say("alert", f"🔬 วิจัยประจำสัปดาห์ไม่สำเร็จ: {e}")
        return []
    minutes = (time.time() - t0) / 60
    text = describe(changes, minutes)
    save_status({"started": t0, "finished": time.time(), "running": False, "minutes": round(minutes, 1),
                 "changes": changes, "summary": text})
    say("journal", text)
    return changes
