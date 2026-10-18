"""
manager_ai.py — ส่วนที่สมอง AI ทุกตัวใช้ร่วมกัน (Gemini: manager_gemini.py · Claude: manager_llm.py)

ทุก MANAGER_LLM_MIN นาที: ส่งภาพรวมพอร์ต (JSON) ให้ AI → AI ตอบเป็นคำสั่งผ่าน function/tool
  pause_bot / resume_bot / close_positions / say
คำสั่งทุกข้อยังต้องผ่าน Manager.allowed() (ห้าม main · เพดานต่อชั่วโมง · ปิดได้เฉพาะไม้ที่มีอยู่)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import config as C
from .manager_learn import Learner, load_summary
from .shadow import scoreboard
from .manager import REGIME_TH, Action, Snapshot, market_regime, range_ratio, read_log, say, suits, to_m5

log = logging.getLogger("manager.ai")

LLM_EVERY_MIN = max(5, int(os.environ.get("MANAGER_LLM_MIN") or 15))
LLM_QUIET_MIN = max(LLM_EVERY_MIN, int(os.environ.get("MANAGER_LLM_QUIET_MIN") or 45))   # พอร์ตว่าง + สภาพตลาดเดิม + ไม่แกว่งแรง → ถามห่างขึ้น
TEST_BOTS = sorted((n for n in C.LIVE_INSTANCES if n != "main"), key=lambda n: int(n.split("_")[1]))

SYSTEM = f"""คุณคือผู้จัดการพอร์ตเทรดอัตโนมัติบนบัญชี DEMO (เงินทดลอง) ของ FxPro MT5
ภายใต้คุณมีบอททดสอบ {len(TEST_BOTS)} ตัว ({", ".join(TEST_BOTS)}) ด้วย lot คงที่ แต่ละตัวใช้กลยุทธ์ที่ผ่านการวิจัยแบบ walk-forward มาเอง
  เกาะ 1: m5_1–m5_9 เทรด BTCUSD (เปิด 24/7) · เกาะ 2: m5_10–m5_19 เทรดทองคำ XAUUSD และ NAS100 (ปิดเสาร์-อาทิตย์) — แต่ละบอทมี field "symbol"
  สภาพตลาดต้องดูของตลาดที่บอทนั้นเล่น (markets.<symbol>.regime) ไม่ใช่ของ BTC เสมอไป
สายกลยุทธ์: ema / donchian / momo / orb / pullback = ตามเทรนด์ · bb / rsi2 / levels = สวนกลับในกรอบ · volx = รอตลาดเงียบแล้วเบรก
บอท main (ทองคำ M15) ไม่ได้อยู่ใต้การดูแลของคุณ คุณไม่มีเครื่องมือแตะมัน

หน้าที่: อ่านภาพรวมพอร์ตกับตลาดที่ส่งมาทุก {LLM_EVERY_MIN} นาที แล้วตัดสินใจเหมือนผู้จัดการกองทุนที่คุมทีมเทรดเดอร์
- ทำนายว่าบอทไหนไม่ควรเข้าไม้ช่วงนี้ (pause_bot) เช่น สายตามเทรนด์ตอนตลาดออกข้าง สายสวนเทรนด์ตอนตลาดวิ่งแรง
  ตอนนี้เป็น "โหมดเรียนรู้": บอทไม่ถูกพักจริง เทรดต่อตามปกติ แล้วระบบให้คะแนนคำทำนายของคุณจากไม้จริง (prediction_scoreboard_30d)
  ทายถูก = ไม้ในช่วงนั้นขาดทุน · ทายผิด = ไม้ในช่วงนั้นกำไร → ใช้คะแนนนี้ปรับวิธีคิดของคุณ ทำนายเฉพาะตอนมั่นใจ
