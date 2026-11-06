"""ผู้จัดการแบบ dynamic: lot มาตรฐาน · ตัวคูณขนาดไม้ · สลับสาย · ให้คะแนนการสั่งปิดไม้"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot import allocator as A
from bot import close_score as CS
from bot import config as C
from bot.core.types import Bar
from bot.risk.risk_manager import RiskConfig, RiskManager


@pytest.fixture(autouse=True)
def _paths(monkeypatch, tmp_path: Path):  # noqa: ANN001, ANN202
    monkeypatch.setattr(A, "ALLOC_PATH", tmp_path / "alloc.json")
    monkeypatch.setattr(CS, "PATH", tmp_path / "closes.jsonl")
    monkeypatch.setattr(C, "M5_BOTS_TUNED", tmp_path / "m5_bots.json")
    return tmp_path


def _rows(bot: str, family: str, profits: list[float], days_ago: float = 1) -> list[dict]:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return [{"bot": bot, "family": family, "open_time": (t + timedelta(minutes=i)).isoformat(), "profit_std": p}
            for i, p in enumerate(profits)]


def test_std_profit_converts_to_research_lot() -> None:
    assert C.std_profit("BTCUSD", 0.5, -150.0) == -51.0          # 0.5 lot → 0.17
    assert C.std_profit("XAUUSD", 0.04, 12.0) == 12.0
    assert C.std_profit("XAUUSD", 0, 7.0) == 7.0                  # ไม่รู้ lot → คงเดิม


def test_rule_scale_follows_t_stat() -> None:
    assert A.rule_scale({"n": 5, "t": -5}) == 1.0                 # ไม้น้อย → มาตรฐาน
    assert A.rule_scale({"n": 30, "t": 2.5}) == 1.5
    assert A.rule_scale({"n": 30, "t": 1.2}) == 1.0
    assert A.rule_scale({"n": 30, "t": -1.2}) == 1.0
    assert A.rule_scale({"n": 30, "t": -3}) == 0.5
    assert A.rule_scale({"n": 30, "t": 0.1}) == 1.0


def test_rebalance_scales_winners_up_losers_down_and_ignores_other_families() -> None:
    win = _rows("m5_1", "orb", [5, 6, 4, 7, 5, 6, 5, 4, 6, 5, 7, 6, 5, 4, 6, 5])
    lose = _rows("m5_2", "bb", [-5, -6, -4, -7, -5, -6, -5, -4, -6, -5, -7, -6, -5, -4, -6, -5])
    old_family = _rows("m5_3", "donchian", [-9] * 20)             # สายเก่าของบอทที่ถูกสลับแล้ว → ไม่นับ
    ch = A.rebalance(win + lose + old_family, [{"bot": "m5_1", "family": "orb"}, {"bot": "m5_2", "family": "bb"},
                                               {"bot": "m5_3", "family": "pullback"}])
    assert A.scale_of("m5_1") == 1.5 and A.scale_of("m5_2") == 0.5 and A.scale_of("m5_3") == 1.0
    assert {c["bot"] for c in ch} == {"m5_1", "m5_2"}
    assert A.scale_of("main") == 1.0


def test_set_scale_is_clamped() -> None:
    assert A.set_scale("m5_4", 9, "x", "claude_smart") == 2.0
    assert A.set_scale("m5_4", 0.01, "x", "claude_smart") == 0.25


def test_risk_manager_applies_lot_scale_with_broker_minimum() -> None:
    spec = C.DEFAULT_SPECS["XAUUSD"]
    acct = NS(equity=lambda: 1000.0, balance=lambda: 1000.0, free_margin=lambda: 1e6, positions=lambda *a: [])
    sig = NS(symbol="XAUUSD", sl=4000.0)
    rm = RiskManager(RiskConfig(fixed_lots=(("XAUUSD", 0.04),)), acct, lot_scale=lambda: 2.0)
    assert rm.size(sig, 4010.0, spec).volume == pytest.approx(0.08)
    rm.lot_scale = lambda: 0.1
    assert rm.size(sig, 4010.0, spec).volume == pytest.approx(spec.volume_min)
    rm.lot_scale = lambda: (_ for _ in ()).throw(OSError())         # อ่านไม่ได้ → มาตรฐาน
    assert rm.size(sig, 4010.0, spec).volume == pytest.approx(0.04)


def _lab(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    slots = [
        {"symbol": "BTCUSD", "family": "donchian", "wf": {"usd_day": -9}, "holdout": {"usd_day": -60}, "live": {"params": {"entry_n": 1}, "plan": "native"}},
        {"symbol": "BTCUSD", "family": "orb", "wf": {"usd_day": -2}, "holdout": {"usd_day": 8}, "live": {"params": {"sl_mode": "mid"}, "plan": "native"},
         "pre_holdout": {"params": {"sl_mode": "opposite"}, "plan": "ratchet"}},
        {"symbol": "BTCUSD", "family": "volx", "wf": {"usd_day": 0}, "holdout": {"usd_day": 3}, "live": {"params": {"box_n": 48}, "plan": "native"}},
        {"symbol": "XAUUSD", "family": "momo", "wf": {"usd_day": 9}, "holdout": {"usd_day": 26}, "live": {"params": {"x": 1}, "plan": "trail"}},
    ]
    p = tmp_path / "lab.json"
    p.write_text(json.dumps({"slots": slots}), encoding="utf-8")
    monkeypatch.setattr(A, "LAB_PATH", p)


def test_candidates_prefer_unused_family_then_variant_params(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    _lab(tmp_path, monkeypatch)
    used = [{"bot": "m5_5", "symbol": "BTCUSD", "family": "donchian", "params": {"entry_n": 1}},
            {"bot": "m5_6", "symbol": "BTCUSD", "family": "orb", "params": {"sl_mode": "mid", "skip_regimes": ["range"]}}]
    c = A.candidates("BTCUSD", "donchian", used)
    assert [x["family"] for x in c] == ["volx", "orb"]                     # สายที่ยังไม่มีใครเล่นมาก่อน
    assert c[1]["params"] == {"sl_mode": "opposite"}                       # orb ซ้ำสาย → ใช้ชุดพารามิเตอร์อื่น


def test_rotate_replaces_proven_loser_once_and_resets_scale(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    _lab(tmp_path, monkeypatch)
    doc = {"generated": "x", "bots": [
        {"bot": "m5_5", "symbol": "BTCUSD", "family": "donchian", "params": {"entry_n": 1}, "plan": "native", "wf_usd_day": -9},
        {"bot": "m5_6", "symbol": "BTCUSD", "family": "orb", "params": {"sl_mode": "mid"}, "plan": "native", "wf_usd_day": -2}]}
    C.M5_BOTS_TUNED.write_text(json.dumps(doc), encoding="utf-8")
    A.set_scale("m5_5", 0.25, "x", "rules")
    rows = _rows("m5_5", "donchian", [-8, -5, -9, -4, -7, -6, -8, -5, -9, -6, -7, -4, -8, -6, -5, -9, -7, -6, -8, -5])
    r = A.rotate(rows)
    assert r and r["bot"] == "m5_5" and r["to"] == "volx"
    new = {b["bot"]: b for b in json.loads(C.M5_BOTS_TUNED.read_text(encoding="utf-8"))["bots"]}
    assert new["m5_5"]["family"] == "volx" and new["m5_5"]["reassigned"]["from"]["family"] == "donchian"
    assert A.scale_of("m5_5") == 1.0
    assert A.rotate(rows) is None                                          # เพิ่งสลับ → วันละไม่เกิน 1 ตัว / ตัวเดิมพัก 7 วัน


def test_rotate_keeps_bot_when_research_is_positive_or_evidence_thin(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    _lab(tmp_path, monkeypatch)
    doc = {"bots": [{"bot": "m5_10", "symbol": "XAUUSD", "family": "donchian", "params": {}, "plan": "native", "wf_usd_day": 13.6},
                    {"bot": "m5_5", "symbol": "BTCUSD", "family": "donchian", "params": {}, "plan": "native", "wf_usd_day": -9}]}
    C.M5_BOTS_TUNED.write_text(json.dumps(doc), encoding="utf-8")
    rows = _rows("m5_10", "donchian", [-8, -5, -9, -4, -7, -6, -8, -5, -9, -6, -7, -4, -8, -6, -5, -9, -7, -6, -8, -5]) \
        + _rows("m5_5", "donchian", [-8, -5, -9])
    assert A.rotate(rows) is None


def _bar(t: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(t, o, h, low, c, 0.0, 0.0)


def test_close_score_counterfactual_sl_tp_and_horizon() -> None:
    t0 = datetime.now(timezone.utc) - timedelta(hours=30)
    r = {"side": "BUY", "sl": 99.0, "tp": 105.0, "close_t": t0.timestamp(), "close_price": 100.0}
    bars = [_bar(t0 + timedelta(minutes=i), 100, 101, 99.5, 100.5) for i in range(1, 5)] + [_bar(t0 + timedelta(minutes=6), 100, 106, 99.5, 105)]
    assert CS.counterfactual(r, bars, time.time()) == (105.0, "TP")
    bars[2] = _bar(t0 + timedelta(minutes=3), 100, 101, 98.0, 99)
    assert CS.counterfactual(r, bars, time.time()) == (99.0, "SL")
    flat = [_bar(t0 + timedelta(minutes=i), 100, 101, 99.5, 100.2) for i in range(0, 24 * 60 + 5, 60)]
    assert CS.counterfactual({**r, "sl": None, "tp": None}, flat, time.time())[1].startswith("ถือครบ")
    fresh = {**r, "close_t": time.time() - 600}
    assert CS.counterfactual(fresh, [_bar(datetime.now(timezone.utc), 100, 101, 99.5, 100.2)], time.time()) is None


def test_close_score_evaluate_and_scoreboard() -> None:
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    CS.record({"ticket": 1, "bot": "m5_8", "symbol": "XAUUSD", "side": "SELL", "volume": 0.04, "close_price": 4000.0, "profit": -5.0},
              {"src": "claude_smart", "why": "สวนเทรนด์", "sl": 4010.0, "tp": 3980.0})
    rows = [json.loads(x) for x in CS.PATH.read_text(encoding="utf-8").splitlines()]
    rows[0]["close_t"] = t0.timestamp()
    CS._save(rows)
    bars = [_bar(t0 + timedelta(minutes=5), 4000, 4011, 3999, 4010)]                 # ถ้าไม่ปิด → ชน SL 4010
    out = CS.evaluate(lambda sym, tf, n: bars)
    assert out and out[0]["cf_how"] == "SL" and out[0]["saved"] == pytest.approx(40.0)   # 10 × 0.04 × 100
    assert CS.scoreboard()["claude_smart"] == {"n": 1, "helped": 1, "hurt": 0, "saved": 40.0}
    assert "ปิดถูก" in CS.describe(out[0])


def test_learner_rebuild_uses_standard_lot(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import manager_learn as L
    monkeypatch.setattr(L, "LEARN_PATH", tmp_path / "learn.json")
    monkeypatch.setattr(L, "TAPE_PATH", tmp_path / "tape.json")
    lr = L.Learner()
    lr.rebuild([{"family": "orb", "regime": "trend", "session": "us", "trend": "with", "profit_std": -51.0},
                {"family": "orb", "regime": "trend", "session": "us", "trend": "with", "profit_std": 9.0}])
    assert lr.table["orb"]["trend"] == {"n": 2, "pnl": -42.0, "wins": 1} and lr.unit == "std"
    assert L.Learner().unit == "std"


def test_ai_set_lot_scale_becomes_action_and_manager_applies_it(monkeypatch) -> None:  # noqa: ANN001
    from bot import manager as M
    from bot.manager_ai import to_actions
    acts = to_actions([("set_lot_scale", {"bot": "m5_8", "scale": 1.5, "reason": "rsi2 เหมาะกับตลาดออกข้าง"}),
                       ("set_lot_scale", {"bot": "main", "scale": 2, "reason": "x"})], "claude_smart")
    assert [(a.kind, a.bot, a.value) for a in acts] == [("scale", "m5_8", 1.5), ("scale", "main", 2.0)]   # main ดูแลเหมือนกัน
    said = []
    monkeypatch.setattr(M, "say", lambda kind, text, **k: said.append(text))
    monkeypatch.setattr(M, "AI_SCALE", True)
    m = M.Manager.__new__(M.Manager)
    m.recent, m.last_scale = [], {}
    s = NS(t=time.time(), bots={"m5_8": {"open": []}})
    m.execute(acts[0], s, {})
    assert A.scale_of("m5_8") == 1.5 and "×1 → ×1.5" in said[-1]
    m.execute(M.Action("scale", "m5_8", reason="อีกรอบ", src="claude_smart", value=2.0), s, {})
    assert A.scale_of("m5_8") == 1.5                                    # ปรับซ้ำเร็วเกิน → ไม่ทำ


def test_deeper_daily_loss_is_an_important_moment(monkeypatch) -> None:  # noqa: ANN001
    from bot import manager_ai as AI
    monkeypatch.setattr(AI, "BAD_DAY_STEP", 150.0)
    brain = AI.AIBrain()
    mk = lambda pnl: NS(t=time.time(), test_bots=lambda: {"m5_1": {"today": pnl, "open": []}},  # noqa: E731
                        symbols=lambda: [], bars_of=lambda sym: [], btc_bars=[])
    brain.last_tickets, brain.last_regimes = frozenset(), {"BTCUSD": None}
    assert brain._important(mk(-100)) is None
    assert brain._important(mk(-160)) == "ขาดทุนวันนี้ลึกขึ้น"
    brain.last_loss = AI.loss_step(mk(-160))                               # ผู้จัดการรับมือขั้นนี้แล้ว
    assert brain._important(mk(-250)) is None
    assert brain._important(mk(-310)) == "ขาดทุนวันนี้ลึกขึ้น"              # ลึกลงอีกขั้น → เรียกอีกครั้ง


def test_quiet_periods_use_claude_sonnet_then_gemini_when_capped(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from bot import manager_ai as AI
    from tests.test_manager import snap
    monkeypatch.setattr(AI, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(AI, "DAILY_MAX", 100)
    monkeypatch.setattr(AI, "CLAUDE_ROUTINE_MAX", 1)
    brain = AI.AIBrain()
    brain.src = "gemini"
    gem, cl = [], []
    brain.ask = lambda prompt, smart=False: gem.append(smart) or [("say", {"text": "g"})]
    brain.claude = NS(ask=lambda prompt, model="opus", effort="high": cl.append((model, effort)) or [("say", {"text": "c"})])
    monkeypatch.setattr(brain, "_interval_min", lambda s: 0)
    monkeypatch.setattr(brain, "_important", lambda s: None)              # ตลาดเงียบ
    s = snap()
    brain.last = 0
    assert {a.src for a in brain.maybe_decide(s, {})} == {"claude"} and cl == [("sonnet", "low")]
    brain.last = 0
    assert {a.src for a in brain.maybe_decide(s, {})} == {"gemini"} and gem == [False]   # ครบเพดาน Sonnet → Gemini Lite


def test_ai_close_is_paper_only_and_scored_while_rules_still_close(monkeypatch) -> None:  # noqa: ANN001
    from bot import manager as M
    said, closed = [], []
    monkeypatch.setattr(M, "say", lambda kind, text, **k: said.append(text))
    m = M.Manager.__new__(M.Manager)
    m.recent, m.close_ctx, m.close_why = [], {}, {}
    m.col = NS(close_bot_positions=lambda bot: closed.append(bot) or [{"ok": True}])
    o = {"ticket": 7, "symbol": "XAUUSD", "side": "BUY", "volume": .04, "price": 4000.0, "profit": -12.0, "sl": 3990.0, "tp": None}
    s = NS(t=time.time(), bots={"m5_8": {"open": [o]}})
    m.execute(M.Action("close", "m5_8", reason="สวนเทรนด์", src="claude_smart"), s, {})
    assert closed == [] and "ไม่ปิดจริง" in said[-1]                         # AI: ไม่ปิดจริง
    row = json.loads(CS.PATH.read_text(encoding="utf-8").splitlines()[0])
    assert row["src"] == "claude_smart_paper" and row["close_price"] == 4000.0 and row["sl"] == 3990.0
    m.execute(M.Action("close", "m5_8", reason="ถือนาน", src="rules"), s, {})
    assert closed == ["m5_8"]                                               # กฎยังปิดจริง


def test_bot_can_have_its_own_timeframe_and_retune_leaves_it_alone(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research"))
    import m5_lab
    cur = [{"bot": "m5_17", "family": "orb", "symbol": "XAUUSD", "tf": "H1", "lot": .04, "params": {"a": 9}, "plan": "trail"}]
    monkeypatch.setattr(C, "load_m5_bots", lambda: cur)
    monkeypatch.setattr(m5_lab, "keep_current", lambda b, z: False)
    monkeypatch.setattr(sys, "argv", ["m5_lab", "--no-gates"])
    slot = {"family": "orb", "symbol": "XAUUSD", "live": {"params": {"a": 1}, "plan": "native"}, "wf": {"usd_day": 1.0},
            "holdout": {"usd_day": .5}, "wf_random": None}
    out = tmp_path / "bots.json"
    assert m5_lab.retune({}, [slot], out) == []                             # จูนจากผล M5 ไม่แตะบอท H1
    assert json.loads(out.read_text(encoding="utf-8"))["bots"][0]["params"] == {"a": 9}


def _explorer(monkeypatch, tmp_path: Path, t: float = 2.0) -> None:  # noqa: ANN001
    p = tmp_path / "explorer.json"
    p.write_text(json.dumps({"rows": [{"family": "momo", "symbol": "XAUUSD", "tf": "M15", "edge": True, "wf": {"t": t, "usd_day": 9.0},
                                       "holdout": {"usd_day": 4.0}, "live": {"params": {"entry_time": "11:00"}, "plan": "trail"}}]}),
                 encoding="utf-8")
    monkeypatch.setattr(A, "EXPLORE_PATH", p)


def test_rotate_moves_idle_loser_to_explorer_edge_in_another_market(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    _lab(tmp_path, monkeypatch)
    _explorer(monkeypatch, tmp_path)
    monkeypatch.setattr(A, "candidates", lambda *a: [])
    monkeypatch.setattr(A, "_idle", lambda bot: True)
    doc = {"bots": [{"bot": "m5_8", "symbol": "BTCUSD", "family": "rsi2", "params": {}, "plan": "native", "wf_usd_day": -9, "holdout_usd_day": -3}]}
    C.M5_BOTS_TUNED.write_text(json.dumps(doc), encoding="utf-8")
    r = A.rotate(_rows("m5_8", "rsi2", [5, -9, 4, -8, 6, -7, 5, -9, 3, -8, 6, -9, 5, -7, 4, -8, 6, -9, 5, -8]))   # t ไม่ถึง −1.5 แต่ติดลบทุกด้าน
    assert r and r["to"] == "momo"
    b = json.loads(C.M5_BOTS_TUNED.read_text(encoding="utf-8"))["bots"][0]
    assert (b["symbol"], b["tf"], b["lot"]) == ("XAUUSD", "M15", C.STD_LOTS["XAUUSD"])


def test_explorer_edge_needs_strong_t_and_bot_with_open_trade_stays(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    _lab(tmp_path, monkeypatch)
    _explorer(monkeypatch, tmp_path, t=0.6)
    assert A.explorer_candidates([]) == []
    _explorer(monkeypatch, tmp_path)
    assert A.explorer_candidates([{"family": "momo", "symbol": "XAUUSD", "tf": "M15"}]) == []
    monkeypatch.setattr(A, "candidates", lambda *a: [])
    monkeypatch.setattr(A, "_idle", lambda bot: False)
    doc = {"bots": [{"bot": "m5_8", "symbol": "BTCUSD", "family": "rsi2", "params": {}, "plan": "native", "wf_usd_day": -9, "holdout_usd_day": -3}]}
    C.M5_BOTS_TUNED.write_text(json.dumps(doc), encoding="utf-8")
    assert A.rotate(_rows("m5_8", "rsi2", [-5, -9, -4, -8, -6, -7, -5, -9, -3, -8, -6, -9, -5, -7, -4, -8, -6, -9, -5, -8])) is None


def test_ai_scale_is_paper_by_default(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    from types import SimpleNamespace as NS

    from bot import manager as M
    said = []
    monkeypatch.setattr(M, "say", lambda kind, text, **k: said.append(text))
    monkeypatch.setattr(M, "SCALE_PAPER_PATH", tmp_path / "s.jsonl")
    m = M.Manager.__new__(M.Manager)
    m.recent, m.last_scale = [], {}
    m.execute(M.Action("scale", "m5_9", reason="x", src="claude_smart", value=0.5), NS(t=time.time(), bots={"m5_9": {"open": []}}), {})
    assert A.scale_of("m5_9") == 1.0 and "ทดลอง" in said[-1]
    assert json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))["scale"] == 0.5
