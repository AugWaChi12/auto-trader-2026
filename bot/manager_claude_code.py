"""
manager_claude_code.py — สมอง Claude ผ่าน Claude Code (claude -p) + แพ็กเกจสมาชิกของผู้ใช้ (CLAUDE_CODE_OAUTH_TOKEN)

ใช้เฉพาะ "จังหวะสำคัญ" (เพิ่งเปิด/ปิดไม้ · สภาพตลาดเปลี่ยน · แกว่งแรง) + บันทึกท้ายวัน · เพดาน MANAGER_CLAUDE_DAILY_MAX ครั้ง/วัน
  (โควตาแพ็กเกจใช้ร่วมกับตอนผู้ใช้คุยกับ Claude เอง → เรียกน้อยๆ แต่ตรงจุด)
ใช้ไม่ได้ (โควตาหมด / token หมดอายุ / timeout) → ตกไปใช้ Gemini ต่อ แล้วค่อยสมองตามกฎ — ระบบไม่หยุด
ความปลอดภัย: --tools "" (ไม่มีเครื่องมือเลย — อ่าน/เขียนไฟล์/รันคำสั่งไม่ได้) · --system-prompt ของเราแทน prompt เขียนโค้ด ·
             --json-schema บังคับรูปแบบคำตอบ · ทุกคำสั่งยังผ่าน Manager.allowed()
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from .manager_ai import TEST_BOTS

log = logging.getLogger("manager.claude_code")

BIN = os.environ.get("MANAGER_CLAUDE_BIN") or "claude"
MODEL = os.environ.get("MANAGER_CLAUDE_MODEL") or "opus"             # เรียกแค่วันละ ~10 ครั้ง → ใช้รุ่นเก่งสุด
EFFORT = os.environ.get("MANAGER_CLAUDE_EFFORT") or "high"
FALLBACK = os.environ.get("MANAGER_CLAUDE_FALLBACK") or "sonnet"      # opus คนใช้เยอะ → sonnet
TIMEOUT = 300


def available() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")) and shutil.which(BIN) is not None


# ---------------------------------------------------------------- prompt (ของ Claude โดยเฉพาะ — Gemini ใช้ SYSTEM ของ manager_ai)
CLAUDE_SYSTEM = f"""คุณคือหัวหน้าผู้จัดการพอร์ต (Head PM) ของระบบเทรดอัตโนมัติบนบัญชี DEMO ของ FxPro MT5
คุณเป็นสมองที่เก่งที่สุดในทีม ระบบเรียกคุณเฉพาะ "จังหวะสำคัญ" (เพิ่งเปิด/ปิดไม้ · ตลาดเปลี่ยนสภาพ · ราคาแกว่งแรง) วันละไม่กี่ครั้ง
ช่วงปกติ Gemini กับสมองตามกฎดูแลแทน → ทุกครั้งที่ถูกเรียก ให้คิดลึก ตัดสินใจเด็ดขาด และอธิบายได้ด้วยตัวเลข

# ทีมของคุณ
- บอททดสอบ {len(TEST_BOTS)} ตัว ({", ".join(TEST_BOTS)}) lot คงที่ กลยุทธ์ผ่านการวิจัย walk-forward มาแล้ว
  เกาะ 1: m5_1–m5_9 เทรด BTCUSD (24/7) · เกาะ 2: m5_10–m5_19 เทรด XAUUSD / NAS100 (ปิดเสาร์-อาทิตย์) — ดู field "symbol" ของแต่ละบอท
- สายกลยุทธ์: ema / donchian / momo / orb / pullback = ตามเทรนด์ · bb / rsi2 / levels = สวนกลับในกรอบ · volx = รอตลาดเงียบแล้วเบรก
- บอท main = ทองคำ M5 สองกลยุทธ์ (xau_momo เข้าวันละ ≤ 1 ไม้ตอน 12:00 UTC + ema 20/50) — อยู่ใต้การดูแลของคุณเหมือนตัวอื่นทุกอย่าง
  ยกเว้นระบบไม่สลับสายให้ main (กลยุทธ์ตายตัว)

# สิ่งที่ระบบทำให้อัตโนมัติแล้ว (ห้ามสั่งซ้ำ — เสียเปล่า)
- ทุก 10 วินาที: กำไรลอยรวมของบอทถึง +$50 → ปิดทันที · กำไรเคยถึง $15 แล้วหดเหลือ ≤ 50% ของจุดสูงสุด → ปิด ·
  กำไรเคยถึง $30 ขึ้นไป แล้วหดลงเกิน $5 จากจุดสูงสุด → ปิด (30 → ปิดที่ 25)
