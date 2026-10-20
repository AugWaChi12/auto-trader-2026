"""
manager_claude_code.py — สมอง Claude ผ่าน Claude Code (claude -p) + แพ็กเกจสมาชิกของผู้ใช้ (CLAUDE_CODE_OAUTH_TOKEN)

ใช้เฉพาะ "จังหวะสำคัญ" (เพิ่งเปิด/ปิดไม้ · สภาพตลาดเปลี่ยน · แกว่งแรง) + บันทึกท้ายวัน · เพดาน MANAGER_CLAUDE_DAILY_MAX ครั้ง/วัน
  (โควตาแพ็กเกจใช้ร่วมกับตอนผู้ใช้คุยกับ Claude เอง → เรียกน้อยๆ แต่ตรงจุด)
ใช้ไม่ได้ (โควตาหมด / token หมดอายุ / timeout) → ตกไปใช้ Gemini ต่อ แล้วค่อยสมองตามกฎ — ระบบไม่หยุด
ความปลอดภัย: --tools "" (ไม่มีเครื่องมือเลย — อ่าน/เขียนไฟล์/รันคำสั่งไม่ได้) · --system-prompt ของเราแทน prompt เขียนโค้ด ·
             --json-schema บังคับรูปแบบคำตอบ · ทุกคำสั่งยังผ่าน Manager.allowed() (ห้าม main ฯลฯ)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from .manager_ai import SYSTEM, TEST_BOTS

log = logging.getLogger("manager.claude_code")

BIN = os.environ.get("MANAGER_CLAUDE_BIN") or "claude"
MODEL = os.environ.get("MANAGER_CLAUDE_MODEL") or "sonnet"
EFFORT = os.environ.get("MANAGER_CLAUDE_EFFORT") or "medium"
TIMEOUT = 240


def available() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")) and shutil.which(BIN) is not None


# ---------------------------------------------------------------- prompt (ต่อจาก SYSTEM ที่ Gemini ใช้ร่วม)
GUIDE = """
# วิธีคิด (ทำตามลำดับทุกครั้ง)
1. อ่านตลาดทีละตัวจาก markets.<symbol> / btc: regime (เทรนด์/ออกข้าง/ไม่ชัด), efficiency_ratio_2h, แท่ง M5 ล่าสุด, ตลาดเปิดหรือปิด
   — ทอง/NAS100 ปิดเสาร์-อาทิตย์ (ไม่มีแท่งใหม่ = ตลาดปิด ไม่ใช่บอทพัก)
2. ไม้ที่ถืออยู่ (bots.*.open): กำไร/ขาดทุนลอย, ระยะถึง SL/TP, ถือมานานเท่าไหร่, ทิศไม้เทียบทิศตลาดล่าสุด
   — กฎอัตโนมัติดูแลอยู่แล้วทุก 10 วินาที: ปิดเมื่อกำไรลอยรวมถึง +$50 · ปิดเมื่อกำไรเคยถึง $15 แล้วหดเหลือครึ่ง ·
     เลื่อน SL ที่โบรกไปล็อกครึ่งหนึ่งของกำไรสูงสุด · ตัดไม้ที่ถือเกิน 3 ชม. และขาดทุนเกิน $20 → ห้ามสั่งซ้ำงานเหล่านี้
3. หลักฐานที่สะสมมา: learned_by_strategy_and_regime (ผลจริงต่อสาย×สภาพตลาด), lessons (จุดอ่อนที่ชัดที่สุด),
   prediction_scoreboard_30d (คะแนนคำทำนายของแต่ละผู้ทำนาย รวมของคุณ = claude_smart), research_report_conclusions
   — ไม้น้อยกว่า ~8 ต่อช่อง = ยังเป็นเรื่องดวง อย่าเชื่อมาก
