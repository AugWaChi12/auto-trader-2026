"""
notify.py — แจ้งเตือนผ่าน Telegram (ไม่บังคับ)

เปิดใช้: ตั้ง env TELEGRAM_BOT_TOKEN และ TELEGRAM_CHAT_ID
  1) คุยกับ @BotFather ใน Telegram → /newbot → ได้ token
  2) ส่งข้อความหาบอทตัวเอง 1 ครั้ง แล้วเปิด https://api.telegram.org/bot<TOKEN>/getUpdates → ได้ chat id
ไม่ตั้ง = แค่เขียน log
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text: str) -> None:
        log.info("NOTIFY %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            body = json.dumps({"chat_id": self.chat, "text": text}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:  # แจ้งเตือนพังต้องไม่ทำให้บอทพัง
            log.warning("telegram failed: %s", e)