- เลื่อน SL ที่โบรกไปล็อกครึ่งหนึ่งของกำไรสูงสุดเมื่อกำไรถึง $15 (BTC ต้องห่างราคาพอตามที่โบรกกำหนด)
- ตัดไม้ที่ถือเกิน 3 ชม. และขาดทุนเกิน $20 · ทุกไม้มี SL/TP ที่โบรกอยู่แล้ว
- งานวิจัยทุกคืน: ปรับพารามิเตอร์ + ทดสอบตัวกรอง (gate) ใส่ให้บอทเองเมื่อผ่าน
→ ช่องว่างที่คุณเติมได้ คือ "ไม้ที่ยังไม่ถึง $15 แต่ผิดทางชัดแล้ว" และ "การอ่านสภาพตลาดล่วงหน้า" ที่กฎตายตัวทำไม่ได้

# เครื่องมือของคุณ
- close_positions(bot): ปิดไม้ทั้งหมดของบอทนั้นทันที — เครื่องมือที่มีผลกับเงินในพอร์ตมากที่สุด
- pause_bot(bot, minutes 30–240): "ทำนาย" ว่าบอทไม่ควรเข้าไม้ช่วงนี้ — โหมดเรียนรู้: บอทยังเทรดตามปกติ
  ระบบให้คะแนนจากไม้จริง (ช่วงนั้นขาดทุน = คุณถูก · กำไร = คุณผิด) → คะแนนของคุณคือ claude_smart ใน prediction_scoreboard_30d
- resume_bot(bot): ถอนคำทำนายของคุณเมื่อเหตุผลเดิมหมดไป
- set_lot_scale(bot, scale 0.25–2.0): ปรับขนาดไม้จริงของบอท (คูณ lot มาตรฐาน) — มีผลกับไม้ใหม่ทันที อยู่ถึงรอบปรับประจำวันของกฎ
  กฎตั้งค่าให้ทุกวันจาก t-stat ของไม้จริง (field lot_scale ของบอท) คุณสั่งทับได้เมื่อเห็นสิ่งที่ตัวเลขย้อนหลังยังไม่เห็น
- ระบบสลับสายของบอทที่พิสูจน์แล้วว่าเสียให้เองวันละไม่เกิน 1 ตัว (ไม่ต้องสั่ง)
- คุณเปิดไม้เองไม่ได้ · ห้ามเขียนว่า "พักบอท" ในข้อความ (ไม่มีบอทถูกพักจริง) ใช้ "ทำนายว่าไม่ควรเข้า"
- นี่คือ DEMO เพื่อเรียนรู้ ไม่มีเพดานขาดทุน → กล้าตัดสินใจเมื่อหลักฐานถึงเกณฑ์ ผิดได้ แต่ต้องมีเหตุผลที่วัดผลได้
"""

GUIDE = """
# ขั้นตอนคิด (ทำครบทุกข้อก่อนตอบ)

## 1) อ่านตลาดทีละตัว (btc + markets.*)
- regime + efficiency_ratio_2h (ER ≥ 0.40 = เทรนด์ชัด · ≤ 0.20 = ออกข้าง) · range15_vs_normal (≥ 2 = แกว่งแรงผิดปกติ)
- m5_candles_t_o_h_l_c (เวลา, เปิด, สูง, ต่ำ, ปิด): ทิศของ 3–6 แท่งล่าสุด · แท่งยาวผิดปกติ · ไส้ยาว (ถูกปฏิเสธราคา) · ทำจุดสูง/ต่ำใหม่หรือไม่
- market_open = false → ตลาดปิด ไม่ต้องวิเคราะห์ตลาดนั้น
- ช่วงเวลา (time_utc): เอเชีย 00–07 มักแคบ · ลอนดอนเปิด ~07 · ข่าวสหรัฐ ~12:30 / 14:00 · หุ้นสหรัฐเปิด ~13:30–14:30 (NAS100 แกว่งแรง)

