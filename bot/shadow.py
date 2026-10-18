"""
shadow.py — บอทเงา: วัดว่าคำสั่ง "พัก" ของผู้จัดการพอร์ตช่วยจริงไหม

ทุกครั้งที่การพักจบ (ครบเวลา / สั่งให้กลับ) → จดช่วงเวลานั้นไว้ (state/manager_episodes.jsonl)
ชั่วโมงละครั้ง: ดึงแท่งราคาจริงย้อนหลัง → รันกลยุทธ์เดิมของบอทนั้นแบบจำลอง (backtest engine ตัวเดียวกับที่ใช้วิจัย)
  นับเฉพาะไม้ที่ "จะเปิด" ระหว่างพัก → missed = กำไร/ขาดทุนที่พลาดไป
  missed < 0 = การพักช่วยประหยัดเงิน · missed > 0 = พักแล้วพลาดกำไร
สรุปแยกตามผู้สั่ง (กฎ / Gemini / Claude) → หน้า 📈 ผลงาน · แชท · ข้อมูลให้ AI
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import config as C

log = logging.getLogger("shadow")
EPISODES_PATH = C.STATE_DIR / "manager_episodes.jsonl"
SETTLE_MIN = 10            # รอให้ไม้จำลองที่เปิดช่วงท้ายๆ มีเวลาปิดก่อนประเมิน
MAX_BARS = 100_000         # เพดานแท่งที่ MT5 ส่งให้ต่อครั้ง (M1 ≈ 69 วัน)
MIN_WARMUP_BARS = 20_000   # แท่งก่อนเริ่มพักขั้นต่ำ ให้อินดิเคเตอร์พร้อมเหมือนบอทจริง


def _load() -> list[dict]:
    try:
        return [json.loads(x) for x in EPISODES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _save(eps: list[dict]) -> None:
    from .live.fileio import atomic_write
    atomic_write(EPISODES_PATH, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eps[-2000:]))


def record(bot: str, pause: dict, end: float) -> None:
    """การพักจบแล้ว → จดไว้รอประเมิน (ต้องรู้เวลาเริ่ม)"""
    start = pause.get("since")
    if not start or end - start < 60:
        return
    eps = _load()
    eps.append({"bot": bot, "start": start, "end": end, "src": pause.get("src") or "rules", "tag": pause.get("tag") or "",
                "reason": pause.get("reason", ""), "done": False})
    _save(eps)


def simulate(bot: str, bars: list, spec: Any, start: float, end: float) -> tuple[float, int]:
    """รันกลยุทธ์ของบอทนี้บนแท่งจริง → (กำไร/ขาดทุนรวม, จำนวนไม้) ของไม้ที่เปิดในช่วง [start, end)"""
    from .backtest.runner import run_backtest
    inst = C.LIVE_INSTANCES[bot]
    strategies = C.build_strategies([bot], ["BTCUSD"], inst["tf"])
    if not strategies:
        return 0.0, 0
    res = run_backtest({"BTCUSD": bars}, {"BTCUSD": spec}, strategies, C.RISK_PROFILES[inst["profile"]], inst["tf"],
                       1_000_000.0)   # ทุนใหญ่เหมือนงานวิจัย: ไม่ให้เพดานบัญชี $600 ตัดการเทรดจำลองก่อนถึงช่วงที่วัด
    picked = [t for t in res.trades if start <= t.open_time.timestamp() < end]
    return round(sum(t.pnl for t in picked), 2), len(picked)


def evaluate(fetch_bars: Any, spec: Any, now: float | None = None) -> list[dict]:
    """ประเมินช่วงพักที่จบแล้ว · fetch_bars(tf, count) -> list[Bar] (เรียกครั้งเดียวต่อ timeframe)"""
    now = now or time.time()
    eps = _load()
    todo = [e for e in eps if not e["done"] and now - e["end"] > SETTLE_MIN * 60 and e["bot"] in C.LIVE_INSTANCES]
    if not todo:
        return []
    cache: dict[str, list] = {}
    out = []
    for e in todo:
        tf = C.LIVE_INSTANCES[e["bot"]]["tf"]
        if tf not in cache:
            cache[tf] = fetch_bars(tf, MAX_BARS)
        bars = cache[tf]
        before = sum(1 for b in bars if b.time.timestamp() < e["start"])
        if before < MIN_WARMUP_BARS:
            e.update(done=True, skipped="แท่งย้อนหลังไม่พอ")
            continue
        try:
            missed, n = simulate(e["bot"], bars, spec, e["start"], e["end"])
        except Exception as ex:  # noqa: BLE001
            log.exception("shadow %s", e["bot"])
            e.update(done=True, skipped=str(ex)[:200])
            continue
        e.update(done=True, missed=missed, trades=n)
        out.append(e)
    _save(eps)
    return out


def scoreboard(days: float = 30) -> dict[str, dict]:
    """ผู้สั่ง → {pauses, trades, missed} ใน N วันล่าสุด · saved = -missed"""
    since = time.time() - days * 86400
    sb: dict[str, dict] = {}
    for e in _load():
        if e.get("done") and "missed" in e and e["end"] >= since:
            x = sb.setdefault(e["src"], {"pauses": 0, "trades": 0, "missed": 0.0})
            x["pauses"] += 1
            x["trades"] += e["trades"]
            x["missed"] = round(x["missed"] + e["missed"], 2)
    return sb


def recent(limit: int = 20) -> list[dict]:
    return [{k: e.get(k) for k in ("bot", "src", "tag", "start", "end", "missed", "trades", "reason")}
            for e in _load() if e.get("done") and "missed" in e][-limit:]


def describe(e: dict) -> str:
    mins = int((e["end"] - e["start"]) / 60)
    who = {"rules": "กฎ", "gemini": "Gemini", "claude": "Claude"}.get(e["src"], e["src"])
    if e["trades"] == 0:
        verdict = "ช่วงนั้นกลยุทธ์ไม่เข้าไม้อยู่แล้ว — ไม่ได้ไม่เสีย"
    elif e["missed"] < 0:
        verdict = f"ช่วยประหยัด {-e['missed']:.2f}$ ✅"
    else:
        verdict = f"พลาดกำไร {e['missed']:.2f}$ ❌"
    when = datetime.fromtimestamp(e["start"], timezone.utc).strftime("%H:%M")
    return f"🔎 บอทเงา: {e['bot']} ที่{who}สั่งพัก {mins} นาที (เริ่ม {when} UTC) — ถ้าไม่พักจะเปิด {e['trades']} ไม้ {e['missed']:+.2f}$ → {verdict}"
