"""วงจรวิจัยจากไม้จริง: ตัวกรองโหมดสังเกต → dataset → หลักฐาน → เลื่อนขั้น · กันเปลี่ยนพารามิเตอร์ไปมา"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import config as C
from bot import dataset as DS
from bot.core.types import Bar, EnterSignal, Side
from bot.strategy.gate import Gate, signature

ROOT = Path(__file__).resolve().parent.parent


class Inner:
    tag, symbol, plan, last_bar = "m5_8", "BTCUSD", None, None

    def on_bar(self, bar, account):  # noqa: ANN001, ANN201
        return [EnterSignal("BTCUSD", Side.SELL, 1.0, None, "m5_8", f"k{bar.time:%H%M}")]

    def status(self):  # noqa: ANN201
        return {}


@pytest.fixture(autouse=True)
def _paths(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(DS, "PATH", tmp_path / "dataset.csv")
    monkeypatch.setattr(C, "instance_state_dir", lambda name: tmp_path / name)
    (tmp_path / "m5_8").mkdir()
    monkeypatch.setattr(Gate, "LOG_DIR", None)


def test_observe_gate_does_not_block_but_flags(tmp_path: Path) -> None:
    Gate.LOG_DIR, Gate.FRESH_SEC = tmp_path / "m5_8", 0
    g = Gate(Inner(), skip_sessions=("asia",), mode="observe")
    t = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    assert len(g.on_bar(Bar(t, 1, 1, 1, 1), None)) == 1                     # ไม่บล็อก
    flags = [json.loads(x) for x in (tmp_path / "m5_8" / "gate_flags.jsonl").read_text().splitlines()]
    assert flags[0]["gate"] == "|asia" and flags[0]["session"] == "asia"
    assert len(Gate(Inner(), skip_sessions=("asia",)).on_bar(Bar(t, 1, 1, 1, 1), None)) == 0   # active บล็อก


def test_dataset_row_joins_gate_flag_and_prediction(tmp_path: Path) -> None:
    sig = signature([], ["asia"])
    t = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    (tmp_path / "m5_8" / "gate_flags.jsonl").write_text(json.dumps({"t": t.isoformat(), "gate": sig}) + "\n")
    bots = {"m5_8": {"params": {"rsi_th": 10, "skip_sessions": ["asia"], "gate_mode": "observe"}, "plan": "native", "wf_usd_day": -2.0}}
    eps = [{"bot": "m5_8", "paper": True, "src": "gemini", "start": t.timestamp() - 60, "end": t.timestamp() + 3600}]
    rec = {"bot": "m5_8", "family": "rsi2", "ticket": 1, "side": "SELL", "open_time": (t + timedelta(minutes=1)).isoformat(),
           "close_time": (t + timedelta(minutes=31)).isoformat(), "profit": -6.0, "regime": "trend", "session": "asia", "trend": "against"}
    r = DS.row(rec, bots, eps)
    assert r["gate_flag"] == 1 and r["gate_mode"] == "observe" and r["prediction_by"] == "gemini" and r["hold_min"] == 30.0
    assert json.loads(r["params"]) == {"rsi_th": 10}
    DS.append([r, {**r, "ticket": 2, "profit": 4.0, "gate_flag": 0}])
    ev = DS.gate_evidence("m5_8", sig)
    assert ev["n"] == 1 and ev["avg"] == -6.0 and ev["n_other"] == 1


def _lab(monkeypatch, cur):  # noqa: ANN001, ANN202
    sys.path.insert(0, str(ROOT / "research"))
    import m5_lab
    monkeypatch.setattr(C, "load_m5_bots", lambda: cur)
    monkeypatch.setattr(m5_lab, "_LAST", {})
    return m5_lab


def test_new_gate_starts_observe_then_promotes_with_live_evidence(monkeypatch, tmp_path: Path) -> None:
    cur = [{"bot": "m5_8", "family": "rsi2", "symbol": "BTCUSD", "params": {"rsi_th": 10.0}, "plan": "native"}]
    L = _lab(monkeypatch, cur)
    gate = {"skip_regimes": [], "skip_sessions": ["asia"], "score_from": -9.0, "score_to": -3.0, "holdout_from": -5.0, "holdout_to": 1.0}
    monkeypatch.setattr(L, "gate_search", lambda bots, w: {"m5_8": dict(gate)})
    slot = {"family": "rsi2", "symbol": "BTCUSD", "live": {"params": {"rsi_th": 10.0}, "plan": "native"}, "live_score": 1.0,
            "wf": {"usd_day": -1.0}, "holdout": {"usd_day": 0.0}, "wf_random": None}
    out = tmp_path / "m5_bots.json"
    L.retune({}, [slot], out)
    b = json.loads(out.read_text(encoding="utf-8"))["bots"][0]
    assert b["params"]["gate_mode"] == "observe"                              # ตัวกรองใหม่ยังไม่บล็อกจริง
    cur[0] = b
    monkeypatch.setattr(L, "GATE_EVIDENCE", {"m5_8": {"n": 12, "avg": -4.0, "t": -2.0}})
    L.retune({}, [slot], out)
    assert json.loads(out.read_text(encoding="utf-8"))["bots"][0]["params"]["gate_mode"] == "observe"   # หลักฐานยังไม่พอ
    monkeypatch.setattr(L, "GATE_EVIDENCE", {"m5_8": {"n": 25, "avg": -4.0, "t": -2.0}})
    ch = L.retune({}, [slot], out)
    assert json.loads(out.read_text(encoding="utf-8"))["bots"][0]["params"]["gate_mode"] == "active"
    assert ch and ch[0]["gate"]["promoted"]["n"] == 25


def test_params_kept_when_new_pick_is_not_clearly_better(monkeypatch, tmp_path: Path) -> None:
    import numpy as np
    import pandas as pd
    cur = [{"bot": "m5_7", "family": "ema", "symbol": "BTCUSD", "plan": "native",
            "params": {"fast": 5, "slow": 13, "sl_atr": 1.0, "tp_r": 1.0, "max_hold": 24}}]
    L = _lab(monkeypatch, cur)
    monkeypatch.setattr(L, "gate_search", lambda bots, w: {})
    end = pd.Timestamp("2026-10-18", tz="UTC")
    t = np.array([(end - pd.Timedelta(days=170 - i)).timestamp() for i in range(160)], dtype=float)
    key = ("ema", "BTCUSD", 0, "native", "rule")
    L._LAST.update(results={key: (t, np.full(160, 2.0))}, span={"BTCUSD": {"end": end}})
    new = {"family": "ema", "symbol": "BTCUSD", "live": {"params": {"fast": 9, "slow": 21, "sl_atr": 1.0, "tp_r": 1.0, "max_hold": 24},
           "plan": "trail"}, "live_score": 2.3, "wf": {"usd_day": 1.0}, "holdout": {"usd_day": 0.0}, "wf_random": None}
    ch = L.retune({}, [new], tmp_path / "m5_bots.json")
    assert ch == []                                                           # 2.3 < 2.0 + 0.5 → คงชุดเดิม
    ch = L.retune({}, [{**new, "live_score": 3.0}], tmp_path / "m5_bots.json")
    assert ch and ch[0]["new"]["params"]["fast"] == 9


def test_live_report_stats_and_conclusions(monkeypatch, tmp_path: Path) -> None:
    from bot import live_report as LR
    from bot import shadow as SH
    monkeypatch.setattr(LR, "MD_PATH", tmp_path / "r.md")
    monkeypatch.setattr(LR, "JSON_PATH", tmp_path / "r.json")
    monkeypatch.setattr(SH, "EPISODES_PATH", tmp_path / "eps.jsonl")
    monkeypatch.setattr(C, "load_m5_bots", lambda: [{"bot": "m5_8", "family": "rsi2", "wf_usd_trade": 1.0, "params": {}}])
    base = {"bot": "m5_8", "family": "rsi2", "session": "asia", "trend": "against", "gate_sig": "|asia", "gate_mode": "observe",
            "open_time": "2026-10-18T03:00:00+00:00"}
    DS.append([{**base, "ticket": i, "profit": -5.0 + (i % 3), "regime": "trend", "gate_flag": i % 2} for i in range(40)])
    r = LR.write()
    assert r["overall"]["n"] == 40 and r["overall"]["sig"] == "ลบชัด"
    assert r["by_family"]["rsi2"]["research_usd_trade"] == 1.0
    assert any("ต่างจากที่งานวิจัยคาด" in c for c in r["conclusions"])        # จริง ~-4 vs คาด +1
    assert r["gates"][0]["would_block"]["n"] == 20
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "## ข้อสรุป" in md and "เอเชีย" in md
    assert LR.wilson(60, 100)[0] > 49 and LR.telegram_summary(r).startswith("📑")


def test_report_page_and_dataset_route_private_only(monkeypatch, tmp_path: Path) -> None:
    from bot import live_report as LR
    from bot.monitor import server
    monkeypatch.setattr(LR, "MD_PATH", tmp_path / "r.md")
    (tmp_path / "r.md").write_text("# หัว\n\n## ข้อสรุป\n- **ตัวหนา** `x`\n", encoding="utf-8")
    page = server.report_page().decode()
    assert "<h2>ข้อสรุป</h2>" in page and "<b>ตัวหนา</b>" in page and "<code>x</code>" in page
    import inspect
    src = inspect.getsource(server.make_handler)
    pub = src[src.index("def _public_get"):src.index("def do_POST")]
    assert "/report" in pub and "dataset.csv" not in pub
