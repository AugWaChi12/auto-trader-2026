"""แชท Telegram: ตอบเฉพาะเจ้าของ · คำสั่งเร็ว · คำถาม → Claude (ล้ม → Gemini) · ข้ามข้อความเก่าตอนเริ่ม"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot import manager_ai as A
from bot import telegram_chat as T


@pytest.fixture()
def chat(monkeypatch, tmp_path: Path):  # noqa: ANN001, ANN201
    monkeypatch.setattr(T, "OFFSET_PATH", tmp_path / "offset.json")
    monkeypatch.setattr(A, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(A, "CHAT_DAILY_MAX", 2)
    sent, calls = [], []
    snap = NS(equity=10012.5, test_bots=lambda: {
        "main": {"today": 4.0, "open": [{"symbol": "XAUUSD", "side": "BUY", "volume": 0.04, "profit": 3.5}]},
        "m5_8": {"today": -12.0, "open": []}})
    m = NS(last_snap=snap, llm=None)
    c = T.TelegramChat(m)
    monkeypatch.setattr(T.TelegramChat, "_notifier", staticmethod(lambda: NS(token="t", chat="42")))
    monkeypatch.setattr(c, "data_pack", lambda: '{"x":1}')

    def tg(method, payload, timeout=15):  # noqa: ANN001, ANN202
        calls.append((method, payload))
        if method == "sendMessage":
            sent.append(payload["text"])
        return {"result": c.updates if method == "getUpdates" else {}}
    monkeypatch.setattr(c, "_tg", tg)
    c.updates, c.sent, c.calls = [], sent, calls
    return c


def _msg(uid: int, text: str, chat: int = 42) -> dict:
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_first_start_skips_backlog_then_answers_owner_only(chat) -> None:  # noqa: ANN001
    chat.updates = [_msg(5, "เก่า")]
    off = chat.poll_once(None)
    assert off == 6 and chat.sent == []                                   # ข้อความค้างก่อนเริ่ม → ไม่ตอบ
    chat.updates = [_msg(6, "/status"), _msg(7, "/status", chat=999)]
    off = chat.poll_once(off)
    assert off == 8 and len(chat.sent) == 1 and "equity $10,012.50" in chat.sent[0] and "main XAUUSD BUY 0.04" in chat.sent[0]
    assert json.loads(T.OFFSET_PATH.read_text())["offset"] == 8


def test_question_goes_to_claude_then_gemini_then_quota(chat) -> None:  # noqa: ANN001
    asked = []
    chat.m.llm = NS(claude=NS(answer=lambda q, d: asked.append((q, d)) or "m5_8 แย่สุดเพราะ…"),
                    write=lambda prompt, smart=False: "gemini ตอบ")
    chat.handle("บอทไหนแย่สุด")
    assert chat.sent[-1] == "m5_8 แย่สุดเพราะ…" and asked == [("บอทไหนแย่สุด", '{"x":1}')]
    assert chat.history == [("บอทไหนแย่สุด", "m5_8 แย่สุดเพราะ…")]
    chat.m.llm.claude = NS(answer=lambda q, d: (_ for _ in ()).throw(RuntimeError("limit")))
    chat.handle("แล้ววันนี้ล่ะ")
    assert chat.sent[-1] == "gemini ตอบ"
    chat.handle("อีกคำถาม")                                               # โควตาแชทวันนี้ (2) หมด
    assert "โควตา" in chat.sent[-1]


def test_long_answers_are_split_and_help_command(chat) -> None:  # noqa: ANN001
    chat.reply("ก" * 8000)
    assert [len(x) for x in chat.sent] == [3900, 3900, 200]
    chat.reply("**ตัวหนา**")
    assert chat.sent[-1] == "ตัวหนา"
    chat.handle("/help@MyBot")
    assert "/status" in chat.sent[-1]
