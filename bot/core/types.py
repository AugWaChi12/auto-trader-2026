"""
types.py — ชนิดข้อมูลกลางที่ทุกส่วนของระบบใช้ร่วมกัน

หลักเดียวกับ ItemEntity / PickupReply ในโปรเจค PUBG:
ข้อมูลที่วิ่งระหว่างเลเยอร์เป็น dataclass แบบ frozen → แชร์ข้ามเธรดได้โดยไม่ต้อง lock

ข้อตกลงเรื่องราคา:
  - Bar จาก MT5 เป็นราคา "bid" เสมอ
  - ฝั่ง BUY เข้าที่ ask (= bid + spread) และออกที่ bid
  - ฝั่ง SELL เข้าที่ bid และออกที่ ask
  - เวลาใน Bar เป็น UTC (แปลงจากเวลา server ตอนโหลดข้อมูลแล้ว) และเป็นเวลา "เปิด" แท่ง
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, IntEnum


class Side(IntEnum):
    BUY = 1
    SELL = -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class Bar:
    time: datetime        # เวลาเปิดแท่ง (UTC, tz-aware)
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0   # สเปรดของแท่งนี้ หน่วยราคา (ไม่ใช่ points) — 0 = ไม่ทราบ
    volume: float = 0.0


@dataclass(frozen=True)
class SymbolSpec:
    """สเปคสัญญา — ค่าจริงต่างกันตามโบรก ให้ดึงจาก mt5.symbol_info() ทับค่าเริ่มต้นเสมอ"""
    name: str
    point: float              # ราคาขยับขั้นต่ำ
    contract_size: float      # จำนวนหน่วยต่อ 1 lot (XAUUSD ส่วนใหญ่ = 100 oz)
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    min_spread: float = 0.0   # สเปรดขั้นต่ำที่ใช้ใน backtest (หน่วยราคา) ถ้าข้อมูลแท่งไม่มี
    slippage: float = 0.0     # slippage ต่อการเข้า/ออกแบบ market (หน่วยราคา)
    commission_per_lot: float = 0.0  # ค่าคอมไป-กลับต่อ 1 lot (USD)
    margin_rate: float = 0.01         # margin = notional * margin_rate (1:100 = 0.01)
    # swap เป็นอัตราต่อปีของมูลค่าไม้ (เช่น −0.061 = จ่าย 6.1%/ปี, บวก = ได้รับ) — 0 = ไม่คิด
    # ต้องเป็น % ไม่ใช่ USD ตายตัว: ใช้ย้อนหลังกับราคาต่างยุคได้ถูก (ทอง 2008 $900 vs 2026 $4,100)
    swap_long_rate: float = 0.0
    swap_short_rate: float = 0.0
    # ต้นทุนถือที่เปลี่ยนตามปี: ((ปี, long_rate, short_rate), ...) — ถ้ามี ใช้แทน swap_*_rate (research)
    carry: tuple = ()
    # ระยะ SL/TP ขั้นต่ำจากราคา (หน่วยราคา) — โบรกปฏิเสธถ้าใกล้กว่านี้ (FxPro BITCOIN = $200)
    min_stop: float = 0.0

    def round_volume_down(self, volume: float) -> float:
        """ปัดลงให้ตรง volume_step (ปัดลงเสมอ → ไม่มีวันเสี่ยงเกินที่คำนวณ)"""
        steps = math.floor(volume / self.volume_step + 1e-9)
        return round(min(steps * self.volume_step, self.volume_max), 8)

    def pnl(self, side: Side, entry: float, exit_: float, volume: float) -> float:
        """กำไร/ขาดทุนเป็น USD — สมมติ symbol ที่ quote เป็น USD และบัญชีเป็น USD"""
        return (exit_ - entry) * int(side) * volume * self.contract_size


class RejectReason(Enum):
    NONE = "none"
    NO_MONEY = "no_money"            # margin ไม่พอ — เหมือน InventoryFull: รอจน equity เปลี่ยน
    MARKET_CLOSED = "market_closed"
    PRICE_CHANGED = "price_changed"  # requote / off quotes — ลองใหม่ได้เร็ว
    INVALID_STOPS = "invalid_stops"  # SL/TP ผิดฝั่งหรือใกล้เกิน — สัญญาณนี้ใช้ไม่ได้แล้ว (เหมือน Gone)
    INVALID_VOLUME = "invalid_volume"
    TRADE_DISABLED = "trade_disabled"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    volume: float
    sl: float
    tp: float | None
    tag: str                  # ชื่อกลยุทธ์
    group_id: str             # ไม้ที่แตกจากสัญญาณเดียวกันใช้ group เดียวกัน
    role: str = "single"      # "single" | "partial" | "runner"


@dataclass(frozen=True)
class OrderReply:
    accepted: bool
    reason: RejectReason = RejectReason.NONE
    ticket: int | None = None
    fill_price: float | None = None
    message: str = ""


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    sl: float
    tp: float | None
    open_time: datetime
    tag: str
    group_id: str
    role: str
    initial_sl: float
    # สถานะการบริหารไม้ (PositionManager แก้ไข)
    moved_to_be: bool = False
    best_price: float = field(default=0.0)   # ราคาที่ดีที่สุดที่เคยไปถึง (ใช้คิด MFE)
    swap: float = 0.0                        # swap สะสม (USD) — backtest เท่านั้น
    swap_date: object = None                 # วันที่คิด swap ล่าสุด

    def __post_init__(self) -> None:
        if not self.best_price:
            self.best_price = self.entry_price

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_sl)


@dataclass(frozen=True)
class ClosedTrade:
    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    exit_price: float
    open_time: datetime
    close_time: datetime
    pnl: float                # รวมค่าคอมแล้ว
    risk_amount: float        # เงินที่เสี่ยงตอนเปิด (|entry - initial_sl| * vol * contract)
    exit_reason: str          # "sl" | "tp" | "be" | "trail" | "exit" | "halt" | "eod"
    tag: str
    group_id: str
    role: str

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.risk_amount if self.risk_amount > 0 else 0.0


@dataclass(frozen=True)
class EnterSignal:
    """สัญญาณเข้า — กลยุทธ์บอกแค่ทิศและจุด SL/TP ส่วนขนาดไม้เป็นหน้าที่ RiskManager"""
    symbol: str
    side: Side
    sl: float
    tp: float | None           # None = ไม่มีเป้าตายตัว ปล่อยวิ่งด้วย trailing
    tag: str
    key: str                   # idempotency key: สัญญาณเดียวกันห้ามส่งซ้ำ (เช่น "xau_bo:2025-01-15:BUY")
    reason: str = ""


@dataclass(frozen=True)
class ExitSignal:
    """สั่งปิดทุกไม้ของกลยุทธ์นี้บน symbol นี้ (เช่น ถึงเวลาปิดสิ้นวัน)"""
    symbol: str
    tag: str
    reason: str = "exit"


Signal = EnterSignal | ExitSignal


TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def timeframe_delta(tf: str) -> timedelta:
    return timedelta(minutes=TIMEFRAME_MINUTES[tf])