4. ตัดสินใจ (ส่วนใหญ่ "ไม่สั่งอะไร" คือคำตอบที่ถูก):
   ก) ทำนาย (pause_bot) เฉพาะเมื่อมีเหตุผลอย่างน้อย 2 ข้อจากข้อมูล เช่น สายนี้เสียเปรียบใน regime ปัจจุบันตาม learned/lessons
      + ตลาดเพิ่งเปลี่ยนสภาพ หรือ ตัวกรองของบอทนี้ (bots.*.gate) ชี้ว่าเป็นจังหวะที่ควรเลี่ยง — นาทีที่ทำนาย 30–240
      ถ้าคะแนนของคุณ (claude_smart) ต่ำกว่า 50% เมื่อมีตั้งแต่ 10 ครั้ง → ทำนายเฉพาะกรณีที่มั่นใจที่สุดเท่านั้น
   ข) ปิดไม้ (close_positions) เฉพาะเมื่อหลักฐานชัดว่าไม้กำลังผิดทางจริง: ขาดทุนลอยและตลาดเปลี่ยนสภาพสวนไม้
      หรือราคาวิ่งสวนต่อเนื่องหลายแท่งจนเหลือระยะถึง SL น้อย — อย่าปิดไม้ที่เพิ่งเปิดไม่กี่นาทีเพียงเพราะติดลบเล็กน้อย
   ค) ถอนคำทำนาย (resume_bot) เมื่อเหตุผลเดิมไม่จริงแล้ว
5. ตรวจซ้ำก่อนตอบ: ทุกตัวเลขที่อ้างต้องมาจากข้อมูลที่ให้ (ห้ามเดา/แต่งตัวเลข) · ชื่อบอทต้องอยู่ในรายการ · ไม่สั่งอะไรกับ main

# รูปแบบคำตอบ (JSON ตาม schema เท่านั้น)
- say: วิเคราะห์ภาษาไทย 1–3 ประโยคสำหรับคนดูไลฟ์ อ้างตัวเลขสำคัญ (ราคา/ER/กำไรลอย) บอกว่าตลาดไหนเป็นอย่างไร และคุณทำ/ไม่ทำอะไรเพราะอะไร
  ห้ามใช้คำว่า "พักบอท" (ไม่มีบอทถูกพักจริง) — ใช้ "ทำนายว่าไม่ควรเข้า" แทน
- actions: รายการคำสั่ง (ว่างได้) แต่ละอันมี name (pause_bot / resume_bot / close_positions), bot, minutes (เฉพาะ pause_bot), reason
  (reason สั้น อ้างข้อมูล เช่น "สาย bb ในตลาดเทรนด์ ผลจริง 12 ไม้ เฉลี่ย −4.1$/ไม้ · ER 0.52")
"""

ACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["say", "actions"],
    "properties": {
        "say": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "bot", "reason"],
            "properties": {"name": {"type": "string", "enum": ["pause_bot", "resume_bot", "close_positions"]},
                           "bot": {"type": "string", "enum": TEST_BOTS},
                           "minutes": {"type": "integer"}, "reason": {"type": "string"}}}},
    },
}


class ClaudeCode:
    name = "Claude"

    def _run(self, system: str, instruction: str, data: str, schema: dict | None = None) -> Any:
        cmd = [BIN, "-p", instruction, "--output-format", "json", "--model", MODEL, "--effort", EFFORT,
               "--tools", "", "--system-prompt", system, "--no-session-persistence", "--strict-mcp-config"]
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
        out = self._run(SYSTEM + GUIDE, "ข้อมูลพอร์ตอยู่ใน stdin (JSON) — วิเคราะห์ตามวิธีคิด แล้วตอบเป็น JSON ตาม schema",
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
                calls.append((a["name"], {k: v for k, v in a.items() if k != "name"}))
        return calls

    def write(self, prompt: str) -> str:
        out = self._run(SYSTEM, "ข้อมูลอยู่ใน stdin — เขียนตามที่ขอเป็นข้อความธรรมดา ภาษาไทย", prompt)
        return (out.get("result") or "").strip()[:1500]
