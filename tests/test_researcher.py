"""researcher.py: รายงานเฉพาะช่องที่ไม้พอ t ถึงเกณฑ์ และทิศเดียวกันทั้งสองครึ่งของข้อมูล"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot import researcher as R


def _rows(profit_of) -> list[dict]:  # noqa: ANN001
    t0 = datetime(2026, 10, 22, 7, tzinfo=timezone.utc)
    out = []
    for i in range(80):
        europe = i % 2 == 0
        out.append({"open_time": (t0 + timedelta(hours=i)).isoformat(), "symbol": "XAUUSD", "family": "ema", "regime": "range",
                    "session": "europe" if europe else "us", "trend": "flat", "side": "BUY", "profit_std": profit_of(i, europe)})
    return out


def test_scan_finds_a_steady_weak_spot_in_both_halves() -> None:
    r = R.scan(_rows(lambda i, europe: (-10.0 - i % 3) if europe else (5.0 + i % 3)))
    assert any(c["cell"] == "session=europe" and c["t"] < -2 for c in r["weak"])
    assert any(c["cell"] == "session=us" for c in r["strong"])
    assert "Researcher" in R.summary_text(r)


def test_scan_ignores_a_cell_that_flips_between_halves() -> None:
    r = R.scan(_rows(lambda i, europe: ((-10.0 - i % 3) if i < 40 else (10.0 + i % 3)) if europe else (i % 3 - 1.0)))
    assert not any(c["cell"] == "session=europe" for c in r["weak"] + r["strong"])
