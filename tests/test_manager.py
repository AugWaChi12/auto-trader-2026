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
    monkeypatch.setattr(M, "LOSS_STOPS", True)          # เทสต์กฎเพดานขาดทุนด้วย (บนเซิร์ฟเวอร์ปิดไว้)
    monkeypatch.setattr(M, "PAUSE_BOTS", True)          # เทสต์ชุดเดิมเป็นโหมดพักจริง (บนเซิร์ฟเวอร์ใช้โหมดทำนาย)
    monkeypatch.setattr(M, "LOG_PATH", tmp_path / "manager_log.jsonl")
    monkeypatch.setattr(M, "PAUSES_PATH", tmp_path / "manager_pauses.json")
    monkeypatch.setattr(M, "JOURNAL_PATH", tmp_path / "manager_journal.jsonl")
    monkeypatch.setattr(M, "RESEARCH_EVERY_DAYS", 0)
    from bot import manager_learn as LR
    from bot import research_weekly as RW
    from bot.live import notify as N
    monkeypatch.setattr(LR, "TAPE_PATH", tmp_path / "tape.json")
    monkeypatch.setattr(LR, "LEARN_PATH", tmp_path / "learn.json")
    monkeypatch.setattr(RW, "STATUS_PATH", tmp_path / "research.json")
    from bot import shadow as SH
    monkeypatch.setattr(SH, "EPISODES_PATH", tmp_path / "episodes.jsonl")
    monkeypatch.setattr(N, "_chat_file", lambda: tmp_path / "telegram_chat.json")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(M, "_notifier", None)


def bot(today=0.0, streak=0, opens=(), symbol="BTCUSD", family="volx") -> dict:
    return {"today": today, "closed_today": today, "total": today, "trades": 5, "win_rate": 50.0, "open": list(opens),
            "alive": True, "state": "เฝ้าราคา", "family": family, "symbol": symbol, "streak": streak, "target_hit": False}


def pos(ticket=1, profit=0.0, hours=0.5) -> dict:
    t = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"ticket": ticket, "profit": profit, "open_time": t, "side": "BUY", "volume": 0.04,
            "open_price": 100.0, "price": 101.0, "sl": 90.0, "tp": None}


def snap(equity=1000.0, bars=(), t=None, **bots) -> M.Snapshot:
    allb = {"main": bot(), **{f"m5_{i}": bot() for i in range(1, 10)}}
    allb.update(bots)
    return M.Snapshot(t or M.now_ts(), equity, equity, allb, list(bars))


