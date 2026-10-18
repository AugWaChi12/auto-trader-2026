"""ผู้จัดการพอร์ต: สมองตามกฎ · ขีดจำกัดความปลอดภัย · การพัก (supervisor) · สมอง Claude (จำลอง ไม่เรียก API จริง)"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot import manager as M


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(M, "LOG_PATH", tmp_path / "manager_log.jsonl")
    monkeypatch.setattr(M, "PAUSES_PATH", tmp_path / "manager_pauses.json")


def bot(today=0.0, streak=0, opens=(), symbol="BTCUSD") -> dict:
    return {"today": today, "closed_today": today, "total": today, "trades": 5, "win_rate": 50.0, "open": list(opens),
            "alive": True, "state": "เฝ้าราคา", "family": "ema", "symbol": symbol, "streak": streak, "target_hit": False}


def pos(ticket=1, profit=0.0, hours=0.5) -> dict:
    t = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"ticket": ticket, "profit": profit, "open_time": t, "side": "BUY", "volume": 0.04,
            "open_price": 100.0, "price": 101.0, "sl": 90.0, "tp": None}


def snap(equity=1000.0, **bots) -> M.Snapshot:
    allb = {"main": bot(), **{f"m5_{i}": bot() for i in range(1, 10)}}
    allb.update(bots)
    return M.Snapshot(M.now_ts(), equity, equity, allb)


def kinds(acts) -> list[tuple]:  # noqa: ANN001
    return [(a.kind, a.bot) for a in acts if a.kind != "say"]


def test_quiet_market_does_nothing_but_report() -> None:
    acts = M.RuleBrain().decide(snap(), {})
    assert kinds(acts) == [] and [a.kind for a in acts] == ["say"]


def test_bot_day_stop_closes_then_pauses_until_new_day() -> None:
    acts = M.RuleBrain().decide(snap(m5_3=bot(today=-50, opens=[pos(profit=-12)])), {})
    assert kinds(acts) == [("close", "m5_3"), ("pause", "m5_3")]
    assert 1 <= acts[1].minutes <= 24 * 60


def test_loss_streak_pauses_only_when_flat() -> None:
    assert ("pause", "m5_2") in kinds(M.RuleBrain().decide(snap(m5_2=bot(streak=4)), {}))
    assert ("pause", "m5_2") not in kinds(M.RuleBrain().decide(snap(m5_2=bot(streak=4, opens=[pos()])), {}))


def test_giveback_and_stale_loser_are_closed() -> None:
    b = M.RuleBrain()
    b.decide(snap(m5_1=bot(opens=[pos(7, profit=20)])), {})                      # จำยอดสูงสุด +20
    assert ("close", "m5_1") in kinds(b.decide(snap(m5_1=bot(opens=[pos(7, profit=9)])), {}))
    assert ("close", "m5_4") in kinds(M.RuleBrain().decide(snap(m5_4=bot(opens=[pos(8, profit=-25, hours=4)])), {}))


def test_portfolio_stops_pause_all_test_bots_never_main() -> None:
    acts = M.RuleBrain().decide(snap(**{f"m5_{i}": bot(today=-20) for i in range(1, 10)}), {})
    assert sorted(b for k, b in kinds(acts)) == [f"m5_{i}" for i in range(1, 10)]
    acts = M.RuleBrain().decide(snap(equity=630), {})
    assert {k for k, _ in kinds(acts)} == {"pause"} and "main" not in {b for _, b in kinds(acts)}


class FakeCol:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_bot_positions(self, bot: str) -> list[dict]:
        self.closed.append(bot)
        return [{"ticket": 1, "ok": True}]


def test_guardrails_refuse_main_unknown_and_empty_close() -> None:
    m = M.Manager(FakeCol())
    s = snap()
    assert m.allowed(M.Action("pause", "main", 30), s)
    assert m.allowed(M.Action("close", "m5_1"), s)                    # ไม่มีไม้
    assert m.allowed(M.Action("pause", "m5_99", 30), s)
    assert not m.allowed(M.Action("pause", "m5_1", 30), s)


def test_hourly_action_cap() -> None:
    m, s, p = M.Manager(FakeCol()), snap(), {}
    for i in range(M.MAX_ACTIONS_PER_HOUR + 5):
        m.execute(M.Action("pause", f"m5_{i % 9 + 1}", 30), s, p)
    assert len(m.recent) == M.MAX_ACTIONS_PER_HOUR


def test_pause_persists_resume_and_close_execute() -> None:
    col = FakeCol()
    m, s, p = M.Manager(col), snap(m5_5=bot(opens=[pos()])), {}
    m.execute(M.Action("pause", "m5_1", 99999, "test"), s, p)
    assert M.load_pauses()["m5_1"]["until"] <= s.t + M.MAX_PAUSE_MIN * 60 + 1   # จำกัดความยาวการพัก
    m.execute(M.Action("resume", "m5_1", reason="ok"), s, p)
    assert "m5_1" not in M.load_pauses()
    m.execute(M.Action("close", "m5_5", reason="x"), s, p)
    assert col.closed == ["m5_5"]
    assert [x["act"] for x in M.read_log() if x["kind"] == "action"] == ["pause", "resume", "close"]


def test_expired_pause_is_announced_and_removed(monkeypatch) -> None:  # noqa: ANN001
    M.save_pauses({"m5_2": {"until": M.now_ts() - 5, "reason": "x"}})
    m = M.Manager(FakeCol())
    monkeypatch.setattr(M, "build_snapshot", lambda col: snap())
    m.tick()
    assert M.load_pauses_raw() == {} and any("ครบเวลาพัก" in x["text"] for x in M.read_log())


def test_supervisor_stops_paused_bot_and_never_main(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import settings
    from bot.live import supervisor as SV
    monkeypatch.setattr(settings, "PATH", tmp_path / "bot_settings.json")
    sv = SV.Supervisor()
    assert "manager" in sv.children
    stopped = []
    monkeypatch.setattr(sv, "stop", lambda c, why: stopped.append(c.name))
    M.save_pauses({"m5_4": {"until": M.now_ts() + 600}, "main": {"until": M.now_ts() + 600}})
    sv.check_pauses()
    assert stopped == ["m5_4"] and not sv.enabled("m5_4") and sv.enabled("main") and sv.enabled("manager")
    M.save_pauses({})
    sv.check_pauses()
    assert sv.enabled("m5_4")


def test_manager_api_is_public(monkeypatch) -> None:  # noqa: ANN001
    from bot.monitor import server
    M.say("info", "hello")
    d = json.loads(server.manager_payload())
    assert d["messages"][-1]["text"] == "hello" and d["pauses"] == {}


def test_claude_brain_maps_tool_calls_to_actions(monkeypatch) -> None:  # noqa: ANN001
    if "anthropic" not in sys.modules:
        try:
            import anthropic  # noqa: F401
        except ImportError:                      # ไม่ได้ลง SDK บนเครื่อง dev → ใช้ตัวจำลอง
            fake = types.ModuleType("anthropic")
            fake.APIError = type("APIError", (Exception,), {})
            fake.Anthropic = lambda: None
            monkeypatch.setitem(sys.modules, "anthropic", fake)
    from bot import manager_llm as L
    blocks = [NS(type="text", text="..."),
              NS(type="tool_use", name="say", input={"text": "ตลาดออกข้าง"}),
              NS(type="tool_use", name="pause_bot", input={"bot": "m5_2", "minutes": 3, "reason": "เทรนด์ไม่มา"}),
              NS(type="tool_use", name="close_positions", input={"bot": "main", "reason": "ห้าม"})]
    resp = NS(content=blocks, stop_reason="tool_use", model=L.MODEL,
              usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))
    seen = {}
    brain = L.ClaudeBrain.__new__(L.ClaudeBrain)
    brain.last = brain.last_error = 0.0
    brain.client = NS(beta=NS(messages=NS(create=lambda **kw: seen.update(kw) or resp)))
    acts = brain.maybe_decide(snap(m5_1=bot(opens=[pos()])), {})
    assert [(a.kind, a.bot, a.src) for a in acts] == [("say", None, "claude"), ("pause", "m5_2", "claude")]
    assert acts[1].minutes == 15                                   # ขั้นต่ำ 15 นาที
    assert "main" not in L.TEST_BOTS and seen["model"] == L.MODEL
    assert json.loads(seen["messages"][0]["content"].split("\n", 1)[1])["bots"]["m5_1"]["open"]
    assert brain.maybe_decide(snap(), {}) == []                     # ยังไม่ถึงรอบถัดไป