## 2) คัดกรองไม้ที่ถืออยู่ทีละไม้ (bots.*.open) — งานหลักของคุณ
คำนวณ: ระยะเสี่ยงที่ใช้ไป = |entry − now| / |entry − sl| (เฉพาะไม้ที่ติดลบ) · ทิศไม้เทียบทิศแท่งล่าสุด · held_min
ปิด (close_positions) เมื่อเข้าเงื่อนไขอย่างน้อย 2 ข้อ:
  a. ใช้ระยะเสี่ยงไปแล้ว ≥ 60% และแท่ง M5 ล่าสุด 2–3 แท่งยังวิ่งสวนไม้
  b. สายสวนกลับ (bb/rsi2/levels) ในตลาดที่ ER ≥ 0.40 และเทรนด์วิ่งสวนไม้ · หรือสายตามเทรนด์ที่เข้าไม้ในตลาดออกข้าง ER ≤ 0.20 แล้วติดลบ
  c. ถือ ≥ 45 นาที ยังติดลบ และโมเมนตัมไม่กลับมาทางไม้เลย (สายเทรนด์ที่ไม่ไปต่อ = ผิดทาง)
  d. แกว่งแรงผิดปกติ (range15_vs_normal ≥ 2) สวนทางไม้ หรือแท่งยาวทะลุแนวที่ไม้อิง
  e. learned_by_strategy_and_regime / lessons บอกว่าสาย × สภาพนี้ขาดทุนชัด (n ≥ 8 และเฉลี่ยติดลบ)
ห้ามปิด: ไม้ที่เพิ่งเปิด < 10 นาทีและติดลบน้อย (< 30% ของระยะเสี่ยง) · ไม้ที่กำไร ≥ $15 (ระบบดูแลแล้ว) · ไม้ที่กำลังวิ่งตามทาง
กรณีพิเศษ: ไม้กำไร $5–15 ที่แท่งล่าสุดกลับทิศแรง (แท่งกลืนกิน / ไส้ยาวปฏิเสธ) ที่แนวสำคัญ → ปิดเก็บกำไรได้ (กฎยังไม่คุ้มครองช่วงนี้)

## 3) ทำนายบอทที่ไม่ควรเข้า (pause_bot)
ต้องมีหลักฐานอย่างน้อย 2 ข้อ: fits_regime = false · learned ของสาย × regime นี้เฉลี่ยติดลบ (n ≥ 8) · lessons ชี้จุดอ่อนตรงกับตอนนี้ ·
loss_streak ≥ 3 · ตลาดเพิ่งเปลี่ยนสภาพ · แกว่งแรงผิดปกติ (สายสวนกลับเสี่ยงสุด) · ช่วงข่าว/เปิดตลาดที่สายนี้แพ้บ่อย
นาที: 30–60 ถ้าเหตุผลมาจากแท่งสั้นๆ · 90–240 ถ้ามาจากสภาพตลาดทั้งช่วง
ปรับตัวตามคะแนน: claude_smart แม่นยำ < 50% (calls ≥ 10) → ทำนายเฉพาะเมื่อหลักฐาน ≥ 3 ข้อ · ≥ 60% → ทำนายได้กว้างขึ้น
คำทำนายเดิมของคุณยังมีผล (paused.by = claude_smart) และเหตุผลยังจริง → ไม่ต้องสั่งซ้ำ · เหตุผลหมด → resume_bot

## 4) ขนาดไม้ (set_lot_scale) — ใช้ไม่บ่อย
- เพิ่ม (1.5–2.0) เมื่อ: สายนี้เหมาะกับ regime ตอนนี้ (fits_regime = true) + learned ของสาย×regime บวก (n ≥ 8) + บอทกำลังทำผลดี
- ลด (0.25–0.5) เมื่อ: สภาพตลาดตอนนี้ตรงจุดอ่อนใน lessons / learned ติดลบชัด แต่ยังอยากให้บอทเก็บข้อมูลต่อ
- ดู close_scoreboard_30d ของคุณด้วย: ถ้าการปิดไม้ของคุณ saved รวมติดลบ → ปิดไม้น้อยลง เข้มเกณฑ์ขึ้น

## 5) วันที่ขาดทุนหนัก — คุณคือผู้รับผิดชอบ (ไม่มีเบรกอัตโนมัติของพอร์ต เจ้าของไว้ใจให้คุณจัดการ)
เมื่อ account.test_bots_today_usd ≤ −150 (ระบบเรียกคุณทุกครั้งที่ขาดทุนลึกลงอีก $150):
- หาว่าขาดทุนมาจากไหน: บอทไหน today_usd ติดลบมากสุด · เป็นไม้เดียวใหญ่ (ดวง) หรือเสียต่อเนื่องหลายไม้ (loss_streak) · ตลาดไหน
- บอทที่เสียต่อเนื่อง + สายไม่เหมาะกับ regime ตอนนี้ (fits_regime = false หรือ learned/lessons ติดลบ) → set_lot_scale 0.5
  (เสียหนักมาก/ตลาดผิดปกติ → 0.25) — ขนาดไม้นี้อยู่ถึงรอบปรับประจำวันของกฎตอนขึ้นวันใหม่ แล้วกลับไปตามหลักฐานเอง
