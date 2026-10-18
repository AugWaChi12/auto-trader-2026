"""
synthetic.py — สร้างแท่งราคาสังเคราะห์ ไว้ทดสอบว่า "ระบบ" ทำงานครบวงจร

!!! ผล backtest บนข้อมูลนี้ไม่มีความหมายทางการเทรดเลย !!!
เป็น random walk ที่ไม่มี edge ใดๆ — ใช้ตรวจว่าโค้ดไม่พัง ไม่ใช่ตรวจว่ากลยุทธ์ดี
(ถ้ากลยุทธ์ "กำไร" บนข้อมูลนี้ แปลว่ามีบั๊กหรือโชคช่วย)

มีรูปแบบความผันผวนตามช่วงเวลาแบบคร่าวๆ: เอเชียเงียบ, ลอนดอน/นิวยอร์กผันผวน
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..core.timeutil import UTC

# (ราคาเริ่ม, ความผันผวนต่อแท่ง M15 ช่วงปกติ, สเปรด)
PROFILES = {
    "XAUUSD": (2000.0, 1.6, 0.25),
    "NAS100": (16000.0, 12.0, 1.5),
}


def _hour_vol(hour: int) -> float:
    if hour < 7:
        return 0.45
    if hour < 12:
        return 1.1
    if hour < 17:
        return 1.5
    if hour < 21:
        return 0.9
    return 0.4


def generate(symbol: str, start: datetime, days: int, tf_minutes: int = 15, seed: int = 1,
             drift: float = 0.15) -> pd.DataFrame:
    """drift = ความแรงของ "ทิศรายวัน" (0 = random walk ล้วน ใช้ตรวจ look-ahead bias: ทุกกลยุทธ์ต้องได้ ~ -ต้นทุน)"""
    price0, vol, spread = PROFILES[symbol]
    rng = np.random.default_rng(seed + sum(map(ord, symbol)))  # hash() สุ่มต่อโปรเซส ใช้ไม่ได้
    step = timedelta(minutes=tf_minutes)
    scale = (tf_minutes / 15) ** 0.5

    rows = []
    price = price0
    t = start.replace(tzinfo=UTC)
    end = t + timedelta(days=days)
    day_drift = 0.0
    current_day = None
    while t < end:
        if t.weekday() >= 5 or (t.weekday() == 4 and t.hour >= 21):
            t += step
            continue
        if t.date() != current_day:
            current_day = t.date()
            day_drift = rng.normal(0, drift) * vol   # บางวันมีทิศ บางวันไม่มี
        sigma = vol * _hour_vol(t.hour) * scale
        o = price
        moves = rng.normal(day_drift * _hour_vol(t.hour) / 4, sigma / 2, size=4)
        path = o + np.cumsum(moves)
        c = float(path[-1])
        h = float(max(o, path.max())) + abs(rng.normal(0, sigma * 0.15))
        l = float(min(o, path.min())) - abs(rng.normal(0, sigma * 0.15))
        rows.append((t, o, h, l, c, spread, 100.0))
        price = c
        t += step
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread", "volume"])
