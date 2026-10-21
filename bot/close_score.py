"""
close_score.py — ให้คะแนนการ "สั่งปิดไม้" ของผู้จัดการ (state/manager_closes.jsonl)

ทุกครั้งที่กฎ / ตัวเฝ้าไม้ / Gemini / Claude สั่งปิดไม้ → จดราคาปิด + SL/TP ที่ไม้มีตอนนั้น
แล้วดูราคาต่อจากนั้น (แท่ง M1): ถ้าไม่ปิด ไม้จะชน SL หรือ TP ก่อน หรือถือครบ HORIZON_H ชม. แล้วได้เท่าไหร่
  saved > 0 → ปิดแล้วดีกว่าปล่อย (ประหยัด/ได้มากกว่า) · saved < 0 → ปิดแล้วพลาดกำไร
ข้อจำกัด: ไม้จริงอาจออกด้วยกฎอื่นของกลยุทธ์ (ออกตามเวลา/trail) ก่อนชน SL/TP — นี่คือการประมาณแบบ "ถือจนชน SL/TP"
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config as C

log = logging.getLogger("close_score")
PATH = C.STATE_DIR / "manager_closes.jsonl"
HORIZON_H = 24
SETTLE_MIN = 30
GIVE_UP_SEC = 3 * 86400
WHO = {"rules": "กฎ", "guard": "ตัวเฝ้าไม้", "gemini": "Gemini Lite", "gemini_smart": "Gemini 3.8", "claude": "Claude",
       "claude_smart": "Claude (แพ็กเกจ)"}


def _load() -> list[dict]:
    try:
        return [json.loads(x) for x in PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    from .live.fileio import atomic_write
    atomic_write(PATH, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-5000:]))


def record(result: dict, ctx: dict) -> None:
    """result = ผลจริงจาก MT5 (closed_result) · ctx = {src, why, sl, tp} ที่จดไว้ตอนสั่งปิด"""
    rows = _load()
    if any(r["ticket"] == result["ticket"] for r in rows):
        return
    rows.append({"ticket": result["ticket"], "bot": result.get("bot"), "symbol": result["symbol"], "side": result["side"],
                 "volume": result["volume"], "close_price": result["close_price"], "profit": result["profit"],
                 "close_t": time.time(), "sl": ctx.get("sl") or None, "tp": ctx.get("tp") or None,
                 "src": ctx.get("src", "rules"), "why": ctx.get("why", ""), "done": False})
    _save(rows)


def counterfactual(r: dict, bars: list, now: float) -> tuple[float, str] | None:
    """ราคาที่ไม้จะออก ถ้าไม่ถูกปิด → (ราคา, เหตุ) · None = ยังตัดสินไม่ได้ (รอแท่งเพิ่ม)"""
    buy = r["side"] == "BUY"
    sl, tp = r.get("sl"), r.get("tp")
    end = r["close_t"] + HORIZON_H * 3600
    last = None
    for b in bars:
        t = b.time.timestamp()
        if t < r["close_t"] - 30:
            continue
        hit_sl = sl and (b.low <= sl if buy else b.high >= sl)
        hit_tp = tp and (b.high >= tp if buy else b.low <= tp)
        if hit_sl:
            return sl, "SL"                       # แท่งเดียวโดนทั้งคู่ → นับ SL (ฝั่งร้าย)
        if hit_tp:
            return tp, "TP"
        last = b
        if t >= end:
            return b.close, f"ถือครบ {HORIZON_H} ชม."
    if now - r["close_t"] > GIVE_UP_SEC and last is not None:
        return last.close, "แท่งล่าสุด"
    return None


def evaluate(fetch_bars: Callable[[str, str, int], list], now: float | None = None) -> list[dict]:
    now = now or time.time()
    rows = _load()
    todo = [r for r in rows if not r["done"] and now - r["close_t"] >= SETTLE_MIN * 60]
    if not todo:
        return []
    cache: dict[str, Any] = {}
    out = []
    for r in todo:
        sym = r["symbol"]
        if sym not in cache:
            try:
                need = int((now - min(x["close_t"] for x in todo if x["symbol"] == sym)) / 60) + 120
                cache[sym] = fetch_bars(sym, "M1", min(max(need, 200), 99_000))
            except Exception as e:  # noqa: BLE001
                log.warning("close_score: ดึงแท่ง %s ไม่ได้ (%s)", sym, e)
                cache[sym] = None
        bars = cache[sym]
        if bars is None:
            if now - r["close_t"] > GIVE_UP_SEC:
                r.update(done=True, skipped="ดึงแท่งไม่ได้")
            continue
        cf = counterfactual(r, bars, now)
        if cf is None:
            continue
        price, how = cf
        d = 1 if r["side"] == "BUY" else -1
        contract = C.DEFAULT_SPECS[sym].contract_size if sym in C.DEFAULT_SPECS else 1.0
        delta = (price - r["close_price"]) * d * r["volume"] * contract        # ถ้าถือต่อจะได้เพิ่ม/เสียเพิ่มเท่าไหร่
        r.update(done=True, cf_price=round(price, 5), cf_how=how, cf_profit=round(r["profit"] + delta, 2), saved=round(-delta, 2))
        out.append(r)
    _save(rows)
    return out


def scoreboard(days: float = 30) -> dict[str, dict]:
    """ผู้สั่งปิด → {n, helped, hurt, saved} · saved รวม > 0 = การสั่งปิดของผู้สั่งนี้ช่วยพอร์ต"""
    since = time.time() - days * 86400
    sb: dict[str, dict] = {}
    for r in _load():
        if r.get("done") and "saved" in r and r["close_t"] >= since:
            x = sb.setdefault(r["src"], {"n": 0, "helped": 0, "hurt": 0, "saved": 0.0})
            x["n"] += 1
            x["helped"] += r["saved"] > 0
            x["hurt"] += r["saved"] < 0
            x["saved"] = round(x["saved"] + r["saved"], 2)
    return sb


def describe(r: dict) -> str:
    who = WHO.get(r["src"], r["src"])
    when = datetime.fromtimestamp(r["close_t"], timezone.utc).strftime("%H:%M")
    head = (f"🧮 ตรวจการปิดไม้ของ{who}: {r['bot']} {r['side']} (ปิด {when} UTC ได้ {r['profit']:+.2f}$) — "
            f"ถ้าไม่ปิดจะ{r['cf_how']} ได้ {r['cf_profit']:+.2f}$")
    return head + (f" → ปิดถูก ✅ (ดีกว่า {r['saved']:.2f}$)" if r["saved"] > 0 else
                   f" → ปิดเร็วไป ❌ (พลาด {-r['saved']:.2f}$)" if r["saved"] < 0 else " → เท่ากัน")
