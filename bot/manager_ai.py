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
from .manager import REGIME_TH, Action, Snapshot, market_regime, range_ratio, read_log, say, suits, to_m5

log = logging.getLogger("manager.ai")

LLM_EVERY_MIN = max(5, int(os.environ.get("MANAGER_LLM_MIN") or 15))
TEST_BOTS = sorted((n for n in C.LIVE_INSTANCES if n != "main"), key=lambda n: int(n.split("_")[1]))

SYSTEM = f"""คุณคือผู้จัดการพอร์ตเทรดอัตโนมัติบนบัญชี DEMO (เงินทดลอง) ของ FxPro MT5
ภายใต้คุณมีบอททดสอบ {len(TEST_BOTS)} ตัว ({", ".join(TEST_BOTS)}) เทรด BTCUSD ด้วย lot คงที่ แต่ละตัวใช้กลยุทธ์ที่ผ่านการวิจัยแบบ walk-forward มาเอง
สายกลยุทธ์: ema / donchian / momo / orb / pullback = ตามเทรนด์ · bb / rsi2 / levels = สวนกลับในกรอบ · volx = รอตลาดเงียบแล้วเบรก
บอท main (ทองคำ M15) ไม่ได้อยู่ใต้การดูแลของคุณ คุณไม่มีเครื่องมือแตะมัน

หน้าที่: อ่านภาพรวมพอร์ตกับตลาดที่ส่งมาทุก {LLM_EVERY_MIN} นาที แล้วตัดสินใจเหมือนผู้จัดการกองทุนที่คุมทีมเทรดเดอร์
- พักบอทที่กลยุทธ์ไม่เข้ากับสภาพตลาดตอนนี้ (เช่น บอทตามเทรนด์ตอนตลาดออกข้าง บอทสวนเทรนด์ตอนตลาดวิ่งแรง แพ้ติดกัน)
- ให้บอทที่พักอยู่กลับมาเมื่อสภาพตลาดเหมาะกับมันอีกครั้ง
- ปิดไม้ที่ค้างอยู่เมื่อมีเหตุผลชัดเจน (กำไรกำลังหดคืน ถือนานแล้วขาดทุน ตลาดกลับทิศแรงสวนไม้)
- อยู่เฉยๆ ก็เป็นการตัดสินใจที่ดีได้ ถ้าไม่มีเหตุผลชัด ไม่ต้องสั่งอะไร

ข้อจำกัด:
- คุณเปิดไม้ใหม่เองไม่ได้ บอทเป็นคนเปิดไม้
- มีสมองตามกฎทำงานอยู่ด้วยทุกนาที: พักทั้งพอร์ตเมื่อเสียรวมวันนี้ถึง -$150, พักบอทที่เสียวันนี้ถึง -$45, พักเมื่อแพ้ติด 4 ไม้,
  และพัก/ให้กลับตามสภาพตลาด (field "regime" ในข้อมูล) — ไม่ต้องสั่งซ้ำสิ่งที่กฎทำแล้ว
  ห้ามสั่ง resume บอทที่กฎพักไว้เพราะเสียถึงเพดานรายวัน (paused.by = rules และเหตุผลพูดถึงเพดาน)
- สั่งการได้รวมไม่เกิน 20 ครั้งต่อชั่วโมง การพักแต่ละครั้งยาว 15–1440 นาที
- ประวัติแชทล่าสุดแนบมาให้ อย่าสั่งกลับไปกลับมาภายในเวลาสั้นๆ

ทุกรอบให้เรียก say หนึ่งครั้งเสมอ: วิเคราะห์ตลาดและพอร์ตสั้นๆ 1–3 ประโยค เป็นภาษาไทย อ่านง่ายสำหรับคนดูไลฟ์
ถ้าสั่ง pause/resume/close ให้ใส่เหตุผลสั้นๆ ที่อ้างอิงตัวเลขในข้อมูล
"""

_BOT = {"type": "string", "enum": TEST_BOTS}
FUNCTIONS = [   # JSON Schema ของแต่ละคำสั่ง — Claude/Gemini แปลงเป็นรูปแบบของตัวเอง
    {"name": "pause_bot", "description": "หยุดบอททดสอบตัวหนึ่งชั่วคราว (supervisor ปิดโปรเซสจนครบเวลา ไม้ที่ถืออยู่ยังมี SL/TP ที่โบรก)",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "minutes", "reason"],
                "properties": {"bot": _BOT, "minutes": {"type": "integer"}, "reason": {"type": "string"}}}},
    {"name": "resume_bot", "description": "ให้บอทที่พักอยู่กลับมาทำงานก่อนครบเวลา",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "close_positions", "description": "ปิดไม้ทั้งหมดที่บอทตัวนี้ถืออยู่ทันทีด้วยราคาตลาด",
     "schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "say", "description": "ข้อความวิเคราะห์ที่จะขึ้นในแชทของหน้า /sandbox (ภาษาไทย สั้น กระชับ)",
     "schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}}},
]


def snapshot_json(s: Snapshot, pauses: dict[str, dict]) -> str:
    now = datetime.now(timezone.utc)
    regime, er = market_regime(s.btc_bars)
    bots = {}
    for n, b in sorted(s.test_bots().items()):
        p = pauses.get(n)
        fit = suits(b.get("family"), regime) if regime else None
        bots[n] = {
            "strategy": b.get("family"), "fits_regime": fit, "running": b["alive"], "state": b.get("state"),
            "today_usd": b["today"], "total_usd": b["total"], "trades": b["trades"], "win_rate": b["win_rate"],
            "loss_streak": b["streak"], "day_target_hit": b.get("target_hit"),
            "paused": {"minutes_left": int((p["until"] - s.t) / 60), "by": p.get("src", "rules"), "reason": p.get("reason")} if p else None,
            "open": [{"side": o["side"], "lots": o["volume"], "entry": o["open_price"], "now": o["price"],
                      "sl": o["sl"], "tp": o["tp"], "profit_usd": o["profit"],
                      "held_min": int((s.t - datetime.fromisoformat(o["open_time"]).timestamp()) / 60)} for o in b["open"]],
        }
    rr = range_ratio(s.btc_bars)
    return json.dumps({
        "time_utc": now.isoformat(timespec="minutes"),
        "account": {"equity": s.equity, "balance": s.balance, "test_bots_today_usd": round(sum(b["today"] for b in s.test_bots().values()), 2)},
        "btc": {"bid": s.btc, "range15_vs_normal": round(rr, 2) if rr else None,
                "regime": REGIME_TH.get(regime) if regime else None, "efficiency_ratio_2h": er,
                "m5_candles_t_o_h_l_c": [[str(b[0])[11:16], *b[1:]] for b in to_m5(s.btc_bars)]},
        "bots": bots,
        "recent_chat": [f'{m["t"][11:16]} {m["kind"]}: {m["text"]}' for m in read_log(15)],
    }, ensure_ascii=False, default=str)


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
    """ถาม AI ทุก LLM_EVERY_MIN นาที · ติดต่อไม่ได้ → แจ้งในแชท (ชั่วโมงละครั้ง) แล้วใช้สมองตามกฎต่อ"""
    name = "AI"
    src = "ai"

    def __init__(self) -> None:
        self.last = 0.0
        self.last_error = 0.0

    def maybe_decide(self, s: Snapshot, pauses: dict[str, dict]) -> list[Action]:
        if s.t - self.last < LLM_EVERY_MIN * 60:
            return []
        self.last = s.t
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