- resume_bot = ถอนคำทำนายของคุณก่อนหมดเวลา
- ปิดไม้ที่ค้างอยู่เมื่อมีเหตุผลชัดเจน (กำไรกำลังหดคืน ถือนานแล้วขาดทุน ตลาดกลับทิศแรงสวนไม้)
- อยู่เฉยๆ ก็เป็นการตัดสินใจที่ดีได้ ถ้าไม่มีเหตุผลชัด ไม่ต้องสั่งอะไร

ข้อจำกัด:
- คุณเปิดไม้ใหม่เองไม่ได้ บอทเป็นคนเปิดไม้
- นี่คือบัญชี DEMO เพื่อเรียนรู้ ไม่มีเพดานขาดทุนรายวัน — ลองตัดสินใจได้เต็มที่ ผิดก็ได้ ขอให้มีเหตุผลและเรียนรู้จากผลในแชทรอบก่อนๆ
- มีสมองตามกฎทำงานอยู่ด้วยทุกนาที: พัก/ให้กลับตามสภาพตลาด (field "regime"), ปิดไม้ที่กำไรหดคืน, ตัดไม้ขาดทุนที่ค้างนาน
  — ไม่ต้องสั่งซ้ำสิ่งที่กฎทำแล้ว แต่ถ้าเห็นต่างจากกฎ สั่ง resume/pause ทับได้ (อธิบายเหตุผล)
- การพักแต่ละครั้งยาว 15–1440 นาที
- ประวัติแชทล่าสุดแนบมาให้ อย่าสั่งกลับไปกลับมาภายในเวลาสั้นๆ
- "learned_by_strategy_and_regime" = ผลจริงของแต่ละสายในตลาดแต่ละแบบ (จำนวนไม้, $/ไม้เฉลี่ย, % ชนะ) ที่สะสมมา — ใช้เป็นหลักฐานประกอบ
  ถ้าไม้ยังน้อย (< 8) อย่าเชื่อมาก
- "prediction_scoreboard_30d" = คะแนนการตัดสินใจย้อนหลังแยกตามผู้สั่ง (rules / gemini / claude): right/wrong/calls
  missed < 0 = ถ้าเชื่อจะประหยัด, missed > 0 = ถ้าเชื่อจะพลาดกำไร — ถ้าคุณทายผิดบ่อยกว่ากฎ ให้ทำนายน้อยลง/เปลี่ยนเกณฑ์
- "lessons" = จุดอ่อนที่ชัดที่สุดจากไม้จริง (สาย × เงื่อนไข) · งานวิจัยทุกคืนจะทดสอบตัวกรองจากจุดอ่อนพวกนี้บนข้อมูลย้อนหลัง
  ถ้าผ่าน จะใส่ให้บอทเอง (bots.*.gate) — คุณไม่ต้องสั่ง แต่ช่วยอธิบายให้คนดูเข้าใจได้

