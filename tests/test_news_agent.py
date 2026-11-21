"""news_agent.py: paper เท่านั้น · รับเฉพาะ JSON ที่ถูกรูปแบบ · ถามเฉพาะเมื่อมีข่าวใหม่ · ให้คะแนนเหนือการตามเทรนด์"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot import news_agent as A

NOW_DT = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp()


@pytest.fixture(autouse=True)
def _paths(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.setattr(A, "PATH", tmp_path / "c.jsonl")
    monkeypatch.setattr(A, "SEEN_PATH", tmp_path / "seen.json")


def test_titles_read_rss_items_with_time_and_strip_cdata() -> None:
    xml = ("<rss><channel><title>Feed</title><item><title><![CDATA[Gold &amp; silver rise]]></title>"
           "<pubDate>Mon, 09 Nov 2026 11:30:00 GMT</pubDate></item></channel></rss>")
    assert A.titles(xml) == [("Gold & silver rise", datetime(2026, 11, 9, 11, 30, tzinfo=timezone.utc))]
    atom = "<feed><title>r/Gold</title><entry><title>Buy now?</title><updated>2026-11-09T10:00:00+00:00</updated></entry></feed>"
    assert A.titles(atom)[0][0] == "Buy now?"


def test_parse_rejects_bad_shapes_and_calls_without_a_headline() -> None:
    assert A.parse("not json") == []
    bad = {"calls": [{"symbol": "EVIL", "view": "bull", "confidence": 90, "trigger": "x"},
                     {"symbol": "XAUUSD", "view": "buy now", "confidence": 90, "trigger": "x"},
                     {"symbol": "NAS100", "view": "bear", "confidence": 90, "trigger": ""}]}
    assert A.parse(json.dumps(bad)) == []
    c = A.parse(json.dumps({"calls": [{"symbol": "XAUUSD", "view": "bear", "confidence": 500, "trigger": "CPI hot", "vs_trend": "x"}]}))[0]
    assert c["confidence"] == 100 and c["vs_trend"] == "none"


def _news(*ages_min: int) -> dict:
    return {"gold": [(f"headline {m}", NOW_DT - timedelta(minutes=m)) for m in ages_min]}


def test_scan_sends_fresh_news_with_moves_and_skips_when_nothing_new() -> None:
    asked = []
    reply = {"calls": [{"symbol": "XAUUSD", "view": "bull", "confidence": 80, "trigger": "headline 10", "vs_trend": "against", "reason": "x"},
                       {"symbol": "NAS100", "view": "neutral", "confidence": 10, "reason": "y"},
                       {"symbol": "BTCUSD", "view": "bear", "confidence": 60, "trigger": "headline 10", "reason": "z"}]}

    def ask(p: str) -> str:
        asked.append(p)
        return json.dumps(reply)
    made = A.scan(lambda s: None if s == "BTCUSD" else 4000.0, NOW, ask, _news(10, 600), prev_of=lambda s: 4010.0)
    assert [c["symbol"] for c in made] == ["XAUUSD"] and made[0]["price_prev"] == 4010.0
    assert "headline 10" in asked[0] and "headline 600" not in asked[0] and "XAUUSD: -0.25%" in asked[0]
    assert A.scan(lambda s: 4000.0, NOW, ask, _news(10, 600)) == [] and len(asked) == 1      # ข่าวเดิม = ไม่ถามซ้ำ


def test_evaluate_scores_excess_over_following_the_trend() -> None:
    reply = {"calls": [{"symbol": "XAUUSD", "view": "bear", "confidence": 75, "trigger": "h", "vs_trend": "against"}]}
    A.scan(lambda s: 4000.0, NOW, lambda p: json.dumps(reply), _news(5), prev_of=lambda s: 3990.0)   # ราคาเพิ่งขึ้น แต่ทายลง
    bars = [NS(time=NOW_DT + timedelta(hours=h), close=4000.0 - 5 * h) for h in range(6)]
    assert A.evaluate(lambda *a: bars, NOW + 3600) == 0
    assert A.evaluate(lambda *a: bars, NOW + 5 * 3600) == 1
    r = A._load()[0]
    assert r["usd_4h"] == 78.3 and r["base_4h"] == -81.7 and r["excess_4h"] == 160.0
    b = A.scoreboard(now=NOW + 5 * 3600)
    assert b["against_trend"]["n"] == 1 and b["all"]["avg_excess_4h"] == 160.0
