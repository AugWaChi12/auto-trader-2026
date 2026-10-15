"""
base.py — สัญญาของกลยุทธ์ + ตัวติดตามกรอบ session ที่ใช้ร่วมกัน

กลยุทธ์มีหน้าที่เดียว: ดูแท่งที่เพิ่งปิด แล้วบอกว่า "ควรเข้า/ออกไหม"
ห้ามคำนวณขนาดไม้ ห้ามส่งออเดอร์เอง ห้ามจำว่าส่งไปแล้วหรือยัง
  - ขนาดไม้        → RiskManager
  - กันส่งซ้ำ       → OrderGuard (ผ่าน EnterSignal.key)
  - เลื่อน SL/trail → PositionManager (ตาม ManagementPlan)

เพราะ OrderGuard กันซ้ำด้วย key ให้แล้ว กลยุทธ์จึงส่งสัญญาณเดิมซ้ำทุกแท่งได้
ตราบที่เงื่อนไขยังจริง → ถ้าแท่งแรกถูกปฏิเสธ (เช่น ติดช่วงข่าว) แท่งถัดไปก็ลองใหม่เอง
หลักเดียวกับ ScanForItems ใน PUBG ที่สแกนเจอของชิ้นเดิมทุกรอบ แต่ queuedIds_ กันไม่ให้เข้าคิวซ้ำ
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from ..core.indicators import DailyRange
from ..core.interfaces import IAccount
from ..core.timeutil import SessionClock, minutes_of_day, parse_hhmm
from ..core.types import TIMEFRAME_MINUTES, Bar, ExitSignal, Signal


@dataclass(frozen=True)
class ManagementPlan:
    """วิธีบริหารไม้หลังเข้าแล้ว (PositionManager อ่านค่านี้)

    partial_r      : แบ่งไม้ออกเป็น 2 ชิ้น ชิ้นแรกมี TP ที่ partial_r * R
                     เมื่อชิ้นแรกถึง TP → เลื่อน SL ของชิ้นที่เหลือไปจุดเข้า (break-even)
                     ถ้า lot เล็กจนแบ่งไม่ได้ → เปิดไม้เดียว แล้วเลื่อน BE เมื่อราคาไปถึง partial_r
    partial_frac   : สัดส่วนของชิ้นแรก
    be_offset_r    : เลื่อน SL ไปที่ entry + offset (หน่วย R) เพื่อให้ครอบคลุมค่าธรรมเนียม
    trail_atr_mult : trailing stop = close ∓ mult * ATR (None = ไม่ trail)
    trail_start_r  : เริ่ม trail เมื่อราคาเคยไปถึงกี่ R
    """
    partial_r: float | None = 1.5
    partial_frac: float = 0.5
    be_offset_r: float = 0.05
    trail_atr_mult: float | None = None
    trail_start_r: float = 1.5


@dataclass(frozen=True)
class SessionWindow:
    """กรอบเวลาของกลยุทธ์ session (เวลาท้องถิ่นตาม tz, ห้ามข้ามเที่ยงคืน)

    [range_start, range_end)  : ช่วงเก็บ high/low ของกรอบ
    [range_end, trade_end)    : ช่วงที่อนุญาตให้เข้าไม้
    flat_time                 : ปิดทุกไม้ของกลยุทธ์นี้เมื่อแท่งปิดถึงเวลานี้
    """
    tz: str
    range_start: str
    range_end: str
    trade_end: str
    flat_time: str


class Strategy(ABC):
    tag: str
    symbol: str
    plan: ManagementPlan

    @abstractmethod
    def on_bar(self, bar: Bar, account: IAccount) -> list[Signal]:
        """เรียกเมื่อแท่งของ self.symbol ปิด"""


class SessionRange:
    """ติดตามกรอบ high/low ของ session ในแต่ละวัน + ADR สำหรับวัดความกว้าง"""

    def __init__(self, window: SessionWindow, timeframe: str, adr_lookback: int = 10) -> None:
        self.window = window
        self.clock = SessionClock(window.tz)
        self.tf_min = TIMEFRAME_MINUTES[timeframe]
        self.daily = DailyRange(adr_lookback)
        self._rs = minutes_of_day(parse_hhmm(window.range_start))
        self._re = minutes_of_day(parse_hhmm(window.range_end))
        self._te = minutes_of_day(parse_hhmm(window.trade_end))
        self._flat = minutes_of_day(parse_hhmm(window.flat_time))
        if not self._rs < self._re <= self._te <= self._flat:
            raise ValueError(f"session window ต้องเรียง range_start < range_end <= trade_end <= flat_time: {window}")

        self.day: date | None = None
        self.high = float("-inf")
        self.low = float("inf")
        self.range_bars = 0
        self.range_open: float | None = None   # ราคาเปิดของแท่งแรกในกรอบ
        # สถานะของแท่งล่าสุด
        self.open_min = 0
        self.close_min = 0
        self.new_day = False

    def update(self, bar: Bar) -> None:
        local = self.clock.local(bar.time)
        day = local.date()
        self.new_day = day != self.day
        if self.new_day:
            self.day = day
            self.high, self.low, self.range_bars = float("-inf"), float("inf"), 0
            self.range_open = None
        if local.weekday() < 5:  # ไม่เอาแท่งวันเสาร์/อาทิตย์ (เช่น ทองเปิดคืนวันอาทิตย์) มาคิด ADR
            self.daily.update(day, bar)

        self.open_min = local.hour * 60 + local.minute
        self.close_min = self.open_min + self.tf_min
        if self.open_min >= self._rs and self.close_min <= self._re:
            self.high = max(self.high, bar.high)
            self.low = min(self.low, bar.low)
            if self.range_bars == 0:
                self.range_open = bar.open
            self.range_bars += 1

    @property
    def expected_range_bars(self) -> int:
        return (self._re - self._rs) // self.tf_min

    @property
    def complete(self) -> bool:
        """กรอบวันนี้เก็บครบแล้ว — ถ้าขาดแท่งเกินครึ่ง (วันหยุด/ข้อมูลหาย) ถือว่าใช้ไม่ได้"""
        return self.open_min >= self._re and self.range_bars * 2 >= self.expected_range_bars

    @property
    def in_trade_window(self) -> bool:
        return self.open_min >= self._re and self.close_min <= self._te

    @property
    def at_flat_time(self) -> bool:
        return self.close_min >= self._flat

    @property
    def width(self) -> float:
        return self.high - self.low

    def width_ratio(self) -> float | None:
        adr = self.daily.adr
        if adr is None or adr <= 0 or not self.complete:
            return None
        return self.width / adr


def flat_signals(strategy: Strategy, rng: SessionRange, account: IAccount) -> list[Signal]:
    if rng.at_flat_time and account.positions(strategy.symbol, strategy.tag):
        return [ExitSignal(strategy.symbol, strategy.tag, "eod")]
    return []