ทุกรอบให้เรียก say หนึ่งครั้งเสมอ: วิเคราะห์ตลาดและพอร์ตสั้นๆ 1–3 ประโยค เป็นภาษาไทย อ่านง่ายสำหรับคนดูไลฟ์
ถ้าสั่ง pause/resume/close ให้ใส่เหตุผลสั้นๆ ที่อ้างอิงตัวเลขในข้อมูล
"""

_BOT = {"type": "string", "enum": TEST_BOTS}
FUNCTIONS = [   # JSON Schema ของแต่ละคำสั่ง — Claude/Gemini แปลงเป็นรูปแบบของตัวเอง
    {"name": "pause_bot", "description": "ทำนายว่าบอทตัวนี้ไม่ควรเข้าไม้ในอีก N นาที (โหมดเรียนรู้: บอทยังเทรดตามปกติ ระบบให้คะแนนจากไม้จริงภายหลัง)",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "minutes", "reason"],
                "properties": {"bot": _BOT, "minutes": {"type": "integer"}, "reason": {"type": "string"}}}},
    {"name": "resume_bot", "description": "ถอนคำทำนายของคุณสำหรับบอทตัวนี้ก่อนหมดเวลา",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "close_positions", "description": "ปิดไม้ทั้งหมดที่บอทตัวนี้ถืออยู่ทันทีด้วยราคาตลาด",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "say", "description": "ข้อความวิเคราะห์ที่จะขึ้นในแชทของหน้า /sandbox (ภาษาไทย สั้น กระชับ)",
     "schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}}},
]


def _report_conclusions() -> list[str]:
    from .live_report import load
    return load().get("conclusions") or []


def _bar_open(bars: list[list], now_ts: float) -> bool:
    """ตลาดนี้ยังเปิดอยู่ไหม (แท่งล่าสุดห่างจากเวลาปัจจุบันไม่เกิน 30 นาที)"""
    if not bars:
        return False
    try:
        d = datetime.fromisoformat(str(bars[-1][0]))
        t = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
        return now_ts - t <= 1800
    except ValueError:
        return True


def snapshot_json(s: Snapshot, pauses: dict[str, dict]) -> str:
    now = datetime.now(timezone.utc)
    regime, er = market_regime(s.btc_bars)
    markets = {}
    for sym in s.symbols():
        if sym == "BTCUSD":
            continue
        bars = s.bars_of(sym)
        rg, e = market_regime(bars)
        m_open = _bar_open(bars, s.t)
        markets[sym] = {"last": bars[-1][4] if bars else None, "last_bar_utc": str(bars[-1][0])[:16] if bars else None,
                        "market_open": m_open, "regime": REGIME_TH.get(rg) if rg else None, "efficiency_ratio_2h": e,
                        **({"m5_candles_t_o_h_l_c": [[str(b[0])[11:16], *b[1:]] for b in to_m5(bars, 6)]} if m_open else {})}
    gates = {b["bot"]: {k: b["params"][k] for k in ("skip_regimes", "skip_sessions") if b["params"].get(k)} or None
             for b in C.load_m5_bots()}
    bots = {}
    for n, b in sorted(s.test_bots().items()):
        p = pauses.get(n)
        brg = market_regime(s.bars_of(b.get("symbol") or "BTCUSD"))[0]
        fit = suits(b.get("family"), brg) if brg else None
        entry: dict[str, Any] = {
            "symbol": b.get("symbol"), "strategy": b.get("family"), "fits_regime": fit, "running": b["alive"],
            "today_usd": b["today"], "total_usd": b["total"], "trades": b["trades"], "win_rate": b["win_rate"],
        }
        if b.get("streak"):
            entry["loss_streak"] = b["streak"]
        if gates.get(n):
            entry["gate"] = gates[n]
        if p:
            entry["paused"] = {"minutes_left": int((p["until"] - s.t) / 60), "by": p.get("src", "rules"), "reason": p.get("reason")}
        if b["open"]:
            entry["open"] = [{"side": o["side"], "lots": o["volume"], "entry": o["open_price"], "now": o["price"],
                              "sl": o["sl"], "tp": o["tp"], "profit_usd": o["profit"],
                              "held_min": int((s.t - datetime.fromisoformat(o["open_time"]).timestamp()) / 60)} for o in b["open"]]
        bots[n] = entry
    rr = range_ratio(s.btc_bars)
    return json.dumps({
        "time_utc": now.isoformat(timespec="minutes"),
        "account": {"equity": s.equity, "balance": s.balance, "test_bots_today_usd": round(sum(b["today"] for b in s.test_bots().values()), 2)},
        "btc": {"bid": s.btc, "range15_vs_normal": round(rr, 2) if rr else None,
                "regime": REGIME_TH.get(regime) if regime else None, "efficiency_ratio_2h": er,
                "m5_candles_t_o_h_l_c": [[str(b[0])[11:16], *b[1:]] for b in to_m5(s.btc_bars, 8)]},
        "markets": markets,
        "bots": bots,
        "learned_by_strategy_and_regime": load_summary(),
        "prediction_scoreboard_30d": scoreboard(),
        "lessons": Learner().lessons(),
        "research_report_conclusions": _report_conclusions(),
        "recent_chat": [f'{m["t"][11:16]} {m["kind"]}: {m["text"]}' for m in read_log(8)],
    }, ensure_ascii=False, separators=(",", ":"), default=str)


def to_actions(calls: list[tuple[str, dict[str, Any]]], src: str) -> list[Action]:
    """(ชื่อคำสั่ง, อาร์กิวเมนต์) จาก AI → Action · ตัดคำสั่งที่ชื่อบอทไม่ถูก/ไม่ครบทิ้ง"""
    acts: list[Action] = []
    for name, x in calls:
        x = x if isinstance(x, dict) else {}
        why = str(x.get("reason", ""))[:300]
        if name == "say" and x.get("text"):
            acts.append(Action("say", reason=str(x["text"])[:600], src=src))
        elif x.get("bot") not in TEST_BOTS:
            continue
        elif name == "pause_bot":
            try:
                minutes = int(x.get("minutes") or 15)
            except (TypeError, ValueError):
                minutes = 15
            acts.append(Action("pause", x["bot"], max(15, minutes), why, src))
        elif name == "resume_bot":
            acts.append(Action("resume", x["bot"], reason=why, src=src))
        elif name == "close_positions":
            acts.append(Action("close", x["bot"], reason=why, src=src))
    return acts


class AIBrain:
    """ถาม AI ทุก LLM_EVERY_MIN นาที (เมื่อมีไม้เปิด/สภาพตลาดเปลี่ยน/แกว่งแรง) หรือ LLM_QUIET_MIN นาทีเมื่อพอร์ตว่างและตลาดนิ่ง
    ติดต่อไม่ได้ → แจ้งในแชท (ชั่วโมงละครั้ง) แล้วใช้สมองตามกฎต่อ"""
    name = "AI"
    src = "ai"

    def __init__(self) -> None:
        self.last = 0.0
        self.last_error = 0.0
        self.last_regimes: dict[str, str | None] = {}

    def _interval_min(self, s: Snapshot) -> int:
        if any(b.get("open") for b in s.test_bots().values()):
            return LLM_EVERY_MIN
        regimes = {sym: market_regime(s.bars_of(sym))[0] for sym in set(s.symbols()) | {"BTCUSD"}}
        last_rg = getattr(self, "last_regimes", None)
        if last_rg and regimes != last_rg:
            return LLM_EVERY_MIN
        rr = range_ratio(s.btc_bars)
        if rr is not None and rr >= 2.2:
            return LLM_EVERY_MIN
        return LLM_QUIET_MIN

    def maybe_decide(self, s: Snapshot, pauses: dict[str, dict]) -> list[Action]:
        if self.last and s.t - self.last < self._interval_min(s) * 60:
            return []
        self.last = s.t
        self.last_regimes = {sym: market_regime(s.bars_of(sym))[0] for sym in set(s.symbols()) | {"BTCUSD"}}
        try:
            return to_actions(self.ask("ภาพรวมพอร์ตตอนนี้:\n" + snapshot_json(s, pauses)), self.src)
        except Exception as e:  # noqa: BLE001 — AI ล่มต้องไม่ทำให้ผู้จัดการหยุด
            log.warning("%s: %s", self.name, e)
            if s.t - self.last_error > 3600:
                self.last_error = s.t
                say("alert", f"สมอง {self.name} ติดต่อไม่ได้ ({type(e).__name__}) — ใช้สมองตามกฎต่อไปก่อน", src="rules")
            return []

    def ask(self, prompt: str) -> list[tuple[str, dict[str, Any]]]:
        raise NotImplementedError

    def write(self, prompt: str) -> str:
        """ข้อความธรรมดา (ไม่มีคำสั่ง) — ใช้เขียนบันทึกท้ายวัน"""
        raise NotImplementedError