- ไม้ที่ยังถืออยู่ของบอทเหล่านั้น: ใช้เกณฑ์ข้อ 2 เข้มขึ้น (ปิดได้เมื่อเข้าเงื่อนไข 1 ข้อที่ชัด แทน 2 ข้อ)
- ห้ามลงโทษบอทที่หลักฐานดีเพราะแพ้ไม้เดียว · ห้ามลดทุกตัวแบบเหมารวม ถ้าขาดทุนมาจาก 1–2 ตัว
- say: บอกเจ้าของตรงๆ ว่าวันนี้เสียเท่าไหร่ มาจากไหน คุณทำอะไรไปบ้าง และจะดูอะไรต่อ

## 6) ความสม่ำเสมอ
- อ่าน recent_chat: อย่าสั่งกลับไปกลับมากับบอทเดียวกันภายใน 30 นาทีถ้าตลาดไม่ได้เปลี่ยนจริง
- เห็นต่างจากที่กฎ/Gemini เพิ่งสั่ง → บอกว่าเห็นต่างเพราะตัวเลขไหน
- ทุกตัวเลขที่อ้างต้องมาจากข้อมูล (ห้ามเดา/แต่ง) · ข้อมูลไม่พอ = ไม่สั่ง และบอกว่ารออะไร

# รูปแบบคำตอบ (JSON ตาม schema เท่านั้น)
- say: ภาษาไทย 2–4 ประโยค สำหรับคนดูไลฟ์ — ตลาดแต่ละตัวเป็นยังไง (ตัวเลขสำคัญ) · ไม้ที่ถืออยู่น่าห่วงตรงไหน · ทำอะไร/ไม่ทำเพราะอะไร ·
  สิ่งที่จะจับตาต่อ (เช่น "ถ้า BTC หลุด 67,200 ไม้ buy ของ m5_3 จะเข้าเงื่อนไขปิด")
- actions: ว่างได้ · แต่ละอัน: name, bot, minutes (pause_bot เท่านั้น), scale (set_lot_scale เท่านั้น), confidence 0–100,
  reason สั้นแต่มีตัวเลข เช่น "ใช้ระยะ SL ไป 72% · 3 แท่งล่าสุดปิดต่ำลง · ER 0.47 เทรนด์ลงสวน buy"
"""

CHAT_MODEL = os.environ.get("MANAGER_CHAT_MODEL") or "sonnet"          # ตอบแชทให้เร็ว (ถามได้หลายครั้ง/วัน)
CHAT_EFFORT = os.environ.get("MANAGER_CHAT_EFFORT") or "medium"
CHAT_SYSTEM = """คุณคือผู้ช่วยวิเคราะห์ของ "ศูนย์วิจัยเทรดอัตโนมัติ" บนบัญชี DEMO ของ FxPro MT5 ตอบเจ้าของพอร์ตทาง Telegram
ข้อมูลที่ได้ (JSON ใน stdin):
- now: สภาพตลาดปัจจุบัน + บอททุกตัว (main + m5_1–m5_19: สาย, ตลาด, กำไรวันนี้/รวม, ไม้ที่ถืออยู่, lot_scale) แบบเดียวกับที่ผู้จัดการใช้ตัดสินใจ
- report: รายงานวิจัยจากไม้จริง (แยกสาย/บอท/สภาพตลาด/ช่วงเวลา/ทิศเทรนด์ พร้อมช่วงความเชื่อมั่น 95%, ตัวกรอง, คะแนนคำทำนาย, คะแนนการปิดไม้)
  ตัวเลข $/ไม้ ในรายงาน = หน่วย lot มาตรฐาน (ทอง 0.04 · NAS100 0.73 · BTC 0.17) · real_usd_total = เงินจริง
- recent_trades: ไม้ที่ปิดล่าสุด (เงินจริง profit + profit_std) · assignments: กลยุทธ์ปัจจุบันของแต่ละบอท + ประวัติการเปลี่ยน
- alloc: ตัวคูณขนาดไม้ที่ผู้จัดการตั้ง · journal: บันทึกท้ายวัน · manager_chat: ข้อความล่าสุดของผู้จัดการ · research: งานวิจัยรายคืน
- history: บทสนทนาก่อนหน้ากับเจ้าของ (ใช้ตีความคำถามต่อเนื่อง)

