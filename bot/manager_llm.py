"""
manager_llm.py — สมอง Claude ของผู้จัดการพอร์ต (เปิดเมื่อมี ANTHROPIC_API_KEY · ส่วนกลางอยู่ใน manager_ai.py)
ค่าใช้จ่ายโดยประมาณ: ~$0.03–0.06 ต่อครั้ง → ทุก 15 นาที ≈ $3–6 ต่อวัน (ปรับด้วย MANAGER_LLM_MIN)
"""
from __future__ import annotations

import logging
import os
from typing import Any

import anthropic

from .manager_ai import FUNCTIONS, SYSTEM, TEST_BOTS, AIBrain  # noqa: F401 — TEST_BOTS ใช้ในเทสต์

log = logging.getLogger("manager.claude")

MODEL = os.environ.get("MANAGER_MODEL") or "claude-opus-5-5"
TOOLS = [{"name": f["name"], "description": f["description"], "strict": True, "input_schema": f["schema"]} for f in FUNCTIONS]


class ClaudeBrain(AIBrain):
    name = "Claude"
    src = "claude"

    def __init__(self) -> None:
        super().__init__()
        self.client = anthropic.Anthropic()

    def write(self, prompt: str, smart: bool = False) -> str:
        r = self.client.messages.create(model=MODEL, max_tokens=4000, output_config={"effort": "low"},
                                        system=SYSTEM, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text").strip()[:1500]

    def ask(self, prompt: str, smart: bool = False) -> list[tuple[str, dict[str, Any]]]:
        r = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": prompt}],
        )
        u = r.usage
        log.info("claude %s stop=%s in=%s cache_read=%s out=%s", r.model, r.stop_reason, u.input_tokens,
                 getattr(u, "cache_read_input_tokens", 0), u.output_tokens)
        if r.stop_reason == "refusal":
            return []
        return [(b.name, b.input) for b in r.content if b.type == "tool_use"]