def bars(kind: str, n: int = 200) -> list[list]:
    """แท่ง M1 จำลอง: trend = ขึ้นต่อเนื่อง · range = ขึ้นลงสลับในกรอบ"""
    out, p, t0 = [], 100.0, datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        step = 0.5 if kind == "trend" else (3.0 if (i // 5) % 2 == 0 else -3.0)
        o, p = p, p + step
        out.append([(t0 + timedelta(minutes=i)).isoformat(), o, max(o, p) + .1, min(o, p) - .1, p])
    return out


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


def test_loss_stops_off_by_default_and_old_stops_released(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(M, "LOSS_STOPS", False)
    acts = M.RuleBrain().decide(snap(equity=500, m5_3=bot(today=-90, streak=6)), {})
    assert kinds(acts) == []
    M.save_pauses({"m5_5": {"until": M.now_ts() + 999, "reason": "m5_5 วันนี้ -135$ ถึงเพดาน -45$"},
                   "m5_1": {"until": M.now_ts() + 999, "reason": "ไม่ถนัด", "tag": "regime"},
                   "m5_2": {"until": M.now_ts() + 999, "reason": "เพดาน", "src": "gemini"}})
    M.Manager.release_loss_stops()
    assert sorted(M.load_pauses()) == ["m5_1", "m5_2"]


def test_market_regime_reads_trend_and_range() -> None:
    assert M.market_regime(bars("trend"))[0] == "trend"
    assert M.market_regime(bars("range"))[0] == "range"
    assert M.market_regime(bars("trend", 60)) == (None, None)
    assert M.suits("bb", "trend") is False and M.suits("ema", "trend") and M.suits("volx", "trend") is None


def test_regime_pauses_mismatched_family_after_confirm_then_resumes() -> None:
    b, p = M.RuleBrain(), {}
    fams = dict(m5_1=bot(family="bb"), m5_2=bot(family="ema"), m5_3=bot(family="rsi2", opens=[pos()]))
    for _ in range(M.REGIME_CONFIRM - 1):
        assert kinds(b.decide(snap(bars=bars("trend"), **fams), p)) == []        # ยังไม่ยืนยัน
    acts = b.decide(snap(bars=bars("trend"), **fams), p)
    assert kinds(acts) == [("pause", "m5_1")]                                  # bb ไม่ถนัดเทรนด์ · ema ถนัด
    assert next(a for a in acts if a.kind == "pause").tag == "regime"
    assert ("pause", "m5_3") not in kinds(acts)                                 # ถือไม้อยู่ ไม่พัก
    assert any("สภาพตลาดเปลี่ยน" in a.reason for a in acts if a.kind == "say")
    assert kinds(b.decide(snap(bars=bars("trend"), **fams), p)) == []           # ไม่สั่งซ้ำ
    t0 = M.now_ts()
    p = {"m5_1": {"until": t0 + 9999, "since": t0 - M.REGIME_MIN_REST_MIN * 60, "tag": "regime"}}
    for _ in range(M.REGIME_CONFIRM):
        acts = b.decide(snap(bars=bars("range"), **fams), p)
    assert ("resume", "m5_1") in kinds(acts) and ("pause", "m5_2") in kinds(acts)


def test_restart_does_not_reannounce_same_regime() -> None:
    M.say("analysis", "สภาพตลาดเปลี่ยน: ตลาดวิ่งเป็นเทรนด์ (efficiency 0.5)")
    b = M.RuleBrain()
    assert b.regime == {"BTCUSD": "trend"}
    for _ in range(M.REGIME_CONFIRM):
        acts = b.decide(snap(bars=bars("trend")), {})
    assert not any("สภาพตลาดเปลี่ยน" in a.reason for a in acts)


def test_pause_records_tag_and_source() -> None:
    m, s, p = M.Manager(FakeCol()), snap(), {}
    m.execute(M.Action("pause", "m5_1", 60, "x", "gemini", "regime"), s, p)
    assert M.load_pauses()["m5_1"]["tag"] == "regime" and M.load_pauses()["m5_1"]["src"] == "gemini"


def test_gemini_brain_maps_function_calls_to_actions(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    from bot import manager_gemini as G
    calls = [NS(name="say", args={"text": "BTC วิ่งขึ้นแรง"}), NS(name="resume_bot", args={"bot": "m5_4", "reason": "เทรนด์มา"}),
             NS(name="pause_bot", args={"bot": "main", "minutes": 60, "reason": "ห้าม"})]
    seen = {}
    brain = G.GeminiBrain()
    brain.client = NS(models=NS(generate_content=lambda **kw: seen.update(kw) or NS(function_calls=calls, usage_metadata=None)))
    acts = brain.maybe_decide(snap(bars=bars("trend")), {})
    assert [(a.kind, a.bot, a.src) for a in acts] == [("say", None, "gemini"), ("resume", "m5_4", "gemini")]
    assert seen["model"] == G.MODEL and '"regime"' in seen["contents"]
    brain.client = NS(models=NS(generate_content=lambda **kw: (_ for _ in ()).throw(RuntimeError("quota"))))
    brain.last = 0
    assert brain.maybe_decide(snap(), {}) == [] and "Gemini" in M.read_log()[-1]["text"]


def test_manager_picks_brain_from_keys(monkeypatch) -> None:  # noqa: ANN001
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "MANAGER_AI"):
        monkeypatch.delenv(k, raising=False)
    assert M.Manager(FakeCol()).llm is None
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    assert M.Manager(FakeCol()).llm.src == "gemini"


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


# ---------------------------------------------------------------- เรียนรู้ / สรุปวัน / Telegram / วิจัยประจำสัปดาห์
def closed(ticket, profit) -> dict:  # noqa: ANN001
    return {"ticket": ticket, "profit": profit}


def test_learner_records_regime_at_open_and_counts_when_closed() -> None:
    from bot.manager_learn import LEARN_MIN, Learner
    L = Learner()
    for k in range(LEARN_MIN):
        L.observe({"m5_4": {"family": "bb", "open": [pos(100 + k)], "recent": []}}, "trend", .5, M.now_ts())
        L.observe({"m5_4": {"family": "bb", "open": [], "recent": [closed(100 + k, -3.0)]}}, "range", .1, M.now_ts())
    assert L.summary()["bb"]["trend"] == {"n": LEARN_MIN, "avg": -3.0, "win_rate": 0}   # นับตามสภาพตอน "เปิด" ไม้
    assert L.fit("bb", "trend") is False and L.fit("bb", "range") is None
    L.observe({"m5_4": {"family": "bb", "open": [], "recent": [closed(100, -3.0)]}}, "trend", .5, M.now_ts())
    assert Learner().summary()["bb"]["trend"]["n"] == LEARN_MIN                         # ไม่นับซ้ำ + บันทึกลงไฟล์แล้ว


def test_rule_brain_prefers_learned_results_over_prior() -> None:
    from bot.manager_learn import Learner
    L = Learner()
    L.table = {"ema": {"trend": {"n": 40, "pnl": -160.0, "wins": 8}}}             # สมมติฐานบอก ema ถนัดเทรนด์ แต่ผลจริงเสีย
    b = M.RuleBrain(L)
    for _ in range(M.REGIME_CONFIRM):
        acts = b.decide(snap(bars=bars("trend"), m5_2=bot(family="ema")), {})
    pause = [a for a in acts if a.kind == "pause" and a.bot == "m5_2"]
    assert pause and "ผลจริง 40 ไม้" in pause[0].reason


def test_journal_written_on_new_day_with_ai_note(monkeypatch) -> None:  # noqa: ANN001
    m = M.Manager(FakeCol())
    m.llm = NS(src="gemini", write=lambda prompt, smart=False: "วันนี้ตลาดออกข้าง พรุ่งนี้ระวังสายตามเทรนด์")
    M.say("action", "⏸ สั่งพัก m5_1", src="gemini")
    m.journal(M.bkk_day(), snap(m5_1=bot(today=12.5), m5_2=bot(today=-30)))
    j = M.read_journal()[-1]
    assert j["pnl"] == -17.5 and "ดีสุด m5_1" in j["text"] and "gemini 1" in j["text"] and j["ai"].startswith("วันนี้")
    assert M.read_log()[-1]["kind"] == "journal"


def test_telegram_discovers_first_private_chat_and_sends(monkeypatch) -> None:  # noqa: ANN001
    from bot.live import notify as N
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    calls = []

    def fake(self, method, payload=None):  # noqa: ANN001, ANN202
        calls.append((method, payload))
        if method == "getUpdates":
            return {"result": [{"message": {"chat": {"id": 42, "type": "private", "first_name": "A"}}}]}
        return {"ok": True}
    monkeypatch.setattr(N.Notifier, "_call", fake)
    n = N.Notifier("manager")
    assert not n.enabled and n.discover() == "42"
    assert N.Notifier("manager").chat == "42"                                     # จำไว้ในไฟล์
    assert not N.Notifier("trades").enabled                                       # ไม้รายตัวไม่ส่ง เว้นแต่ TELEGRAM_TRADES=1
    monkeypatch.setattr(M, "_notifier", n)
    M.say("action", "⏸ สั่งพัก m5_1")
    M.say("analysis", "รายงานทั่วไป")                                              # ไม่ส่ง
    sent = [p["text"] for meth, p in calls if meth == "sendMessage"]
    assert sent[-1] == "⚡ ⏸ สั่งพัก m5_1" and not any("รายงานทั่วไป" in t for t in sent)


def test_research_describe_changes() -> None:
    from bot.research_weekly import describe
    ch = [{"bot": "m5_2", "family": "ema", "old": {"params": {"fast": 9, "slow": 21}, "plan": "native"},
           "new": {"params": {"fast": 12, "slow": 21}, "plan": "ratchet"}, "wf_usd_day": -1.5}]
    t = describe(ch, 33)
    assert "m5_2 (ema): fast 9→12, วิธีออก native→ratchet" in t and "33 นาที" in t
    assert "ไม่เปลี่ยนอะไร" in describe([], 30)


def test_supervisor_restarts_only_bots_whose_research_changed(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import config as C
    from bot import settings
    from bot.live import supervisor as SV
    monkeypatch.setattr(settings, "PATH", tmp_path / "bot_settings.json")
    tuned = tmp_path / "m5_bots.json"
    monkeypatch.setattr(C, "M5_BOTS_TUNED", tuned)
    sv = SV.Supervisor()
    for c in sv.children.values():
        c.proc = NS(poll=lambda: None)
    stopped = []
    monkeypatch.setattr(sv, "stop", lambda c, why: stopped.append(c.name))
    new = [dict(b) for b in sv.m5]
    new[1] = {**new[1], "params": {**new[1]["params"], "x": 1}}
    tuned.write_text(json.dumps({"bots": new}), encoding="utf-8")
    sv.check_research()
    assert stopped == [new[1]["bot"]]


def test_lab_retune_keeps_family_per_bot(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research"))
    import m5_lab
    from bot import config as C
    cur = [{"bot": "m5_1", "family": "ema", "symbol": "BTCUSD", "lot": 0.04, "params": {"fast": 9}, "plan": "native"},
           {"bot": "m5_2", "family": "bb", "symbol": "BTCUSD", "lot": 0.04, "params": {"k": 2.0}, "plan": "trail"}]
    monkeypatch.setattr(C, "load_m5_bots", lambda: cur)
    monkeypatch.setattr(sys, "argv", ["m5_lab", "--no-gates"])
    sl = lambda fam, params, plan: {"family": fam, "symbol": "BTCUSD", "live": {"params": params, "plan": plan},  # noqa: E731
                                    "wf": {"usd_day": 1.0}, "holdout": {"usd_day": 0.5}, "wf_random": None}
    out = tmp_path / "m5_bots.json"
    ch = m5_lab.retune({}, [sl("bb", {"k": 2.0}, "trail"), sl("ema", {"fast": 12}, "ratchet")], out)
    bots = json.loads(out.read_text(encoding="utf-8"))["bots"]
    assert [b["family"] for b in bots] == ["ema", "bb"] and bots[0]["params"] == {"fast": 12}
    assert [c["bot"] for c in ch] == ["m5_1"]


def test_interrupted_research_is_not_stuck_running() -> None:
    from bot import research_weekly as RW
    RW.save_status({"started": M.now_ts() - 4 * 3600, "running": True})
    st = RW.load_status()
    assert st["running"] is False and "ขัดจังหวะ" in st["error"]



def test_shadow_records_pause_and_scores_by_source(monkeypatch) -> None:  # noqa: ANN001
    from datetime import datetime, timedelta, timezone
    from bot import shadow as SH
    from bot.core.types import Bar
    t0 = M.now_ts() - 3 * 3600
    m, p = M.Manager(FakeCol()), {}
    s = snap(t=t0)
    m.execute(M.Action("pause", "m5_4", 60, "สวนเทรนด์", "gemini", "regime"), s, p)
    m.execute(M.Action("resume", "m5_4", reason="กลับ", src="gemini"), snap(t=t0 + 3600), p)
    M.save_pauses({"m5_7": {"until": t0 + 1800, "since": t0, "reason": "x", "src": "rules"}})
    monkeypatch.setattr(M, "build_snapshot", lambda col: snap(t=t0 + 7200))
    m.tick()                                                          # m5_7 ครบเวลา → จดช่วงพัก
    start = datetime.now(timezone.utc) - timedelta(minutes=SH.MIN_WARMUP_BARS + 600)
    bars = [Bar(start + timedelta(minutes=i), 1, 1, 1, 1) for i in range(SH.MIN_WARMUP_BARS + 600)]
    monkeypatch.setattr(SH, "simulate", lambda bot, b, spec, a, z: (-12.5, 3) if bot == "m5_4" else (4.0, 1))
    done = SH.evaluate(lambda tf, n: bars, None)
    assert sorted(e["bot"] for e in done) == ["m5_4", "m5_7"]
    sb = SH.scoreboard()
    assert sb["gemini"] == {"calls": 1, "right": 1, "wrong": 0, "trades": 3, "missed": -12.5} and sb["rules"]["wrong"] == 1
    assert "ช่วยประหยัด 12.50$" in SH.describe(next(e for e in done if e["bot"] == "m5_4"))
    assert SH.evaluate(lambda tf, n: bars, None) == []                # ไม่ประเมินซ้ำ


def test_watchdog_reports_new_problems_once_and_heals(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).resolve().parent.parent / "deploy" / "watchdog.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    monkeypatch.setattr(W, "MEMO", tmp_path / "memo.json")
    sent, cmds = [], []
    monkeypatch.setattr(W, "tg", lambda method, fields, file=None: sent.append(fields["text"]))
    monkeypatch.setattr(W, "sh", lambda *c, timeout=0: cmds.append(c) or "")
    probs = {"down:trader-bots": "container trader-bots ไม่ทำงาน"}
    monkeypatch.setattr(W, "check", lambda: dict(probs))
    W.run_check()
    W.run_check()                                                     # ปัญหาเดิม ไม่ส่งซ้ำ ไม่ซ่อมซ้ำ (cooldown)
    assert len(sent) == 1 and "trader-bots" in sent[0] and "เปิด container bots ใหม่" in sent[0]
    assert sum(1 for c in cmds if "up" in c) == 1
    probs.clear()
    W.run_check()
    assert sent[-1].startswith("✅ กลับมาปกติ")



def test_gate_blocks_entries_in_skipped_regime_and_session() -> None:
    from datetime import datetime, timedelta, timezone
    from bot.core.types import Bar, EnterSignal, ExitSignal, Side
    from bot.strategy.gate import Gate

    class Inner:
        tag, symbol, plan, last_bar = "t", "BTCUSD", None, None

        def on_bar(self, bar, account):  # noqa: ANN001, ANN201
            return [EnterSignal("BTCUSD", Side.BUY, 1.0, None, "t", str(bar.time)), ExitSignal("BTCUSD", "t", "x")]

        def status(self):  # noqa: ANN201
            return {"state": "ok"}
    t0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)                       # ช่วงยุโรป
    g = Gate(Inner(), skip_regimes=("trend",))
    out = []
    for i in range(26 * 5 + 1):                                                  # ราคาขึ้นตรงๆ = เทรนด์
        out = g.on_bar(Bar(t0 + timedelta(minutes=i), 100 + i, 100 + i, 100 + i, 100 + i), None)
    assert g.regime() == "trend" and [type(x).__name__ for x in out] == ["ExitSignal"]   # ทางออกผ่านเสมอ
    g2 = Gate(Inner(), skip_sessions=("asia",))
    assert len(g2.on_bar(Bar(t0, 1, 1, 1, 1), None)) == 2                         # 09:00 ไม่ใช่เอเชีย
    assert len(g2.on_bar(Bar(t0.replace(hour=3), 1, 1, 1, 1), None)) == 1
    assert g.status()["gate"]["blocked"] >= 1


def test_catalog_wraps_strategy_with_gate_params() -> None:
    from bot.strategy.catalog import build
    from bot.strategy.gate import Gate
    s = build("ema", "BTCUSD", {"fast": 9, "slow": 21, "sl_atr": 1.5, "tp_r": 2.0, "max_hold": 24,
                                "skip_regimes": ["range"], "skip_sessions": []}, None, "M5", "m5_7")
    assert isinstance(s, Gate) and s.skip_regimes == ("range",) and s.tag == "m5_7"
    assert not isinstance(build("ema", "BTCUSD", {"fast": 9, "slow": 21, "sl_atr": 1.5, "tp_r": 2.0, "max_hold": 24},
                                None, "M5", "m5_7"), Gate)



def test_paper_mode_predicts_instead_of_pausing_and_scores_real_trades(monkeypatch) -> None:  # noqa: ANN001
    from datetime import datetime, timezone
    from bot import shadow as SH
    monkeypatch.setattr(M, "PAUSE_BOTS", False)
    t0 = M.now_ts() - 5 * 3600
    m, p = M.Manager(FakeCol()), {}
    m.execute(M.Action("pause", "m5_8", 60, "สวนเทรนด์", "gemini"), snap(t=t0), p)
    m.execute(M.Action("pause", "m5_8", 60, "ซ้ำ", "gemini"), snap(t=t0 + 60), p)      # ค้างอยู่แล้ว ไม่จดซ้ำ
    m.execute(M.Action("pause", "m5_2", 60, "x", "rules"), snap(t=t0), p)
    assert p == {} and M.load_pauses() == {} and m.recent == []                         # ไม่พักจริง ไม่กินโควตา
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()  # noqa: E731
    rows = {"m5_8": [{"open_time": iso(t0 + 600), "profit": -7.0}, {"open_time": iso(t0 + 9000), "profit": 50.0}],
            "m5_2": [{"open_time": iso(t0 + 300), "profit": 3.0}]}
    done = SH.evaluate(lambda tf, n: [], None, t0 + 4 * 3600, lambda b: rows.get(b, []))
    by = {e["bot"]: e for e in done}
    assert by["m5_8"]["missed"] == -7.0 and by["m5_2"]["missed"] == 3.0            # นับเฉพาะไม้ที่เปิดในช่วงทำนาย
    sb = SH.scoreboard()
    assert sb["gemini"]["right"] == 1 and sb["rules"]["wrong"] == 1
    assert "ทายถูก" in SH.describe(by["m5_8"]) and "ทายผิด" in SH.describe(by["m5_2"])
    msgs = [x["text"] for x in M.read_log()]
    assert sum("ทำนาย: m5_8" in t for t in msgs) == 1


def test_release_all_pauses_when_paper_mode() -> None:
    from bot import shadow as SH
    t = M.now_ts()
    M.save_pauses({"m5_1": {"until": t + 999, "since": t - 3600, "src": "rules", "tag": "regime", "reason": "x"}})
    M.Manager.release_all_pauses()
    assert M.load_pauses() == {} and SH._load()[-1]["bot"] == "m5_1" and not SH._load()[-1]["paper"]


def test_regime_is_per_symbol_for_gold_and_btc_bots() -> None:
    b = M.RuleBrain()
    bots = dict(m5_4=bot(family="bb"), m5_12=bot(family="bb", symbol="XAUUSD"))
    for _ in range(M.REGIME_CONFIRM):
        s = snap(bars=bars("trend"), **bots)
        s.bars = {"BTCUSD": bars("trend"), "XAUUSD": bars("range")}
        acts = b.decide(s, {})
    assert ("pause", "m5_4") in kinds(acts) and ("pause", "m5_12") not in kinds(acts)   # ทองออกข้าง → bb ถนัด
    assert b.regime == {"BTCUSD": "trend", "XAUUSD": "range"}
    assert any("— ทอง" in a.reason for a in acts if a.kind == "say")


def test_late_trade_and_backfill_recover_context_from_bars(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from datetime import datetime, timezone
    from bot import dataset as DS
    from bot.manager_learn import Learner
    monkeypatch.setattr(DS, "PATH", tmp_path / "dataset.csv")
    bs = bars("trend")
    ot = bs[-5][0]
    t_now = datetime.fromisoformat(bs[-1][0]).replace(tzinfo=timezone.utc).timestamp()
    L = Learner()
    rec_trade = {"ticket": 999, "open_time": ot, "close_time": bs[-1][0], "side": "BUY", "profit": -12.0, "symbol": "BTCUSD"}
    learned = L.observe({"m5_5": {"family": "momo", "symbol": "BTCUSD", "open": [], "recent": [rec_trade]}},
                        "range", 0.1, t_now, 0, {"BTCUSD": ("range", 0.1, 0)}, {"BTCUSD": bs})
    assert len(learned) == 1 and learned[0]["regime"] == "trend" and learned[0]["ctx_src"] == "late"
    assert L.table["momo"]["trend"]["n"] == 1
    meta = {"m5_5": {"family": "momo", "symbol": "BTCUSD"}, "m5_10": {"family": "donchian", "symbol": "XAUUSD"}}
    gold_trade = {"ticket": 1000, "open_time": ot, "close_time": bs[-1][0], "side": "SELL", "profit": 8.5, "symbol": "XAUUSD"}
    bf = L.backfill({"m5_5": [rec_trade], "m5_10": [gold_trade]}, meta, {"XAUUSD": bs}, bs[0][0])
    assert len(bf) == 1 and bf[0]["ticket"] == 1000 and bf[0]["ctx_src"] == "backfill"
    added = DS.append([DS.row(x, meta, []) for x in learned + bf])
    assert added == 2 and DS.append([DS.row(x, meta, []) for x in learned]) == 0
    rows = DS.load()
    assert [int(r["island"]) for r in rows] == [1, 2] and [r["symbol"] for r in rows] == ["BTCUSD", "XAUUSD"]


def test_shadow_failed_fetch_does_not_block_other_episodes() -> None:
    from datetime import datetime, timedelta, timezone
    from bot import shadow as SH
    from bot.core.types import Bar
    t0 = M.now_ts() - 5 * 3600
    SH.record({"bot": "m5_2", "start": t0, "end": t0 + 1800, "src": "rules", "paper": False})
    SH.record({"bot": "m5_10", "start": t0, "end": t0 + 1800, "src": "rules", "paper": True})
    iso = datetime.fromtimestamp(t0 + 300, timezone.utc).isoformat()
    def bad_fetch(sym: str, tf: str, n: int) -> list:
        raise ConnectionError("Invalid params")
    done = SH.evaluate(bad_fetch, None, t0 + 4 * 3600, lambda b: [{"open_time": iso, "profit": -5.0}] if b == "m5_10" else [])
    assert [e["bot"] for e in done] == ["m5_10"] and done[0]["missed"] == -5.0
    assert any(e["bot"] == "m5_2" and not e["done"] for e in SH._load())


def test_ai_adaptive_interval_and_compact_snapshot(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    from bot import manager_ai as AI
    from bot import manager_gemini as G
    brain = G.GeminiBrain()
    calls = []
    brain.client = NS(models=NS(generate_content=lambda **kw: calls.append(kw["contents"]) or NS(function_calls=[], usage_metadata=None)))
    t0 = M.now_ts()
    s0 = snap(t=t0, bars=bars("range"))
    brain.maybe_decide(s0, {})                                            # รอบแรกถามทันที
    assert len(calls) == 1
    brain.maybe_decide(snap(t=t0 + 20 * 60, bars=bars("range")), {})      # พอร์ตว่าง + ตลาดเดิม: 20 นาทียังไม่ถาม (รอ 45 นาที)
    assert len(calls) == 1
    brain.maybe_decide(snap(t=t0 + 20 * 60, bars=bars("trend")), {})      # สภาพตลาดเปลี่ยน → ถามที่ 20 นาทีทันที
    assert len(calls) == 2
    brain.maybe_decide(snap(t=t0 + 36 * 60, bars=bars("trend"), m5_1=bot(opens=[pos()])), {})  # มีไม้เปิด → ถามทุก 15 นาที
    assert len(calls) == 3
    payload = json.loads(calls[0].split("\n", 1)[1])
    assert "open" not in payload["bots"]["m5_1"] and len(payload["btc"]["m5_candles_t_o_h_l_c"]) <= 8


def test_new_research_families_vwap_fvg_div_build_and_signal() -> None:
    from bot.core.types import Bar
    from bot.strategy.catalog import FAMILIES, build
    assert {"vwap", "fvg", "div"} <= set(FAMILIES)
    t0 = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
    acct = NS(positions=lambda sym, tag: [])
    # 1) VWAP reversion: ราคาแกว่งแถว 100 แล้วทุบแรงหลุด -band_atr * ATR → BUY
    vwap = build("vwap", "XAUUSD", {"band_atr": 1.5, "sl_atr": 1.5, "tp_r": 1.5, "max_hold": 12}, None, "M5", "t_vwap")
    sigs = []
    for i in range(30):
        p = 100.0 if i < 29 else 92.0
        sigs.extend(vwap.on_bar(Bar(t0 + timedelta(minutes=5 * i), p, p + 1.0, p - 1.0, p, volume=100.0), acct))
    assert sigs and sigs[-1].side.name == "BUY" and vwap.status()["type"] == "vwap"
    # 2) FVG pullback: เทรนด์ขาขึ้น > EMA200 → กระโดดเปิด gap → ย่อแตะโซน → BUY
    fvg = build("fvg", "XAUUSD", {"min_gap_atr": 0.2, "expire_bars": 10, "sl_atr": 1.5, "tp_r": 2.0}, None, "M5", "t_fvg")
    fvg_sigs = []
    for i in range(210):
        b = Bar(t0 + timedelta(minutes=5 * i), 100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100.2 + i * 0.1)
        fvg.on_bar(b, acct)
    base = 122.0
    fvg.on_bar(Bar(t0 + timedelta(minutes=5 * 210), base, base + 0.5, base - 0.5, base + 0.3), acct)         # b0 high = base+0.5
    fvg.on_bar(Bar(t0 + timedelta(minutes=5 * 211), base + 0.3, base + 4.0, base + 0.2, base + 3.8), acct)   # แท่งพุ่งแรง
    fvg.on_bar(Bar(t0 + timedelta(minutes=5 * 212), base + 3.5, base + 4.5, base + 2.0, base + 4.0), acct)   # b2 low = base+2.0 > b0.high
    fvg_sigs.extend(fvg.on_bar(Bar(t0 + timedelta(minutes=5 * 213), base + 3.0, base + 3.2, base + 1.0, base + 2.5), acct))
    assert fvg_sigs and fvg_sigs[-1].side.name == "BUY"
    # 3) RSI Divergence: สร้างได้และคืน status ครบ
    div = build("div", "BTCUSD", {"lookback": 14, "rsi_ext": 35.0, "sl_atr": 1.5, "tp_r": 2.0}, None, "M5", "t_div")
    for i in range(30):
        div.on_bar(Bar(t0 + timedelta(minutes=5 * i), 100 - i, 101 - i, 99 - i, 100 - i), acct)
    assert div.status()["type"] == "div"


def test_watchdog_flags_repeated_system_order_failures(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    import importlib.util
    from datetime import datetime, timedelta, timezone
    spec = importlib.util.spec_from_file_location("watchdog2", Path(__file__).resolve().parent.parent / "deploy" / "watchdog.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    monkeypatch.setattr(W, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    now = datetime.now(timezone.utc)
    line = lambda dt, why: f"{dt:%Y-%m-%d %H:%M:%S},000 INFO    live: signal m5_8:x not sent: rejected:{why}"  # noqa: E731
    (tmp_path / "logs" / f"m5_8_{now:%Y%m%d}.log").write_text("\n".join([
        line(now - timedelta(hours=2), "connection (-2, 'old')"),                     # เก่าเกิน 30 นาที
        line(now - timedelta(minutes=5), "invalid_stops sl 1 vs bid 2"),             # ตลาดปฏิเสธปกติ ไม่นับ
        *[line(now - timedelta(minutes=m), "connection (-2, 'Unnamed arguments not allowed')") for m in (1, 2, 3)]]),
        encoding="utf-8")
    bad = W.failed_orders(30)
    assert len(bad) == 3 and bad[0][0] == "m5_8" and "Unnamed" in bad[-1][1]


def test_take_profit_closes_bot_when_floating_sum_reaches_target() -> None:
    acts = M.RuleBrain().decide(snap(m5_6=bot(opens=[pos(1, profit=26.0), pos(2, profit=25.0)]),
                                     m5_7=bot(opens=[pos(3, profit=49.0)])), {})
    assert ("close", "m5_6") in kinds(acts) and ("close", "m5_7") not in kinds(acts)
    assert sum(1 for a in acts if a.kind == "close" and a.bot == "m5_6") == 1        # ปิดครั้งเดียวทั้งกลุ่ม


def test_giveback_closes_once_per_bot_even_with_two_pieces() -> None:
    b = M.RuleBrain()
    b.decide(snap(m5_6=bot(opens=[pos(1, profit=40.0), pos(2, profit=40.0)])), {})     # รวม 80 → เป้า $50 ปิดก่อน
    b2 = M.RuleBrain()
    b2.peaks = {1: 30.0, 2: 30.0}
    acts = b2.decide(snap(m5_6=bot(opens=[pos(1, profit=10.0), pos(2, profit=10.0)])), {})
    assert sum(1 for a in acts if a.kind == "close") == 1


def test_llm_daily_quota_stops_calls_and_keeps_one_for_journal(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import manager_ai as A
    monkeypatch.setattr(A, "DAILY_MAX", 3)
    monkeypatch.setattr(A, "USAGE_PATH", tmp_path / "usage.json")
    calls = []
    brain = A.AIBrain()
    brain.ask = lambda prompt, smart=False: calls.append(smart) or []
    monkeypatch.setattr(brain, "_interval_min", lambda s: 0)
    for i in range(5):
        brain.last = 0
        brain.maybe_decide(snap(), {})
    assert len(calls) == 2 and A.quota_left() == 1                    # เหลือ 1 ไว้บันทึกท้ายวัน


def test_smart_model_used_for_important_moments_within_its_quota(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import manager_ai as A
    monkeypatch.setattr(A, "DAILY_MAX", 100)
    monkeypatch.setattr(A, "SMART_DAILY_MAX", 3)
    monkeypatch.setattr(A, "USAGE_PATH", tmp_path / "usage.json")
    calls = []
    brain = A.AIBrain()
    brain.src = "gemini"
    brain.ask = lambda prompt, smart=False: calls.append(smart) or [("say", {"text": "x"})]
    monkeypatch.setattr(brain, "_interval_min", lambda s: 0)
    srcs = []
    for k in range(4):                                                     # ไม้ใหม่ทุกรอบ = จังหวะสำคัญ
        brain.last = 0
        srcs += [a.src for a in brain.maybe_decide(snap(m5_1=bot(opens=[pos(100 + k)])), {})]
    brain.last = 0
    brain.maybe_decide(snap(m5_1=bot(opens=[pos(103)])), {})                # ไม้เดิมยังถืออยู่ = ช่วงปกติ
    assert calls == [True, True, False, False, False]                      # รุ่นฉลาด 2 ครั้ง (เก็บ 1 ให้บันทึกท้ายวัน)
    assert srcs[:2] == ["gemini_smart", "gemini_smart"] and srcs[2] == "gemini"
    assert A.quota_left(key="smart") == 1


def test_claude_code_first_for_important_moments_then_falls_back(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import manager_ai as A
    monkeypatch.setattr(A, "DAILY_MAX", 100)
    monkeypatch.setattr(A, "SMART_DAILY_MAX", 5)
    monkeypatch.setattr(A, "CLAUDE_DAILY_MAX", 3)
    monkeypatch.setattr(A, "USAGE_PATH", tmp_path / "usage.json")
    brain = A.AIBrain()
    brain.src = "gemini"
    gem = []
    brain.ask = lambda prompt, smart=False: gem.append(smart) or [("say", {"text": "g"})]
    cl = []

    class FakeClaude:
        def ask(self, prompt):  # noqa: ANN001, ANN202
            cl.append(prompt)
            if len(cl) == 2:
                raise RuntimeError("limit")
            return [("say", {"text": "c"}), ("pause_bot", {"bot": "m5_1", "minutes": 60, "reason": "r"})]
    brain.claude = FakeClaude()
    monkeypatch.setattr(brain, "_interval_min", lambda s: 0)
    out = []
    for k in range(3):
        brain.last = 0
        out.append({a.src for a in brain.maybe_decide(snap(m5_1=bot(opens=[pos(200 + k)])), {})})
    assert out[0] == {"claude_smart"}                                       # ครั้งแรก Claude
    assert out[1] == {"gemini_smart"}                  # Claude ล้ม → Gemini รุ่นฉลาดในรอบเดียวกัน
    assert out[2] == {"gemini_smart"} and len(cl) == 2 and len(gem) == 2                    # โควตา Claude เหลือ 1 เก็บไว้บันทึกท้ายวัน
    assert A.quota_left(key="claude") == 1


def test_claude_code_runner_parses_structured_output(monkeypatch) -> None:  # noqa: ANN001
    import json as J
    from types import SimpleNamespace as NS2
    from bot import manager_claude_code as C
    seen = {}

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN202
        seen["cmd"], seen["input"] = cmd, kw["input"]
        out = {"is_error": False, "result": "", "structured_output": {
            "say": "ตลาดออกข้าง", "actions": [{"name": "close_positions", "bot": "m5_2", "reason": "สวนเทรนด์"}]}}
        return NS2(returncode=0, stdout=J.dumps(out), stderr="")
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    calls = C.ClaudeCode().ask('{"x":1}')
    assert calls == [("say", {"text": "ตลาดออกข้าง"}), ("close_positions", {"bot": "m5_2", "reason": "สวนเทรนด์"})]
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--json-schema" in cmd and seen["input"] == '{"x":1}'
    monkeypatch.setattr(C.subprocess, "run", lambda cmd, **kw: NS2(returncode=1, stdout='{"is_error":true,"result":"limit"}', stderr=""))
    try:
        C.ClaudeCode().write("x")
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "limit" in str(e)



class GuardCol:
    """จำลอง Collector สำหรับตัวเฝ้าไม้แบบเร็ว"""
    def __init__(self, positions):  # noqa: ANN001
        self.bots = {"main": 1, "m5_10": 30, "m5_1": 21}
        self.by_magic = {v: k for k, v in self.bots.items()}
        self.positions, self.closed, self.modified = positions, [], []

    def raw_positions(self):  # noqa: ANN201
        return list(self.positions)

    def symbol_meta(self, sym):  # noqa: ANN001, ANN201
        return (100.0, 0.5, 2) if sym == "GOLD" else (1.0, 200.0, 2)     # ทอง: ระยะขั้นต่ำแคบ · BTC: $200

    def modify_sl(self, ticket, sl):  # noqa: ANN001, ANN201
        self.modified.append((ticket, sl))
        return True

    def close_bot_positions(self, bot):  # noqa: ANN001, ANN201
        self.closed.append(bot)
        return [{"ok": True}]


def P(ticket, magic, profit, symbol="GOLD", typ=0, open_=4100.0, cur=4105.0, sl=4090.0, vol=0.04):  # noqa: ANN001, ANN201, N802
    return NS(ticket=ticket, magic=magic, profit=profit, swap=0.0, symbol=symbol, type=typ, price_open=open_,
              price_current=cur, sl=sl, tp=0.0, volume=vol)


def test_guard_locks_profit_at_broker_and_never_touches_main() -> None:
    col = GuardCol([P(1, 30, 20.0), P(9, 1, 80.0)])                       # m5_10 กำไร 20 · main กำไร 80
    m = M.Manager(col)
    m.guard()
    assert col.modified and col.modified[0][0] == 1 and col.closed == []
    t, sl = col.modified[0]
    assert abs((sl - 4100.0) * 0.04 * 100 - 10.0) < 0.01                  # ล็อก 50% ของ +$20 = +$10
    col.positions = [P(1, 30, 8.0, sl=sl)]                                 # หดเหลือ 8 (< ครึ่งของ 20)
    m.guard()
    assert col.closed == ["m5_10"]


def test_guard_takes_profit_at_50_and_skips_lock_when_broker_distance_too_wide() -> None:
    col = GuardCol([P(2, 21, 30.0, symbol="BITCOIN", open_=81000, cur=81060, sl=80600, vol=0.5)])
    m = M.Manager(col)
    m.guard()
    assert col.modified == [] and col.closed == []                        # BTC กำไร $30 แต่ SL ต้องห่าง $200 → ล็อกยังไม่ได้
    col.positions = [P(2, 21, 51.0, symbol="BITCOIN", open_=81000, cur=81102, sl=80600, vol=0.5)]
    m.guard()
    assert col.closed == ["m5_1"]


def test_gemini_falls_back_to_next_model_when_overloaded(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    from bot import manager_gemini as G
    tried = []

    def gen(model, contents, config):  # noqa: ANN001, ANN202
        tried.append(model)
        if len(tried) == 1:
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand")
        return NS(function_calls=[NS(name="say", args={"text": "ok"})], usage_metadata=None)
    b = G.GeminiBrain()
    b.client = NS(models=NS(generate_content=gen))
    assert b.ask("x") == [("say", {"text": "ok"})] and len(tried) == 2 and tried[0] != tried[1]
    tried.clear()

    def bad(model, contents, config):  # noqa: ANN001, ANN202
        tried.append(model)
        raise RuntimeError("400 INVALID_ARGUMENT")
    b.client = NS(models=NS(generate_content=bad))
    with pytest.raises(RuntimeError):
        b.ask("x")
    assert len(tried) == 1                                                  # error อื่นไม่ลองรุ่นสำรอง


def test_closed_trades_reported_with_real_pnl_once_per_bot(monkeypatch) -> None:  # noqa: ANN001
    from bot.live import notify as N
    sent = []
    monkeypatch.setattr(M, "_notifier", NS(send=lambda t: sent.append(t), token=None, chat=None))
    results = {11: {"bot": "m5_6", "symbol": "BTCUSD", "side": "BUY", "volume": 0.25, "reason": "manual_close", "profit": 34.87},
               12: {"bot": "m5_6", "symbol": "BTCUSD", "side": "BUY", "volume": 0.25, "reason": "manual_close", "profit": 34.86},
               13: None}                                                    # deal ยังไม่มา

    class Col(GuardCol):
        def closed_result(self, t):  # noqa: ANN001, ANN201
            return results.get(t)

        def data(self):  # noqa: ANN201
            return {"per_bot": {"m5_6": {"today": 69.73, "today_trades": 2, "today_wins": 2},
                                "m5_4": {"today": -118.87, "today_trades": 1, "today_wins": 0}}}
    col = Col([P(11, 21, 30.0), P(12, 21, 30.0), P(13, 30, 5.0)])
    col.bots["m5_6"] = 21
    col.by_magic = {v: k for k, v in col.bots.items()}
    m = M.Manager(col)
    m.report_closed(col.raw_positions())                                    # เริ่มงาน: จำไว้ ไม่แจ้ง
    m.close_why["m5_6"] = "ผู้จัดการปิด: ถึงเป้า +50$"
    m.report_closed([])                                                     # ทั้ง 3 ไม้หายไป
    assert len(sent) == 1 and "m5_6 BTC BUY 0.5 (2 ชิ้น) · +69.73$ · ผู้จัดการปิด: ถึงเป้า +50$" in sent[0]
    assert "วันนี้: -49.14$ (ชนะ 2 แพ้ 1)" in sent[0]
    assert 13 in m.pending_closed                                           # รอ deal รอบหน้า
    results[13] = {"bot": "m5_10", "symbol": "XAUUSD", "side": "SELL", "volume": 0.04, "reason": "SL", "profit": -33.76}
    m.report_closed([])
    assert sent[-1].startswith("🔴 m5_10 ทอง SELL 0.04 · -33.76$ · ชน SL") and not m.pending_closed
