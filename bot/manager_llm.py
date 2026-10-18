"""
manager_llm.py — สมอง Claude ของผู้จัดการพอร์ต (เปิดเองเมื่อมี ANTHROPIC_API_KEY ใน environment)

ทุก LLM_EVERY_MIN นาที: ส่งภาพรวมพอร์ต (JSON) ให้ Claude → Claude ตอบเป็นคำสั่งผ่าน tool
  pause_bot / resume_bot / close_positions / say
คำสั่งทุกข้อยังต้องผ่าน Manager.allowed() (ห้าม main · เพดานต่อชั่วโมง · ปิดได้เฉพาะไม้ที่มีอยู่)
ค่าใช้จ่ายโดยประมาณ: ~$0.03–0.06 ต่อครั้ง → ทุก 15 นาที ≈ $3–6 ต่อวัน (ปรับด้วย MANAGER_LLM_MIN)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic

from . import config as C
from .manager import Action, Snapshot, read_log, range_ratio, say

log = logging.getLogger("manager.llm")

MODEL = os.environ.get("MANAGER_MODEL", "claude-opus-5-5")
LLM_EVERY_MIN = max(5, int(os.environ.get("MANAGER_LLM_MIN", "15")))
TEST_BOTS = sorted((n for n in C.LIVE_INSTANCES if n != "main"), key=lambda n: int(n.split("_")[1]))

SYSTEM = f"""คุณคือผู้จัดการพอร์ตเทรดอัตโนมัติบนบัญชี DEMO (เงินทดลอง) ของ FxPro MT5
ภายใต้คุณมีบอททดสอบ {len(TEST_BOTS)} ตัว ({", ".join(TEST_BOTS)}) เทรด BTCUSD ด้วย lot คงที่ แต่ละตัวใช้กลยุทธ์ที่ผ่านการวิจัยแบบ walk-forward มาเอง
บอท main (ทองคำ M15) ไม่ได้อยู่ใต้การดูแลของคุณ คุณไม่มีเครื่องมือแตะมัน

หน้าที่: อ่านภาพรวมพอร์ตกับตลาดที่ส่งมาทุก {LLM_EVERY_MIN} นาที แล้วตัดสินใจเหมือนผู้จัดการกองทุนที่คุมทีมเทรดเดอร์
- พักบอทที่กลยุทธ์ไม่เข้ากับสภาพตลาดตอนนี้ (เช่น บอทตามเทรนด์ตอนตลาดออกข้าง บอทสวนเทรนด์ตอนตลาดวิ่งแรง แพ้ติดกัน)
- ให้บอทที่พักอยู่กลับมาเมื่อสภาพตลาดเหมาะกับมันอีกครั้ง
- ปิดไม้ที่ค้างอยู่เมื่อมีเหตุผลชัดเจน (กำไรกำลังหดคืน ถือนานแล้วขาดทุน ตลาดกลับทิศแรงสวนไม้)
- อยู่เฉยๆ ก็เป็นการตัดสินใจที่ดีได้ ถ้าไม่มีเหตุผลชัด ไม่ต้องสั่งอะไร

ข้อจำกัด:
- คุณเปิดไม้ใหม่เองไม่ได้ บอทเป็นคนเปิดไม้
- มีกฎความปลอดภัยแยกต่างหากที่ทำงานอยู่แล้ว (พักทั้งพอร์ตเมื่อเสียรวมวันนี้ถึง -$150, พักบอทที่เสียวันนี้ถึง -$45, พักเมื่อแพ้ติด 4 ไม้)
  คุณไม่ต้องสั่งซ้ำสิ่งที่กฎทำไปแล้ว และไม่ควรสั่ง resume บอทที่กฎพักไว้เพราะเสียถึงเพดานรายวัน
- สั่งการได้รวมไม่เกิน 20 ครั้งต่อชั่วโมง การพักแต่ละครั้งยาว 15–1440 นาที
- ประวัติแชทล่าสุดของคุณ (และของกฎ) แนบมาให้ อย่าสั่งกลับไปกลับมาภายในเวลาสั้นๆ

