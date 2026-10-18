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
    out, p = [], 100.0
    for i in range(n):
        step = 0.5 if kind == "trend" else (3.0 if (i // 5) % 2 == 0 else -3.0)
        o, p = p, p + step
        out.append([f"2026-10-18T00:{i % 60:02d}:00+00:00", o, max(o, p) + .1, min(o, p) - .1, p])
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
    assert b.regime == "trend"
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
    L.table = {"ema": {"trend": {"n": 10, "pnl": -40.0, "wins": 2}}}             # สมมติฐานบอก ema ถนัดเทรนด์ แต่ผลจริงเสีย
    b = M.RuleBrain(L)
    for _ in range(M.REGIME_CONFIRM):
        acts = b.decide(snap(bars=bars("trend"), m5_2=bot(family="ema")), {})
    pause = [a for a in acts if a.kind == "pause" and a.bot == "m5_2"]
    assert pause and "ผลจริง 10 ไม้" in pause[0].reason


def test_journal_written_on_new_day_with_ai_note(monkeypatch) -> None:  # noqa: ANN001
    m = M.Manager(FakeCol())
    m.llm = NS(src="gemini", write=lambda prompt: "วันนี้ตลาดออกข้าง พรุ่งนี้ระวังสายตามเทรนด์")
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
