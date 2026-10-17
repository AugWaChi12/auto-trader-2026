"""
settings.py — ค่าที่ผู้ใช้ปรับเองได้จากหน้าเว็บ (/settings) สำหรับบอททดสอบ m5_* เท่านั้น — main ไม่อ่านไฟล์นี้

เก็บที่ state/bot_settings.json · config.py อ่านตอน import → บอทต้องรีสตาร์ทถึงจะใช้ค่าใหม่
(หน้าเว็บกดบันทึกแล้วสั่ง restart_bots.ps1 ให้เอง)
"""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "state" / "bot_settings.json"

TIMEFRAMES = ("M1", "M5", "M15")
DEFAULTS: dict = {
    "tf": "M5",                 # timeframe ของบอททดสอบทั้ง 9 ตัว
    "btc_lot": 0.17,            # ไม้ตายตัวของ BTC
    "daily_target": 120.0,      # ได้เท่านี้ในวันนั้น → ปิดทุกไม้แล้วพัก (0 = ไม่หยุด)
    "widen_stops": False,       # SL ใกล้กว่าระยะขั้นต่ำของโบรก ($200 สำหรับ BTC): False = ข้ามสัญญาณ, True = ขยาย SL ไปที่ขั้นต่ำ
}
LIMITS = {"btc_lot": (0.01, 1.0), "daily_target": (0.0, 5000.0)}


def validate(raw: dict) -> dict:
    """คืนค่าที่ผ่านการตรวจแล้ว (โยน ValueError พร้อมเหตุผลภาษาไทยถ้าผิด)"""
    if not isinstance(raw, dict):
        raise ValueError("รูปแบบข้อมูลไม่ถูกต้อง")
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k not in DEFAULTS:
            raise ValueError(f"ไม่รู้จักค่า {k}")
        out[k] = v
    if out["tf"] not in TIMEFRAMES:
        raise ValueError(f"timeframe ต้องเป็น {', '.join(TIMEFRAMES)}")
    for k, (lo, hi) in LIMITS.items():
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} ต้องเป็นตัวเลข") from None
        if not lo <= out[k] <= hi:
            raise ValueError(f"{k} ต้องอยู่ระหว่าง {lo} ถึง {hi}")
    out["btc_lot"] = round(out["btc_lot"], 2)
    out["widen_stops"] = bool(out["widen_stops"])
    return out


def load() -> dict:
    try:
        return validate(json.loads(PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(values: dict) -> dict:
    v = validate(values)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(v, indent=1), encoding="utf-8")
    return v
