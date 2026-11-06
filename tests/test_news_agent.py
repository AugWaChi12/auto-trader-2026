"""news_agent.py: paper เท่านั้น · รับเฉพาะ JSON ที่ถูกรูปแบบ · ให้คะแนนจากราคาจริง"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from bot import news_agent as A

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp()


def test_titles_strip_cdata_and_skip_channel_title() -> None:
    xml = "<rss><title>Feed</title><item><title><![CDATA[Gold &amp; silver rise]]></title></item></rss>"
    assert A.titles(xml) == ["Gold & silver rise"]


def test_parse_rejects_bad_shapes_and_clamps_confidence() -> None:
    assert A.parse("not json") == []
    bad = {"calls": [{"symbol": "EVIL", "view": "bull", "confidence": 90}, {"symbol": "XAUUSD", "view": "buy now", "confidence": 90}]}
    assert A.parse(json.dumps(bad)) == []
    assert A.parse(json.dumps({"calls": [{"symbol": "XAUUSD", "view": "bear", "confidence": 500}]}))[0]["confidence"] == 100


def test_scan_records_only_directional_calls_with_open_market(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.setattr(A, "PATH", tmp_path / "c.jsonl")
    reply = {"calls": [{"symbol": "XAUUSD", "view": "bull", "confidence": 80, "reason": "x"},
                       {"symbol": "NAS100", "view": "neutral", "confidence": 10, "reason": "y"},
                       {"symbol": "BTCUSD", "view": "bear", "confidence": 60, "reason": "z"}]}
    made = A.scan(lambda s: None if s == "BTCUSD" else 4000.0, NOW, lambda p: json.dumps(reply), {"gold": ["headline"]})
    assert [c["symbol"] for c in made] == ["XAUUSD"] and A.recent()[0]["view"] == "bull"
    assert A.scan(lambda s: 1.0, NOW, lambda p: "{}", {}) == []


def test_evaluate_scores_after_four_hours_net_of_cost(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.setattr(A, "PATH", tmp_path / "c.jsonl")
    A.scan(lambda s: 4000.0, NOW, lambda p: json.dumps({"calls": [{"symbol": "XAUUSD", "view": "bear", "confidence": 75}]}), {"g": ["h"]})
    t0 = datetime.fromtimestamp(NOW, timezone.utc)
    bars = [NS(time=t0 + timedelta(hours=h), close=4000.0 - 5 * h) for h in range(6)]
    assert A.evaluate(lambda *a: bars, NOW + 3600) == 0
    assert A.evaluate(lambda *a: bars, NOW + 5 * 3600) == 1
    b = A.scoreboard(now=NOW + 5 * 3600)
    assert b["high_confidence"]["n"] == 1 and b["all"]["avg_usd_4h"] == 78.3 and b["all"]["hit_pct"] == 100
