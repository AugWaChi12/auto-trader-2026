"""
manager_gemini.py — สมอง Gemini ของผู้จัดการพอร์ต (เปิดเมื่อมี GEMINI_API_KEY · ส่วนกลางอยู่ใน manager_ai.py)

คีย์ฟรีจาก aistudio.google.com มีเพดานจำนวนครั้ง · ถามทุก 15 นาที = 96 ครั้ง/วัน
เกินโควตาฟรี/ใช้ tier จ่ายเงิน: gemini-3.8-flash ≈ $0.007 ต่อครั้ง → ≈ $0.7 ต่อวัน (ราคา ต.ค. 2026)
เปลี่ยนรุ่นได้ด้วย MANAGER_MODEL (เช่น gemini-3.5-flash-lite ถูกกว่า)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from google import genai
from google.genai import types

from .manager_ai import FUNCTIONS, SYSTEM, AIBrain

log = logging.getLogger("manager.gemini")

MODEL = os.environ.get("MANAGER_MODEL") or "gemini-3.8-flash"                  # ช่วงปกติ (เช่น gemini-3.1-flash-lite ฟรี 500/วัน)
SMART_MODEL = os.environ.get("MANAGER_SMART_MODEL") or "gemini-3.8-flash"      # จังหวะสำคัญ (MANAGER_SMART_DAILY_MAX ครั้ง/วัน)
# รุ่นสำรองเมื่อรุ่นหลักล่มชั่วคราว (503/500 — Google รับโหลดไม่ไหว) · โปรเจกต์ฟรีไม่มี billing → เกินโควตาแค่ถูกปฏิเสธ ไม่เสียเงิน
FALLBACK_MODELS = [m for m in (os.environ.get("MANAGER_FALLBACK_MODELS") or "gemini-3.6-flash,gemini-3.8-flash").split(",") if m]


def _overloaded(e: Exception) -> bool:
    """ล่มชั่วคราว / รุ่นนี้ใช้ไม่ได้กับบัญชีเรา / โควตารุ่นนี้หมด → ลองรุ่นถัดไป (error อื่น เช่น 400 = ปัญหาที่คำขอ ไม่ลองต่อ)"""
    t = str(e)
    return any(x in t for x in ("503", "UNAVAILABLE", "500", "INTERNAL", "overloaded", "high demand",
                                "404", "NOT_FOUND", "429", "RESOURCE_EXHAUSTED"))


class GeminiBrain(AIBrain):
    name = "Gemini"
    src = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self.client = genai.Client()        # อ่าน GEMINI_API_KEY / GOOGLE_API_KEY จาก environment เอง
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=f["name"], description=f["description"], parameters_json_schema=f["schema"])
                for f in FUNCTIONS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),   # เรารันคำสั่งเองผ่านขีดจำกัด
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        )

    def _generate(self, first: str, prompt: str, config: Any) -> tuple[str, Any]:
        """เรียกรุ่นหลัก · ล่มชั่วคราว → ลองรุ่นสำรองตามลำดับ (ไม่ซ้ำรุ่น)"""
        models = [first] + [m for m in FALLBACK_MODELS if m != first]
        for i, model in enumerate(models):
            try:
                return model, self.client.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e:  # noqa: BLE001
                if i == len(models) - 1 or not _overloaded(e):
                    raise
                log.warning("gemini %s ล่ม (%s) → ลอง %s", model, str(e)[:80], models[i + 1])
        raise RuntimeError("unreachable")

    def write(self, prompt: str, smart: bool = False) -> str:
        _, r = self._generate(SMART_MODEL if smart else MODEL, prompt, types.GenerateContentConfig(system_instruction=SYSTEM))
        return (r.text or "").strip()[:1500]

    def ask(self, prompt: str, smart: bool = False) -> list[tuple[str, dict[str, Any]]]:
        model, r = self._generate(SMART_MODEL if smart else MODEL, prompt, self.config)
        u = r.usage_metadata
        log.info("gemini %s in=%s out=%s think=%s", model, getattr(u, "prompt_token_count", None),
                 getattr(u, "candidates_token_count", None), getattr(u, "thoughts_token_count", None))
        return [(c.name, dict(c.args or {})) for c in (r.function_calls or [])]
