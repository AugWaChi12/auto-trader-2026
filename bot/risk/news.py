"""
news.py — ช่วงงดเทรดรอบข่าวแรง (NFP / CPI / FOMC)

ทองกับ NAS100 กระชากหลายสิบดอลลาร์/หลายร้อยจุดในไม่กี่วินาทีตอนข่าวออก
สเปรดถ่างและ slippage หนัก → backtest จากแท่ง M15 ประเมินช่วงนี้ไม่ได้อยู่แล้ว ให้หลบดีกว่า

แหล่งข้อมูล:
  1) NFP: คำนวณจากกฎ "ศุกร์แรกของเดือน 08:30 นิวยอร์ก" (เป็นค่าประมาณ บางเดือนเลื่อน)
  2) ไฟล์ CSV (config/news.csv) คอลัมน์: time_ny,event  เช่น  2025-01-15 08:30,CPI
     ใส่ CPI / FOMC / อื่นๆ เพิ่มเองได้ — ไฟล์ในโปรเจคมีวัน FOMC ไว้ให้แล้ว (ควรตรวจกับ federalreserve.gov)
"""
from __future__ import annotations

import bisect
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core.timeutil import NEW_YORK, UTC


def nfp_times(start_year: int, end_year: int) -> list[datetime]:
    out = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            d = date(y, m, 1)
            d += timedelta(days=(4 - d.weekday()) % 7)  # ศุกร์แรก
            out.append(datetime(d.year, d.month, d.day, 8, 30, tzinfo=NEW_YORK).astimezone(UTC))
    return out


def load_csv(path: Path) -> list[datetime]:
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("time_ny") or row["time_ny"].startswith("#"):
                continue
            t = datetime.strptime(row["time_ny"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=NEW_YORK)
            out.append(t.astimezone(UTC))
    return out


class NewsCalendar:
    def __init__(self, events: list[datetime], window: timedelta = timedelta(minutes=30)) -> None:
        self._events = sorted(events)
        self.window = window

    @classmethod
    def default(cls, csv_path: Path | None, years: tuple[int, int], window_min: int = 30) -> "NewsCalendar":
        events = nfp_times(*years)
        if csv_path and csv_path.exists():
            events += load_csv(csv_path)
        return cls(events, timedelta(minutes=window_min))

    def in_blackout(self, t: datetime) -> bool:
        i = bisect.bisect_left(self._events, t - self.window)
        return i < len(self._events) and self._events[i] <= t + self.window
