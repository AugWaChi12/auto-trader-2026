"""
shadow.py — ให้คะแนนการตัดสินใจของผู้จัดการพอร์ต (state/manager_episodes.jsonl)

สองแบบ:
  พักจริง (MANAGER_PAUSE=1)  : บอทหยุดไปช่วงหนึ่ง → "บอทเงา" รันกลยุทธ์เดิมแบบจำลองบนราคาจริงช่วงนั้น
  คำทำนาย (ค่าเริ่มต้น)        : ผู้จัดการทำนายว่า "บอทนี้ไม่ควรเข้าไม้ช่วงนี้" แต่บอทยังเทรด → ดูไม้จริงที่เปิดในช่วงนั้น
ทั้งสองแบบได้ missed = กำไร/ขาดทุนของไม้ที่ (จะ) เปิดในช่วงนั้น
  missed < 0 → การตัดสินใจถูก (เชื่อแล้วประหยัด) · missed > 0 → ผิด (เชื่อแล้วพลาดกำไร) · ไม่มีไม้ = ไม่นับ
สรุปแยกตามผู้สั่ง (กฎ / Gemini / Claude) → แชท · หน้า 📈 ผลงาน · ส่งให้ AI ปรับตัว
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config as C

log = logging.getLogger("shadow")
EPISODES_PATH = C.STATE_DIR / "manager_episodes.jsonl"
SETTLE_MIN = 10            # พักจริง: รอให้ไม้จำลองช่วงท้ายมีเวลาปิด
PAPER_SETTLE_MIN = 60      # คำทำนาย: รอไม้จริงที่เปิดช่วงท้ายปิดก่อนนับ
MAX_BARS = 100_000         # เพดานแท่งที่ MT5 ส่งให้ต่อครั้ง (M1 ≈ 69 วัน)
MIN_WARMUP_BARS = 20_000   # แท่งก่อนเริ่มพักขั้นต่ำ ให้อินดิเคเตอร์พร้อมเหมือนบอทจริง
WHO = {"guard": "ตัวเฝ้าไม้", "rules": "กฎ", "gemini": "Gemini Lite", "gemini_smart": "Gemini 3.8", "claude": "Claude Sonnet", "claude_smart": "Claude Opus"}


def _load() -> list[dict]:
    try:
        return [json.loads(x) for x in EPISODES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _save(eps: list[dict]) -> None:
    from .live.fileio import atomic_write
    atomic_write(EPISODES_PATH, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eps[-3000:]))


def record(ep: dict) -> None:
    eps = _load()
    eps.append({"done": False, "paper": False, "tag": "", "reason": "", **ep})
    _save(eps)


def record_pause(bot: str, pause: dict, end: float) -> None:
    """การพักจริงจบแล้ว → รอบอทเงาประเมิน"""
    start = pause.get("since")
    if start and end - start >= 60:
        record({"bot": bot, "start": start, "end": end, "src": pause.get("src") or "rules", "tag": pause.get("tag") or "",
                "reason": pause.get("reason", "")})


def open_paper(bot: str, src: str, now: float) -> bool:
    return any(e["paper"] and not e["done"] and e["bot"] == bot and e["src"] == src and e["end"] > now for e in _load())


def end_paper(bot: str, src: str, now: float) -> bool:
    eps, hit = _load(), False
    for e in eps:
        if e["paper"] and not e["done"] and e["bot"] == bot and e["src"] == src and e["end"] > now:
            e["end"], hit = now, True
    if hit:
        _save(eps)
    return hit


def symbol_of(bot: str) -> str:
    if bot == "main":
        return "XAUUSD"
    return next((b["symbol"] for b in C.load_m5_bots() if b["bot"] == bot), "BTCUSD")


def simulate(bot: str, bars: list, spec: Any, start: float, end: float) -> tuple[float, int]:
    """รันกลยุทธ์ของบอทนี้บนแท่งจริงของตลาดที่บอทเทรด → (กำไร/ขาดทุนรวม, จำนวนไม้) ของไม้ที่เปิดในช่วง [start, end)"""
    from .backtest.runner import run_backtest
    inst = C.LIVE_INSTANCES[bot]
    sym = symbol_of(bot)
    strategies = C.build_strategies([bot], [sym], inst["tf"])
    if not strategies:
        return 0.0, 0
    res = run_backtest({sym: bars}, {sym: spec}, strategies, C.RISK_PROFILES[inst["profile"]], inst["tf"],
                       1_000_000.0)   # ทุนใหญ่เหมือนงานวิจัย: ไม่ให้เพดานบัญชี $600 ตัดการเทรดจำลองก่อนถึงช่วงที่วัด
    picked = [t for t in res.trades if start <= t.open_time.timestamp() < end]
    return round(sum(t.pnl for t in picked), 2), len(picked)


def actual(rows: list[dict], start: float, end: float) -> tuple[float, int]:
    """ไม้จริงที่เปิดในช่วง [start, end) และปิดแล้ว"""
    picked = [r for r in rows if start <= datetime.fromisoformat(r["open_time"]).timestamp() < end]
    return round(sum(r["profit"] for r in picked), 2), len(picked)


FETCH_GIVE_UP_SEC = 2 * 86400   # ดึงแท่งไม่ได้นานเกินนี้ → เลิกรอ จดว่าข้าม


def evaluate(fetch_bars: Callable[[str, str, int], list], spec: Any, now: float | None = None,
             trades_of: Callable[[str], list[dict]] | None = None) -> list[dict]:
    """ประเมินทุกอันที่ถึงเวลาแล้ว · fetch_bars(symbol, tf, count) เรียกครั้งเดียวต่อ (ตลาด, timeframe)
    spec = spec_of(symbol) หรือ spec ตัวเดียว · trades_of(bot) = ไม้จริงที่ปิดแล้ว"""
    now = now or time.time()
    eps = _load()
    todo = [e for e in eps if not e["done"] and e["bot"] in C.LIVE_INSTANCES
            and now - e["end"] > (PAPER_SETTLE_MIN if e["paper"] else SETTLE_MIN) * 60]
    if not todo:
        return []
    cache: dict[tuple, list | None] = {}
    out = []
    for e in todo:
        # ดึงข้อมูลพัง (MT5 หลุดชั่วคราว) → ข้ามเฉพาะรายการนั้นไว้รอบหน้า (ไม่ลากรายการอื่นพังไปด้วย) · จำลองพัง → ข้ามถาวร
        if e["paper"]:
            if trades_of is None:
                continue
            try:
                rows = trades_of(e["bot"])
            except Exception:  # noqa: BLE001
                log.exception("shadow trades %s", e["bot"])
                continue
        else:
            sym, tf = symbol_of(e["bot"]), C.LIVE_INSTANCES[e["bot"]]["tf"]
            if (sym, tf) not in cache:
                try:
                    try:
                        cache[(sym, tf)] = fetch_bars(sym, tf, MAX_BARS)
                    except TypeError:
                        cache[(sym, tf)] = fetch_bars(tf, MAX_BARS)  # type: ignore[call-arg]
                except Exception as ex:  # noqa: BLE001
                    log.warning("shadow: ดึงแท่ง %s %s ไม่ได้ (%s) — รอรอบหน้า", sym, tf, ex)
                    cache[(sym, tf)] = None
            bars = cache[(sym, tf)]
            if bars is None:
                if now - e["end"] > FETCH_GIVE_UP_SEC:
                    e.update(done=True, skipped="ดึงแท่งไม่ได้เกิน 2 วัน")
                continue
        try:
            if e["paper"]:
                missed, n = actual(rows, e["start"], e["end"])
            else:
                if sum(1 for b in bars if b.time.timestamp() < e["start"]) < MIN_WARMUP_BARS:
                    e.update(done=True, skipped="แท่งย้อนหลังไม่พอ")
                    continue
                missed, n = simulate(e["bot"], bars, spec(sym) if callable(spec) else spec, e["start"], e["end"])
        except Exception as ex:  # noqa: BLE001
            log.exception("shadow %s", e["bot"])
            e.update(done=True, skipped=str(ex)[:200])
            continue
        e.update(done=True, missed=missed, trades=n)
        out.append(e)
    _save(eps)
    return out


def scoreboard(days: float = 30) -> dict[str, dict]:
    """ผู้สั่ง → {calls, right, wrong, trades, missed} ใน N วันล่าสุด (นับเฉพาะครั้งที่มีไม้)"""
    since = time.time() - days * 86400
    sb: dict[str, dict] = {}
    for e in _load():
        if e.get("done") and e.get("trades") and e["end"] >= since:
            x = sb.setdefault(e["src"], {"calls": 0, "right": 0, "wrong": 0, "trades": 0, "missed": 0.0})
            x["calls"] += 1
            x["right" if e["missed"] < 0 else "wrong"] += 1
            x["trades"] += e["trades"]
            x["missed"] = round(x["missed"] + e["missed"], 2)
    return sb


def open_calls(now: float | None = None) -> list[dict]:
    """คำทำนายที่ยังไม่หมดเวลา (หน้าเว็บโชว์ว่าผู้จัดการกำลังคิดว่าตัวไหนไม่ควรเข้า)"""
    now = now or time.time()
    return [{k: e.get(k) for k in ("bot", "src", "end", "reason")} for e in _load()
            if e.get("paper") and not e.get("done") and e["start"] <= now < e["end"]]


def recent(limit: int = 20) -> list[dict]:
    return [{k: e.get(k) for k in ("bot", "src", "tag", "start", "end", "missed", "trades", "reason", "paper")}
            for e in _load() if e.get("done") and "missed" in e][-limit:]


def describe(e: dict) -> str:
    mins = int((e["end"] - e["start"]) / 60)
    who = WHO.get(e["src"], e["src"])
    when = datetime.fromtimestamp(e["start"], timezone.utc).strftime("%H:%M")
    if e["paper"]:
        head = f"📝 ตรวจคำทำนายของ{who}: {e['bot']} ไม่ควรเข้าไม้ {mins} นาที (เริ่ม {when} UTC) — ไม้จริง {e['trades']} ไม้ {e['missed']:+.2f}$"
        if e["trades"] == 0:
            return head + " → ช่วงนั้นไม่มีไม้ ไม่นับคะแนน"
        return head + (f" → ทายถูก ✅ (ถ้าเชื่อจะประหยัด {-e['missed']:.2f}$)" if e["missed"] < 0
                       else f" → ทายผิด ❌ (ถ้าเชื่อจะพลาดกำไร {e['missed']:.2f}$)")
    head = f"🔎 บอทเงา: {e['bot']} ที่{who}สั่งพัก {mins} นาที (เริ่ม {when} UTC) — ถ้าไม่พักจะเปิด {e['trades']} ไม้ {e['missed']:+.2f}$"
    if e["trades"] == 0:
        return head + " → ไม่ได้ไม่เสีย"
    return head + (f" → ช่วยประหยัด {-e['missed']:.2f}$ ✅" if e["missed"] < 0 else f" → พลาดกำไร {e['missed']:.2f}$ ❌")