ทุกรอบให้เรียก say หนึ่งครั้งเสมอ: วิเคราะห์ตลาดและพอร์ตสั้นๆ 1–3 ประโยค เป็นภาษาไทย อ่านง่ายสำหรับคนดูไลฟ์
ถ้าสั่ง pause/resume/close ให้ใส่เหตุผลสั้นๆ ที่อ้างอิงตัวเลขในข้อมูล
"""

_BOT = {"type": "string", "enum": TEST_BOTS}
TOOLS = [
    {"name": "pause_bot", "description": "หยุดบอททดสอบตัวหนึ่งชั่วคราว (supervisor ปิดโปรเซสจนครบเวลา ไม้ที่ถืออยู่ยังมี SL/TP ที่โบรก)",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["bot", "minutes", "reason"],
                      "properties": {"bot": _BOT, "minutes": {"type": "integer"}, "reason": {"type": "string"}}}},
    {"name": "resume_bot", "description": "ให้บอทที่พักอยู่กลับมาทำงานก่อนครบเวลา",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                      "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "close_positions", "description": "ปิดไม้ทั้งหมดที่บอทตัวนี้ถืออยู่ทันทีด้วยราคาตลาด",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["bot", "reason"],
                      "properties": {"bot": _BOT, "reason": {"type": "string"}}}},
    {"name": "say", "description": "ข้อความวิเคราะห์ที่จะขึ้นในแชทของหน้า /sandbox (ภาษาไทย สั้น กระชับ)",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                      "properties": {"text": {"type": "string"}}}},
]


def _m5(bars: list[list], n: int = 36) -> list[list]:
    """แท่ง M1 → M5 ล่าสุด n แท่ง [เวลา, เปิด, สูง, ต่ำ, ปิด] (ย่อข้อมูลให้ Claude อ่านง่าย)"""
    out = []
    for i in range(len(bars) - (len(bars) % 5), 0, -5)[:n]:
        ch = bars[i - 5:i]
        if ch:
            out.append([str(ch[0][0])[11:16], ch[0][1], max(b[2] for b in ch), min(b[3] for b in ch), ch[-1][4]])
    return out[::-1]


def snapshot_json(s: Snapshot, pauses: dict[str, dict]) -> str:
    now = datetime.now(timezone.utc)
    bots = {}
    for n, b in sorted(s.test_bots().items()):
        p = pauses.get(n)
        bots[n] = {
            "strategy": b.get("family"), "running": b["alive"], "state": b.get("state"),
            "today_usd": b["today"], "total_usd": b["total"], "trades": b["trades"], "win_rate": b["win_rate"],
            "loss_streak": b["streak"], "day_target_hit": b.get("target_hit"),
            "paused": {"minutes_left": int((p["until"] - s.t) / 60), "reason": p.get("reason")} if p else None,
            "open": [{"side": o["side"], "lots": o["volume"], "entry": o["open_price"], "now": o["price"],
                      "sl": o["sl"], "tp": o["tp"], "profit_usd": o["profit"],
                      "held_min": int((s.t - datetime.fromisoformat(o["open_time"]).timestamp()) / 60)} for o in b["open"]],
        }
    rr = range_ratio(s.btc_bars)
    return json.dumps({
        "time_utc": now.isoformat(timespec="minutes"),
        "account": {"equity": s.equity, "balance": s.balance, "test_bots_today_usd": round(sum(b["today"] for b in s.test_bots().values()), 2)},
        "btc": {"bid": s.btc, "range15_vs_normal": round(rr, 2) if rr else None, "m5_candles_t_o_h_l_c": _m5(s.btc_bars)},
        "bots": bots,
        "recent_chat": [f'{m["t"][11:16]} {m["kind"]}: {m["text"]}' for m in read_log(15)],
    }, ensure_ascii=False, default=str)


class ClaudeBrain:
    name = "Claude"

    def __init__(self) -> None:
        self.client = anthropic.Anthropic()
        self.last = 0.0
        self.last_error = 0.0

    def maybe_decide(self, s: Snapshot, pauses: dict[str, dict]) -> list[Action]:
        if s.t - self.last < LLM_EVERY_MIN * 60:
            return []
        self.last = s.t
        try:
            return self.decide(s, pauses)
        except anthropic.APIError as e:
            log.warning("claude: %s", e)
            if s.t - self.last_error > 3600:            # แจ้งในแชทไม่เกินชั่วโมงละครั้ง
                self.last_error = s.t
                say("alert", f"สมอง Claude ติดต่อไม่ได้ ({type(e).__name__}) — ใช้สมองตามกฎต่อไปก่อน")
            return []

    def decide(self, s: Snapshot, pauses: dict[str, dict]) -> list[Action]:
        r = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "ภาพรวมพอร์ตตอนนี้:\n" + snapshot_json(s, pauses)}],
        )
        u = r.usage
        log.info("claude %s stop=%s in=%s cache_read=%s out=%s", r.model, r.stop_reason, u.input_tokens,
                 getattr(u, "cache_read_input_tokens", 0), u.output_tokens)
        if r.stop_reason == "refusal":
            return []
        acts: list[Action] = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            x = b.input if isinstance(b.input, dict) else {}
            why = str(x.get("reason", ""))[:300]
            if b.name == "say" and x.get("text"):
                acts.append(Action("say", reason=str(x["text"])[:600], src="claude"))
            elif b.name == "pause_bot" and x.get("bot") in TEST_BOTS:
                acts.append(Action("pause", x["bot"], max(15, int(x.get("minutes") or 15)), why, "claude"))
            elif b.name == "resume_bot" and x.get("bot") in TEST_BOTS:
                acts.append(Action("resume", x["bot"], reason=why, src="claude"))
            elif b.name == "close_positions" and x.get("bot") in TEST_BOTS:
                acts.append(Action("close", x["bot"], reason=why, src="claude"))
        return acts
