"""
news_scout.py — ทีมงาน News Scout: ดึงปฏิทินข่าวแรงของสหรัฐฯ ให้ระบบหลบไม้และให้ผู้จัดการรู้ล่วงหน้า

  · ทุก REFRESH_H ชม.: ปฏิทินสัปดาห์นี้ (Forex Factory) → state/news_events.csv (เก็บสะสม รูปแบบเดียวกับ config/news.csv)
  · บอทอ่านไฟล์นี้เองเมื่อไฟล์เปลี่ยน (risk/news.py) ไม่ต้องรีสตาร์ท
  · ดึงไม่ได้ = ใช้ตารางเดิมต่อ · NFP ยังคำนวณเองและ FOMC ยังอยู่ใน config/news.csv
"""
from __future__ import annotations

import csv
import json
import logging
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from . import config as C
from .core.timeutil import NEW_YORK, UTC

log = logging.getLogger("news_scout")
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_H = 6
SKIP = ("speaks", "election", "holiday", "bank holiday", "daylight")     # ไม่มีเวลาตัวเลขออกแน่นอน หรือไม่ใช่ข่าวตัวเลข


def keep(item: dict) -> bool:
    title = (item.get("title") or "").lower()
    return item.get("country") == "USD" and item.get("impact") == "High" and not any(w in title for w in SKIP)


def fetch() -> list[dict]:
    req = urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:      # noqa: S310 — URL ตายตัว
        return json.loads(r.read().decode("utf-8"))


def to_rows(items: list[dict]) -> dict[str, str]:
    """{เวลานิวยอร์ก: ชื่อข่าว} · ข่าวเวลาเดียวกันรวมเป็นแถวเดียว"""
    out: dict[str, str] = {}
    for x in filter(keep, items):
        t = datetime.fromisoformat(x["date"]).astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M")
        out[t] = f"{out[t]} + {x['title']}" if t in out else x["title"]
    return out


def read_events() -> dict[str, str]:
    try:
        with C.NEWS_EVENTS_CSV.open(newline="", encoding="utf-8") as f:
            return {r["time_ny"]: r["event"] for r in csv.DictReader(f)}
    except (OSError, KeyError):
        return {}


def refresh(items: list[dict] | None = None) -> int:
    """คืนจำนวนข่าวใหม่ · ไฟล์ไม่เปลี่ยนถ้าไม่มีข่าวใหม่ (บอทจะได้ไม่โหลดซ้ำ)"""
    known = read_events()
    fresh = to_rows(fetch() if items is None else items)
    merged = {**known, **fresh}
    if merged == known:
        return 0
    from .live.fileio import atomic_write
    lines = ["time_ny,event"] + [f'{t},"{e.replace(chr(34), "")}"' for t, e in sorted(merged.items())]
    atomic_write(C.NEWS_EVENTS_CSV, "\n".join(lines) + "\n")
    return len(set(merged) - set(known))


def upcoming(hours: float = 24, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    out = []
    for t, e in sorted(read_events().items()):
        at = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=NEW_YORK).astimezone(UTC)
        if now <= at <= now + timedelta(hours=hours):
            out.append({"at_utc": at.isoformat(timespec="minutes"), "in_hours": round((at - now).total_seconds() / 3600, 1), "event": e})
    return out


def plain(event: str) -> str:
    low = event.lower()
    for key, word in (("non-farm", "ตัวเลขจ้างงาน (NFP)"), ("cpi", "เงินเฟ้อ (CPI)"), ("pce", "เงินเฟ้อ (PCE)"), ("fomc", "ประชุมเฟด"),
                      ("federal funds", "ประชุมเฟด"), ("gdp", "GDP"), ("retail sales", "ยอดค้าปลีก"), ("ppi", "เงินเฟ้อผู้ผลิต (PPI)")):
        if key in low:
            return word
    return event.split(" + ")[0]


def today_text(now: datetime | None = None) -> str:
    ev = upcoming(24, now)
    if not ev:
        return ""
    bkk = [f"{(datetime.fromisoformat(x['at_utc']) + timedelta(hours=7)):%H:%M} {plain(x['event'])}" for x in ev]
    return f"📅 ข่าวแรงวันนี้: {' · '.join(bkk)}\nบอทหยุดเข้าไม้ใหม่ ±{C.NEWS_BLACKOUT_MIN} นาที"
