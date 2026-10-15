"""
timeutil.py — แปลงเวลา server ของโบรก ↔ UTC และช่วยเช็ค session

ปัญหา: MT5 ส่งเวลาแท่งเป็น "เวลา server" ของโบรก ไม่ใช่ UTC
โบรก XAUUSD ส่วนใหญ่ใช้ธรรมเนียม "New York close":
    server time = เวลานิวยอร์ก + 7 ชั่วโมง
    → GMT+2 ช่วงหน้าหนาว, GMT+3 ช่วง US daylight saving
    → แท่ง D1 เริ่มเที่ยงคืน server = 17:00 นิวยอร์ก (ตลาดปิดรอบวันพอดี)
สังเกต: เปลี่ยน offset ตามวัน DST ของ "อเมริกา" ไม่ใช่ยุโรป
ถ้าใช้ offset คงที่ จะมี 2–3 สัปดาห์ต่อปีที่กรอบเวลาเลื่อนไป 1 ชั่วโมง

โหมดที่รองรับ:
    "ny_close"  — server = America/New_York + 7h  (ค่าเริ่มต้น)
    "fixed:+3"  — offset คงที่ (ชั่วโมง)
ตัวดาวน์โหลดจะตรวจ offset จริงของโบรกตอนนี้แล้วเตือนถ้าไม่ตรงกับโหมดที่เลือก
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def server_offset(server_naive: datetime, mode: str) -> timedelta:
    """offset ของเวลา server เทียบกับ UTC ณ เวลานั้น (server - UTC)"""
    if mode == "ny_close":
        ny_naive = server_naive - timedelta(hours=7)
        ny_offset = ny_naive.replace(tzinfo=NEW_YORK).utcoffset()
        assert ny_offset is not None
        return ny_offset + timedelta(hours=7)
    if mode.startswith("fixed:"):
        return timedelta(hours=float(mode.split(":", 1)[1]))
    raise ValueError(f"unknown server time mode: {mode}")


def server_to_utc(server_naive: datetime, mode: str) -> datetime:
    return (server_naive - server_offset(server_naive, mode)).replace(tzinfo=UTC)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def minutes_of_day(t: time) -> int:
    return t.hour * 60 + t.minute


class SessionClock:
    """แปลงเวลาแท่ง (UTC) เป็นเวลาท้องถิ่นของ session ที่กลยุทธ์สนใจ

    ทองใช้ UTC (กรอบเอเชีย 00:00–07:00 UTC)
    NAS100 ใช้ America/New_York (เปิดตลาด 09:30 ตามเวลานิวยอร์ก ไม่ว่าจะ DST หรือไม่)
    """

    def __init__(self, tz: str) -> None:
        self.tz = ZoneInfo(tz)

    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.tz)
