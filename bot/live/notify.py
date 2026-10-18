"""
notify.py — แจ้งเตือนผ่าน Telegram (ไม่บังคับ)

เปิดใช้: ใส่ TELEGRAM_BOT_TOKEN ใน deploy/.env (ssh -t root@<server> /opt/auto-trader/deploy/set-key.sh TELEGRAM_BOT_TOKEN)
  แล้วส่งข้อความอะไรก็ได้หาบอทใน Telegram 1 ครั้ง → ผู้จัดการพอร์ตจำ chat นั้นเอง (state/telegram_chat.json)
  (หรือกำหนดเองด้วย TELEGRAM_CHAT_ID) · จำได้แล้วคนอื่นทักบอทก็ไม่ได้รับอะไร
ผู้จัดการพอร์ตส่ง: คำสั่ง / คำเตือน / สรุปรายวัน / ผลวิจัยประจำสัปดาห์
บอทแต่ละตัว (เข้า/ออกไม้ทุกไม้) ส่งเฉพาะเมื่อตั้ง TELEGRAM_TRADES=1 — บอท 10 ตัวบน M1 ส่งถี่มาก
ไม่ตั้ง token = แค่เขียน log
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def _chat_file() -> Path:
    from .. import config as C
    return C.STATE_DIR / "telegram_chat.json"


class Notifier:
    def __init__(self, scope: str = "trades") -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
        self.scope = scope
        self.chat = os.environ.get("TELEGRAM_CHAT_ID") or self._saved_chat()

    @staticmethod
    def _saved_chat() -> str | None:
        try:
            return str(json.loads(_chat_file().read_text(encoding="utf-8"))["chat_id"])
        except (OSError, ValueError, KeyError):
            return None

    @property
    def enabled(self) -> bool:
        if self.scope == "trades" and os.environ.get("TELEGRAM_TRADES") != "1":
            return False
        if self.chat is None and self.token:
            self.chat = self._saved_chat()          # ผู้จัดการอาจเพิ่งจำ chat ได้
        return bool(self.token and self.chat)

    def _call(self, method: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    def discover(self) -> str | None:
        """ยังไม่รู้ chat → ดูข้อความล่าสุดที่มีคนส่งหาบอท แล้วจำ chat ส่วนตัวแรกที่เจอ"""
        if not self.token or self.chat:
            return self.chat
        try:
            ups = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]}).get("result", [])
        except Exception as e:  # noqa: BLE001
            log.warning("telegram getUpdates failed: %s", e)
            return None
        for u in ups:
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id"):
                self.chat = str(chat["id"])
                p = _chat_file()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"chat_id": self.chat, "name": chat.get("first_name")}, ensure_ascii=False), encoding="utf-8")
                log.info("telegram: จำ chat %s แล้ว", chat.get("first_name"))
                self.send("✅ เชื่อมกับผู้จัดการพอร์ตแล้ว — จะส่งคำสั่ง คำเตือน สรุปรายวัน และผลวิจัยประจำสัปดาห์มาที่นี่")
                return self.chat
        return None

    def send(self, text: str) -> None:
        log.info("NOTIFY %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            self._call("sendMessage", {"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001 — แจ้งเตือนพังต้องไม่ทำให้บอทพัง
            log.warning("telegram failed: %s", e)
