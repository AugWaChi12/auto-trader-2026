"""
telegram_chat.py — คุยกับศูนย์วิจัยทาง Telegram (เธรดเบื้องหลังในโปรเซสผู้จัดการ)

รับเฉพาะข้อความจาก chat ของเจ้าของ (state/telegram_chat.json / TELEGRAM_CHAT_ID) — คนอื่นทักบอทจะไม่ได้อะไรกลับ
  /status  สถานะตอนนี้ทันที (ไม่ใช้ AI): equity · กำไรวันนี้ · ไม้ที่ถืออยู่ · ขนาดไม้ที่ผู้จัดการปรับ
  /report  สรุปรายงานวิจัยล่าสุด
  /help    วิธีใช้
  ข้อความอื่น = คำถาม → รวบรวมข้อมูลจริง (สถานะพอร์ต · รายงานวิจัย · ไม้ล่าสุด · กลยุทธ์ · บันทึก) → Claude ตอบ
               (Claude ใช้ไม่ได้/โควตาหมด → Gemini · ไม่มีทั้งคู่ → แนะนำ /status)
อ่านอย่างเดียว: ตอบคำถามได้ แต่สั่งปิดไม้/ปรับบอทผ่านแชทไม่ได้
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

from . import config as C

log = logging.getLogger("telegram_chat")
OFFSET_PATH = C.STATE_DIR / "telegram_offset.json"
HISTORY = 4                   # จำคำถาม-คำตอบล่าสุดกี่คู่ (ถามต่อเนื่องได้)
POLL_SEC = 25
HELP = ("🤖 ศูนย์วิจัย — ถามอะไรก็ได้เป็นภาษาไทย เช่น\n"
        "• วันนี้เป็นไงบ้าง\n• บอทไหนแย่สุดสัปดาห์นี้ เพราะอะไร\n• สาย rsi2 ได้กำไรตอนตลาดแบบไหน\n"
        "• Claude ทายแม่นแค่ไหน · ผู้จัดการปิดไม้ช่วยจริงไหม\n\n"
        "คำสั่งเร็ว (ไม่ใช้ AI): /status สถานะตอนนี้ · /report สรุปรายงานวิจัย")


def _load_offset() -> int | None:
    try:
        return int(json.loads(OFFSET_PATH.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError):
        return None


def _save_offset(n: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": n}), encoding="utf-8")


class TelegramChat:
    def __init__(self, manager: Any) -> None:
        self.m = manager
        self.history: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ Telegram
    def _tg(self, method: str, payload: dict, timeout: float = 15) -> dict:
        n = self._notifier()
        req = urllib.request.Request(f"https://api.telegram.org/bot{n.token}/{method}", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _notifier():  # noqa: ANN205
        from .manager import notifier
        return notifier()

    def reply(self, text: str) -> None:
        n = self._notifier()
        for i in range(0, max(len(text), 1), 3900):                # Telegram รับไม่เกิน 4096 ตัวอักษรต่อข้อความ
            try:
                self._tg("sendMessage", {"chat_id": n.chat, "text": text[i:i + 3900], "disable_web_page_preview": True})
            except Exception as e:  # noqa: BLE001
                log.warning("telegram reply failed: %s", e)

    def typing(self) -> None:
        try:
            self._tg("sendChatAction", {"chat_id": self._notifier().chat, "action": "typing"})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ ข้อมูล
    def status_text(self) -> str:
        s = self.m.last_snap
        if s is None:
            return "ผู้จัดการยังไม่ได้อ่านข้อมูลรอบแรก — ลองใหม่ในอีกครู่"
        from . import allocator
        bots = s.test_bots()
        today = round(sum(b["today"] for b in bots.values()), 2)
        L = [f"💼 equity ${s.equity:,.2f} · วันนี้ (รวมลอย) {today:+.2f}$"]
        opens = [(n, o) for n, b in bots.items() for o in b.get("open") or []]
        if opens:
            L.append(f"📂 ถืออยู่ {len(opens)} ไม้:")
            L += [f"• {n} {o.get('symbol', '')} {o['side']} {o['volume']} · {o['profit']:+.2f}$" for n, o in opens[:15]]
        else:
            L.append("📂 ไม่มีไม้ที่ถืออยู่")
        top = sorted(bots.items(), key=lambda kv: kv[1]["today"], reverse=True)
        if top:
            L.append("🏆 วันนี้ดีสุด: " + ", ".join(f"{n} {b['today']:+.0f}$" for n, b in top[:3])
                     + " · แย่สุด: " + ", ".join(f"{n} {b['today']:+.0f}$" for n, b in top[::-1][:3]))
        sc = [f"{b} ×{a['scale']:g}" for b, a in sorted(allocator.summary().items()) if a.get("scale") not in (None, 1.0)]
        if sc:
            L.append("⚖️ ขนาดไม้ที่ปรับ: " + ", ".join(sc))
        return "\n".join(L)

    def data_pack(self) -> str:
        from . import allocator, dataset, live_report
        from .manager import load_pauses, read_journal, read_log
        from .manager_ai import snapshot_json
        from .research_weekly import load_status
        s = self.m.last_snap
        rows = dataset.load()[-40:]
        keep = ("bot", "symbol", "family", "side", "volume", "open_time", "close_time", "hold_min", "profit", "profit_std",
                "exit_reason", "regime", "session", "trend", "gate_flag", "prediction_by")
        try:
            doc = json.loads((C.M5_BOTS_TUNED if C.M5_BOTS_TUNED.exists() else C.M5_BOTS_FILE).read_text(encoding="utf-8"))
            assign = {b["bot"]: {k: b.get(k) for k in ("symbol", "family", "params", "plan", "wf_usd_day", "reassigned")}
                      for b in doc["bots"]}
        except (OSError, ValueError, KeyError):
            assign = {}
        assign["main"] = {"symbol": "XAUUSD", "family": C.MAIN_FAMILY, "strategies": C.LIVE_STRATEGIES, "params": C.MAIN_M5}
        rs = load_status()
        return json.dumps({
            "now": json.loads(snapshot_json(s, load_pauses())) if s is not None else None,
            "report": live_report.load(),
            "recent_trades": [{k: r.get(k) for k in keep} for r in rows],
            "assignments": assign, "alloc": allocator.summary(),
            "journal": read_journal(7), "manager_chat": [f'{m["t"][5:16]} {m["kind"]}: {m["text"]}' for m in read_log(25)],
            "research": {k: rs.get(k) for k in ("finished", "running", "summary", "error")},
            "history": [{"q": q, "a": a} for q, a in self.history[-HISTORY:]],
        }, ensure_ascii=False, separators=(",", ":"), default=str)

    # ------------------------------------------------------------------ ตอบ
    def answer(self, q: str) -> str:
        from .manager_ai import count_call, quota_left
        if quota_left(key="chat") <= 0:
            return "วันนี้ถามครบโควตาแล้ว (ตั้งด้วย MANAGER_CHAT_DAILY_MAX) — ดูสถานะทันทีได้ที่ /status"
        data = self.data_pack()
        llm = self.m.llm
        claude = getattr(llm, "claude", None)
        if claude is not None:
            try:
                count_call("chat")
                return claude.answer(q, data) or "(Claude ไม่ได้ตอบอะไรกลับมา)"
            except Exception as e:  # noqa: BLE001
                log.warning("chat claude: %s", str(e)[:200])
        if llm is not None and hasattr(llm, "write"):
            from .manager_claude_code import CHAT_SYSTEM
            try:
                count_call("chat")
                return llm.write(f"{CHAT_SYSTEM}\n\nคำถาม: {q}\n\nข้อมูล (JSON):\n{data}", smart=True)
            except Exception as e:  # noqa: BLE001
                log.warning("chat gemini: %s", str(e)[:200])
        return "ตอนนี้สมอง AI ใช้ไม่ได้ — ดูสถานะทันทีได้ที่ /status"

    def handle(self, text: str) -> None:
        t = text.strip()
        cmd = t.split()[0].split("@")[0].lower() if t.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.reply(HELP)
        elif cmd == "/status":
            self.reply(self.status_text())
        elif cmd == "/report":
            from . import live_report
            r = live_report.load()
            self.reply(live_report.telegram_summary(r) if r else "ยังไม่มีรายงาน — สร้างทุกเที่ยงคืนเวลาไทย")
        elif t:
            self.typing()
            a = self.answer(t)
            self.history = (self.history + [(t, a)])[-HISTORY:]
            self.reply(a)

    # ------------------------------------------------------------------ วนรับข้อความ
    def poll_once(self, offset: int | None) -> int | None:
        n = self._notifier()
        ups = self._tg("getUpdates", {"timeout": POLL_SEC, "allowed_updates": ["message"],
                                      **({"offset": offset} if offset is not None else {})}, timeout=POLL_SEC + 10).get("result", [])
        if offset is None and ups:                                   # เริ่มครั้งแรก: ข้ามข้อความเก่าที่ค้าง ไม่ตอบย้อนหลัง
            nxt = ups[-1]["update_id"] + 1
            _save_offset(nxt)
            return nxt
        for u in ups:
            offset = u["update_id"] + 1
            _save_offset(offset)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(n.chat) or not msg.get("text"):
                continue                                             # ไม่ใช่เจ้าของ → เงียบ
            try:
                self.handle(msg["text"])
            except Exception as e:  # noqa: BLE001
                log.exception("chat handle")
                self.reply(f"⚠️ ตอบไม่สำเร็จ: {type(e).__name__}")
        return offset

    def run(self) -> None:
        offset = _load_offset()
        while True:
            n = self._notifier()
            if not n.token or not n.chat:
                time.sleep(60)
                continue
            try:
                offset = self.poll_once(offset)
            except Exception as e:  # noqa: BLE001 — เน็ต/Telegram ล่ม → รอแล้วลองใหม่
                log.warning("telegram poll: %s", str(e)[:200])
                time.sleep(15)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="telegram-chat", daemon=True)
        th.start()
        return th