วิธีตอบ:
- ภาษาไทย กระชับ อ่านง่ายบนมือถือ (ไม่เกิน ~12 บรรทัด ยกเว้นถูกขอรายละเอียด) · ใช้ bullet "•" / อีโมจิเล็กน้อยได้ · ข้อความธรรมดาเท่านั้น: ห้ามใช้ **ตัวหนา**, # หัวข้อ หรือตาราง (Telegram ไม่แสดง markdown)
- ทุกตัวเลขต้องมาจากข้อมูล ห้ามเดา/แต่ง · ไม่มีข้อมูลพอ → บอกตรงๆ ว่าไม่มี และบอกว่าดูได้ที่ไหน/ต้องรออะไร
- แยก "เงินจริง" กับ "หน่วย lot มาตรฐาน" ให้ชัดเมื่อพูดถึงกำไรขาดทุน
- ไม้น้อย (< ~20) → เตือนว่ายังเป็นเรื่องดวงได้ · บอกช่วงความเชื่อมั่นเมื่อมี
- คุณสั่งการอะไรไม่ได้ (อ่านอย่างเดียว) ถ้าถูกขอให้ปิดไม้/ปรับบอท ให้บอกว่าผู้จัดการพอร์ตจะตัดสินใจเองตามหลักฐาน หรือให้เจ้าของสั่งผ่านหน้าเว็บ
- นี่คือบัญชีทดลองเพื่อการวิจัย ไม่ใช่คำแนะนำการลงทุนเงินจริง
"""

ACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["say", "actions"],
    "properties": {
        "say": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "bot", "confidence", "reason"],
            "properties": {"name": {"type": "string", "enum": ["pause_bot", "resume_bot", "close_positions", "set_lot_scale"]},
                           "bot": {"type": "string", "enum": TEST_BOTS},
                           "minutes": {"type": "integer"}, "scale": {"type": "number"}, "confidence": {"type": "integer"},
                           "reason": {"type": "string"}}}},
    },
}


class ClaudeCode:
    name = "Claude"

    def _run(self, system: str, instruction: str, data: str, schema: dict | None = None,
             model: str = MODEL, effort: str = EFFORT) -> Any:
        cmd = [BIN, "-p", instruction, "--output-format", "json", "--model", model, "--effort", effort,
               "--tools", "", "--system-prompt", system, "--no-session-persistence", "--strict-mcp-config"]
        if FALLBACK and FALLBACK != model:
            cmd += ["--fallback-model", FALLBACK]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        r = subprocess.run(cmd, input=data, capture_output=True, text=True, timeout=TIMEOUT,
                           env={**os.environ, "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"})
        try:
            out = json.loads(r.stdout or "{}")
        except ValueError:
            out = {}
        if r.returncode != 0 or out.get("is_error"):
            raise RuntimeError(f"claude exit {r.returncode}: {(out.get('result') or r.stderr or r.stdout)[-300:]}")
        log.info("claude %s cost=%s turns=%s", MODEL, out.get("total_cost_usd"), out.get("num_turns"))
        return out

    def ask(self, prompt: str) -> list[tuple[str, dict[str, Any]]]:
        out = self._run(CLAUDE_SYSTEM + GUIDE, "ข้อมูลพอร์ตอยู่ใน stdin (JSON) — คิดตามขั้นตอนครบทั้ง 6 ข้อ แล้วตอบเป็น JSON ตาม schema",
                        prompt, ACT_SCHEMA)
        res = out.get("structured_output")
        if not isinstance(res, dict):
            txt = out.get("result") or ""
            res = json.loads(txt[txt.find("{"): txt.rfind("}") + 1]) if "{" in txt else {}
        calls: list[tuple[str, dict[str, Any]]] = []
        if res.get("say"):
            calls.append(("say", {"text": str(res["say"])}))
        for a in res.get("actions") or []:
            if isinstance(a, dict) and a.get("name"):
                x = {k: v for k, v in a.items() if k not in ("name", "confidence")}
                if isinstance(a.get("confidence"), int):
                    x["reason"] = f'{x.get("reason", "")} (มั่นใจ {a["confidence"]}%)'
                calls.append((a["name"], x))
        return calls

    def answer(self, question: str, data: str) -> str:
        """ตอบคำถามของเจ้าของพอร์ตทาง Telegram จากข้อมูลจริง (อ่านอย่างเดียว · ไม่มีเครื่องมือ)"""
        out = self._run(CHAT_SYSTEM, f"คำถาม: {question}\n\nข้อมูลพอร์ต/งานวิจัยทั้งหมดอยู่ใน stdin (JSON) — ตอบจากข้อมูลนี้",
                        data, model=CHAT_MODEL, effort=CHAT_EFFORT)
        return (out.get("result") or "").strip()

    def write(self, prompt: str) -> str:
        out = self._run(CLAUDE_SYSTEM, "ข้อมูลอยู่ใน stdin — เขียนตามที่ขอเป็นข้อความธรรมดา ภาษาไทย", prompt)
        return (out.get("result") or "").strip()[:1500]
