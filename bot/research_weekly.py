"""
research_weekly.py — วิจัยบนเซิร์ฟเวอร์ (python -m bot research · ผู้จัดการพอร์ตสั่งเองทุกคืน 04:00 เวลาไทย · RESEARCH_EVERY_DAYS)

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


GATE_TH = {"trend": "ตลาดเทรนด์", "range": "ตลาดออกข้าง", "mixed": "ตลาดไม่ชัดทาง", "asia": "ช่วงเอเชีย",
           "europe": "ช่วงยุโรป", "us": "ช่วงอเมริกา"}


def _gate_text(params: dict) -> str:
    skip = list(params.get("skip_regimes") or []) + list(params.get("skip_sessions") or [])
    return "ไม่เข้าไม้ตอน" + "/".join(GATE_TH.get(x, x) for x in skip) if skip else "ไม่มีตัวกรอง"


def _diff(old: dict, new: dict) -> str:
    """อธิบายพารามิเตอร์ที่เปลี่ยนแบบสั้น: fast 9→12 · วิธีออก native→ratchet · ตัวกรองใหม่"""
    gk = ("skip_regimes", "skip_sessions", "gate_mode")
    parts = [f"{k} {old['params'].get(k)}→{v}" for k, v in new["params"].items() if k not in gk and old["params"].get(k) != v]
    if any((old["params"].get(k) or []) != (new["params"].get(k) or []) for k in gk):
        mode = {"observe": " (โหมดสังเกต: ยังไม่บล็อกจริง รอพิสูจน์กับไม้จริง)",
                "active": " (✅ ใช้จริงแล้ว — ไม้จริงยืนยัน)"}.get(new["params"].get("gate_mode"), "")
        parts.append(f"ตัวกรอง: {_gate_text(new['params'])}{mode}")
    if old["plan"] != new["plan"]:
        parts.append(f"วิธีออก {old['plan']}→{new['plan']}")
    return ", ".join(parts) or "–"


def describe(changes: list[dict], minutes: float) -> str:
    if not changes:
        return f"🔬 วิจัยประจำวันเสร็จ ({minutes:.0f} นาที): ค่าเดิม + ตัวกรองเดิมยังดีที่สุดทุกตัว — ไม่เปลี่ยนอะไร"
    lines = []
    for c in changes:
        g = c.get("gate") or {}
        proof = (f" (ทดสอบย้อนหลัง: ครึ่งที่แย่กว่า {g['score_from']:+.2f}→{g['score_to']:+.2f}$/ไม้ · 2 เดือนล่าสุด "
                 f"{g['holdout_from']:+.2f}→{g['holdout_to']:+.2f}$/วัน)") if g.get("score_to") is not None else ""
        lines.append(f"{c['bot']} ({c['family']}): {_diff(c['old'], c['new'])}{proof} · walk-forward {c['wf_usd_day']:+.2f}$/วัน")
    return (f"🔬 วิจัยประจำวันเสร็จ ({minutes:.0f} นาที): ปรับ {len(changes)} ตัวด้วยข้อมูลล่าสุด + ตัวกรองจากสิ่งที่ผู้จัดการเรียนรู้ — "
            + " · ".join(lines) + " · บอทที่เปลี่ยนจะรีสตาร์ทใช้ค่าใหม่")


def run_weekly(download: bool = True) -> list[dict]:
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    t0 = time.time()
    save_status({"started": t0, "running": True})
    say("info", "🔬 เริ่มวิจัยประจำวัน: ดึงข้อมูลล่าสุดของทุกตลาด (BTC/ทอง/NAS100) → ทดสอบย้อนหลังทุกสาย + ตัวกรองจากสิ่งที่ผู้จัดการเรียนรู้ (ประมาณ 1.5–2 ชม.)")
    try:
        symbols = sorted({b["symbol"] for b in C.load_m5_bots()}) or ["BTCUSD"]
        if download:
            from .data import mt5_source
            mt5_source.connect()
            end = datetime.now(timezone.utc) + timedelta(days=1)
            for sym in symbols:
                mt5_source.download(sym, "M5", end - timedelta(days=365 if sym == "BTCUSD" else 520), end, DATA_DIR,
                                    C.SERVER_TIME_MODE, C.DEFAULT_SPECS[sym])
        sys.path.insert(0, str(C.ROOT / "research"))
        import m5_lab  # noqa: PLC0415 — โหลดตอนใช้ (มี numpy/pandas หนัก)
        from .dataset import gate_evidence
        from .strategy.gate import signature
        for b in C.load_m5_bots():                       # หลักฐานไม้จริงของตัวกรองโหมดสังเกต → ใช้ตัดสินเลื่อนขั้น
            p = b["params"]
            if p.get("skip_regimes") or p.get("skip_sessions"):
                m5_lab.GATE_EVIDENCE[b["bot"]] = gate_evidence(b["bot"], signature(p.get("skip_regimes") or [], p.get("skip_sessions") or []))
        changes = m5_lab.main(["--symbols", ",".join(symbols), "--workers", os.environ.get("RESEARCH_WORKERS", "1"),
                               "--retune", "--assign-to", str(C.M5_BOTS_TUNED)]) or []
    except Exception as e:  # noqa: BLE001
        log.exception("research failed")
        save_status({"started": t0, "finished": time.time(), "running": False, "error": str(e)})
        say("alert", f"🔬 วิจัยประจำวันไม่สำเร็จ: {e}")
        return []
    minutes = (time.time() - t0) / 60
    text = describe(changes, minutes)
    save_status({"started": t0, "finished": time.time(), "running": False, "minutes": round(minutes, 1),
                 "changes": changes, "summary": text})
    say("journal", text)
    return changes


# ---------------------------------------------------------------- ล่า edge ใหม่ (อาทิตย์ละครั้ง · ไม่ใส่ให้บอทเอง)
EXPLORE_PATH = C.STATE_DIR / "explorer.json"
EXPLORE_TFS = ("M15", "H1")


def load_explore() -> dict:
    try:
        return json.loads(EXPLORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_edge(z: dict) -> bool:
    """เกณฑ์เดียวกับงานวิจัยหลัก: walk-forward บวก (ไม้พอ) · 2 เดือนล่าสุดยังบวก · ชนะกลุ่มสุ่มทิศ"""
    wf, ho, rnd = z["wf"], z["holdout"], z.get("wf_random")
    return wf["n"] >= 30 and wf["usd_day"] > 0 and ho["usd_day"] > 0 and (rnd is None or wf["usd_day"] > rnd["usd_day"])


def run_explore(tfs: tuple[str, ...] = EXPLORE_TFS) -> list[dict]:
    """ลองทุกสายกลยุทธ์บน BTC timeframe ใหญ่ขึ้น (ต้นทุนสเปรดต่อไม้กินกำไรน้อยกว่า M1/M5) → รายงานตัวที่ผ่านเกณฑ์ ให้ผู้ใช้ตัดสินใจเอง"""
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    t0 = time.time()
    say("info", f"🧭 เริ่มล่า edge ประจำสัปดาห์: ทุกสายกลยุทธ์บน BTC {'/'.join(tfs)} (walk-forward + เทียบกลุ่มสุ่ม)")
    sys.path.insert(0, str(C.ROOT / "research"))
    import m5_lab  # noqa: PLC0415
    rows = []
    try:
        for tf in tfs:
            for z in m5_lab.main(["--symbols", "BTCUSD", "--workers", os.environ.get("RESEARCH_WORKERS", "1"), "--tf", tf, "--explore"]) or []:
                rows.append({"tf": tf, "family": z["family"], "wf": z["wf"], "holdout": z["holdout"],
                             "random": z.get("wf_random"), "live": z["live"], "edge": is_edge(z)})
    except Exception as e:  # noqa: BLE001
        log.exception("explore failed")
        say("alert", f"🧭 ล่า edge ไม่สำเร็จ: {e}")
        return []
    rows.sort(key=lambda r: r["wf"]["usd_day"], reverse=True)
    found = [r for r in rows if r["edge"]]
    from .live.fileio import atomic_write
    atomic_write(EXPLORE_PATH, json.dumps({"finished": time.time(), "minutes": round((time.time() - t0) / 60, 1),
                                           "rows": rows, "found": found}, ensure_ascii=False, indent=1, default=str))
    if found:
        lines = [f"{r['tf']} {r['family']}: walk-forward {r['wf']['usd_day']:+.2f}$/วัน ({r['wf']['n']} ไม้) · "
                 f"2 เดือนล่าสุด {r['holdout']['usd_day']:+.2f}$/วัน" for r in found[:5]]
        say("journal", "🎯 ล่า edge เจอตัวที่ผ่านเกณฑ์! " + " · ".join(lines)
            + " — ยังไม่ใส่ให้บอทเอง (เปลี่ยน timeframe) บอกผู้ช่วยได้ถ้าจะลองบน DEMO")
    else:
        best = rows[0] if rows else None
        say("journal", f"🧭 ล่า edge ประจำสัปดาห์เสร็จ ({(time.time() - t0) / 60:.0f} นาที): ยังไม่พบตัวที่ผ่านเกณฑ์"
            + (f" · ใกล้สุด {best['tf']} {best['family']} walk-forward {best['wf']['usd_day']:+.2f}$/วัน, "
               f"2 เดือนล่าสุด {best['holdout']['usd_day']:+.2f}$/วัน" if best else ""))
    return found
