"""news_scout.py + ปฏิทินสดของ risk/news.py"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config as C
from bot import news_scout as N
from bot.risk.news import NewsCalendar

FEED = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},
        {"title": "Core CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},
        {"title": "Fed Chair Speaks", "country": "USD", "date": "2026-11-12T12:00:00-05:00", "impact": "High"},
        {"title": "GDP", "country": "EUR", "date": "2026-11-12T05:00:00-05:00", "impact": "High"},
        {"title": "Retail Sales", "country": "USD", "date": "2026-11-13T08:30:00-05:00", "impact": "Medium"}]


def test_refresh_keeps_high_usd_numbers_and_merges_same_time(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.setattr(C, "NEWS_EVENTS_CSV", tmp_path / "e.csv")
    assert N.refresh(FEED) == 1
    assert N.read_events() == {"2026-11-12 08:30": "CPI m/m + Core CPI m/m"}
    assert N.refresh(FEED) == 0


def test_upcoming_and_today_text_use_bangkok_time(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.setattr(C, "NEWS_EVENTS_CSV", tmp_path / "e.csv")
    N.refresh(FEED)
    now = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    assert N.upcoming(24, now)[0]["in_hours"] == 7.5
    assert "20:30 CPI" in N.today_text(now)
    assert N.upcoming(24, now + timedelta(days=2)) == []


def test_calendar_picks_up_new_events_without_restart(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    path = tmp_path / "e.csv"
    monkeypatch.setattr(C, "NEWS_EVENTS_CSV", path)
    cal = NewsCalendar([], timedelta(minutes=30), path)
    t = datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc)
    assert not cal.in_blackout(t)
    N.refresh(FEED)
    assert cal.in_blackout(t) and cal.in_blackout(t + timedelta(minutes=20)) and not cal.in_blackout(t + timedelta(hours=1))
