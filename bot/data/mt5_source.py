"""
mt5_source.py — ดึงแท่งราคาย้อนหลังและสเปคสัญญาจาก MetaTrader 5

ต้องมี MT5 terminal ติดตั้งในเครื่องและล็อกอินบัญชี (demo ก็ได้) ไว้แล้ว
library MetaTrader5 จะเปิด/เชื่อมต่อ terminal ให้เอง

ข้อควรรู้:
  - ชื่อ symbol ต่างกันตามโบรก (NAS100 / USTEC / US100 / NAS100.cash ...)
    → find_symbol() ลองชื่อในรายการ alias แล้วค่อยหาแบบมี suffix
  - ประวัติที่ดึงได้ถูกจำกัดด้วย "Max bars in chart" ของ terminal
    (Tools → Options → Charts) ถ้าได้ข้อมูลน้อยกว่าที่ขอ ให้ตั้งเป็น Unlimited แล้วรีสตาร์ท MT5
  - เวลาแท่งเป็นเวลา server → แปลงเป็น UTC ด้วย timeutil.server_to_utc
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from ..core.timeutil import UTC, server_offset, server_to_utc
from ..core.types import SymbolSpec
from .store import bars_path, save_bars, save_spec

log = logging.getLogger(__name__)

SYMBOL_ALIASES: dict[str, list[str]] = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.r"],
    "NAS100": ["NAS100", "#USNDAQ100", "USTEC", "US100", "NDX100", "NAS100.cash", "US100.cash", "USTECm"],
    "XAGUSD": ["XAGUSD", "SILVER"],
    "XPTUSD": ["XPTUSD", "PLATINUM"],
    "WTI": ["WTI", "USOIL", "XTIUSD"],
    "BRENT": ["BRENT", "UKOIL", "XBRUSD"],
    "NATGAS": ["NAT.GAS", "NATGAS", "XNGUSD"],
    "US30": ["#US30", "US30", "DJ30"],
    "SPX500": ["#USSPX500", "US500", "SPX500"],
    "BTCUSD": ["BTCUSD", "BITCOIN", "BTCUSDm"],
}


MAX_POS_BARS = 99_990   # ต้องน้อยกว่า "Max bars in chart" (100,000) ไม่งั้น MT5 ตอบ Invalid params


def _mt5():
    from ..mt5api import load      # Windows: MetaTrader5 ตัวจริง · Linux: MT5 ใน Docker (MT5_HOST)
    return load()


def _find_terminal() -> str | None:
    """หา terminal64.exe ใน Program Files (MT5 ของโบรกมักอยู่ในโฟลเดอร์ชื่อโบรก เช่น "FxPro Markets MT5")"""
    if os.environ.get("MT5_PATH"):          # ตั้งเอง / Docker (path ฝั่ง Wine ไม่มีในเครื่องนี้ ห้ามเช็กว่ามีไฟล์)
        return os.environ["MT5_PATH"]
    for root in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")),):
        found = sorted(root.glob("*/terminal64.exe"))
        if len(found) > 1:
            log.warning("พบ MT5 หลายตัว %s — ใช้ตัวแรก (ตั้ง MT5_PATH เพื่อเลือกเอง)", [str(f) for f in found])
        if found:
            return str(found[0])
    return None


def connect() -> None:
    """เชื่อมต่อ terminal — ถ้า MT5 ของโบรกติดตั้งในโฟลเดอร์ชื่อโบรก library จะหาเองไม่เจอ
    ให้ตั้ง env MT5_PATH เป็น path ของ terminal64.exe"""
    mt5 = _mt5()
    path = os.environ.get("MT5_PATH") or _find_terminal()
    ok = mt5.initialize(path=path) if path else mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()} — ติดตั้ง/เปิด MT5 และล็อกอินบัญชีก่อน "
                           f"(ถ้าติดตั้งในโฟลเดอร์อื่น ให้ตั้ง MT5_PATH=...\terminal64.exe)")
    info = mt5.account_info()
    term = mt5.terminal_info()
    log.info("MT5 connected: server=%s login=%s company=%s", info.server if info else "?",
             info.login if info else "?", term.company if term else "?")


def find_symbol(canonical: str, mt5=None) -> str:  # noqa: ANN001
    mt5 = mt5 or _mt5()
    names = [s.name for s in (mt5.symbols_get() or [])]
    for cand in SYMBOL_ALIASES.get(canonical, [canonical]):
        if cand in names:
            return cand
    for cand in SYMBOL_ALIASES.get(canonical, [canonical]):
        for n in names:
            if n.upper().startswith(cand.upper()):
                return n
    raise LookupError(f"ไม่พบ symbol {canonical} ในโบรกนี้ (ลองดูชื่อใน Market Watch แล้วเพิ่มใน SYMBOL_ALIASES)")


def detect_offset_hours() -> float | None:
    """offset ของเวลา server ปัจจุบัน (ชั่วโมง) โดยเทียบเวลา tick ล่าสุดกับนาฬิกาเครื่อง

    ใช้ได้เฉพาะตอนตลาดเปิด (tick ล่าสุดต้องใหม่)
    """
    mt5 = _mt5()
    sym = find_symbol("XAUUSD")
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return None
    now_utc = datetime.now(UTC).timestamp()
    diff = tick.time - now_utc
    hours = round(diff / 1800) / 2   # ปัดทีละครึ่งชั่วโมง
    # tick เก่าเกิน (ตลาดปิด) → offset ที่วัดได้เชื่อไม่ได้
    if abs(diff - hours * 3600) > 600:
        return None
    return hours


def check_time_mode(mode: str) -> None:
    measured = detect_offset_hours()
    if measured is None:
        log.warning("ตรวจ offset เวลา server ไม่ได้ (ตลาดอาจปิดอยู่) — ใช้โหมด %s ตามที่ตั้งไว้", mode)
        return
    server_now = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=measured)
    expected = server_offset(server_now, mode).total_seconds() / 3600
    if abs(expected - measured) > 0.01:
        log.error("เวลา server ของโบรกนี้คือ UTC%+g แต่โหมด %s ให้ UTC%+g — แก้ SERVER_TIME_MODE ใน config.py",
                  measured, mode, expected)
        raise SystemExit(2)
    log.info("server time offset UTC%+g ตรงกับโหมด %s", measured, mode)


def fetch_spec(canonical: str, broker_name: str, default: SymbolSpec) -> SymbolSpec:
    mt5 = _mt5()
    info = mt5.symbol_info(broker_name)
    if info is None:
        raise LookupError(f"symbol_info({broker_name}) = None")
    # swap ปัจจุบันของโบรก (USD ต่อ lot ต่อคืน) — แบบระวัง: ฝั่งที่ได้รับนับเป็น 0
    # เพราะดอกเบี้ยในอดีตต่างจากปัจจุบันมาก ใช้ค่าวันนี้ย้อนหลังจะให้ "รายได้ swap" ที่ไม่เคยมีจริง
    # แปลง USD/lot/คืน → อัตราต่อปีของมูลค่าไม้ ด้วยราคาปัจจุบัน
    tick = mt5.symbol_info_tick(broker_name)
    notional = (tick.bid if tick and tick.bid else 0.0) * info.trade_contract_size
    if info.swap_mode in (2, 9) and notional > 0:
        swap_long = min(float(info.swap_long), 0.0) * 365 / notional
        swap_short = min(float(info.swap_short), 0.0) * 365 / notional
    elif info.swap_mode in (5, 6):      # ดอกเบี้ย % ต่อปีของราคา (คริปโต FxPro: long −30%, short −10%)
        swap_long, swap_short = min(float(info.swap_long), 0.0) / 100, min(float(info.swap_short), 0.0) / 100
    else:
        swap_long, swap_short = default.swap_long_rate, default.swap_short_rate
    return SymbolSpec(
        name=canonical, point=info.point, contract_size=info.trade_contract_size,
        volume_min=info.volume_min, volume_step=info.volume_step, volume_max=info.volume_max,
        min_spread=default.min_spread, slippage=default.slippage,
        commission_per_lot=default.commission_per_lot, margin_rate=default.margin_rate,
        swap_long_rate=round(swap_long, 5), swap_short_rate=round(swap_short, 5),
        min_stop=round(info.trade_stops_level * info.point, 8),
    )


def download(canonical: str, tf: str, start: datetime, end: datetime, out: Path,
             time_mode: str, default_spec: SymbolSpec) -> Path:
    mt5 = _mt5()
    broker_name = find_symbol(canonical)
    mt5.symbol_select(broker_name, True)
    timeframe = getattr(mt5, f"TIMEFRAME_{tf}")

    frames = []
    chunk = timedelta(days=90)   # ดึงทีละช่วง ลดโอกาส terminal คืนค่าว่างเมื่อขอช่วงยาว
    t = start
    while t < end:
        t2 = min(t + chunk, end)
        rates = mt5.copy_rates_range(broker_name, timeframe, t, t2)
        if rates is not None and len(rates):
            frames.append(pd.DataFrame(rates))
        t = t2
    # timeframe เล็ก (M1) terminal มักคืนช่วงสั้นผิดปกติเมื่อขอแบบช่วงเวลา → ดึงแบบนับย้อนจากแท่งล่าสุดแทน
    got = sum(len(f) for f in frames)
    if got < 50_000:
        rates = mt5.copy_rates_from_pos(broker_name, timeframe, 1, MAX_POS_BARS)
        if rates is not None and len(rates) > got:
            log.info("%s %s: ช่วงเวลาได้ %d แท่ง → ใช้ copy_rates_from_pos ได้ %d แท่ง", canonical, tf, got, len(rates))
            frames = [pd.DataFrame(rates)]
    if not frames:
        raise RuntimeError(f"ไม่ได้ข้อมูล {broker_name} {tf}: {mt5.last_error()}")

    df = pd.concat(frames).drop_duplicates("time").sort_values("time")
    server_naive = pd.to_datetime(df["time"], unit="s")
    df["time"] = pd.to_datetime([server_to_utc(ts.to_pydatetime(), time_mode) for ts in server_naive], utc=True)
    spec = fetch_spec(canonical, broker_name, default_spec)
    df["spread"] = df["spread"] * spec.point          # MT5 ให้หน่วย points → แปลงเป็นหน่วยราคา
    df["volume"] = df["tick_volume"]

    path = bars_path(out, canonical, tf)
    save_bars(path, df)
    save_spec(out, spec)
    log.info("%s (%s) %s: %d bars %s → %s  saved %s", canonical, broker_name, tf, len(df),
             df["time"].iloc[0], df["time"].iloc[-1], path)
    return path

