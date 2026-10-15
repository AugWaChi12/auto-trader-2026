"""
runner.py — วงรอบ live (demo): ดึงแท่ง M15 ที่ปิดแล้ว → Engine.on_bar (ตัวเดียวกับ backtest)

    python -m bot live

ลำดับเริ่มต้น:
  1) เชื่อม MT5 — ปฏิเสธถ้าไม่ใช่บัญชี DEMO
  2) กู้ signal key ที่ใช้ไปแล้ว (จาก comment ของไม้/deal) → รีสตาร์ทกลางวันไม่เข้าซ้ำ
  3) กู้สถานะ RiskManager (peak equity, kill switch) จาก state/risk.json
  4) warmup ด้วยแท่งย้อนหลังโดยไม่เทรด
  5) วนทุก POLL_SECONDS: มีแท่งใหม่ปิด → ป้อน Engine → บันทึก state

ระหว่างแท่ง บอทไม่ต้องทำอะไร เพราะ SL/TP อยู่ฝั่ง server
Ctrl+C / ปิดหน้าต่าง → บอทหยุด แต่ไม้ที่เปิดอยู่ยังมี SL/TP คุ้มครอง
OrderGuard.stale_after (5 นาที) กันเข้าไม้จากแท่งเก่าที่ตามเก็บหลังเน็ตหลุด
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import config as C
from ..core.timeutil import UTC
from ..engine import Engine
from ..execution.order_guard import OrderGuard
from ..risk.news import NewsCalendar
from ..risk.risk_manager import RiskManager
from .fileio import atomic_write
from .mt5_broker import Mt5Broker
from .notify import Notifier

log = logging.getLogger("live")


def _find_terminal() -> str | None:
    from ..data.mt5_source import _find_terminal as f
    return os.environ.get("MT5_PATH") or f()


class LiveRunner:
    """หนึ่ง instance = หนึ่งบอท (C.LIVE_INSTANCES) — magic / timeframe / state แยกกัน รันหลายตัวพร้อมกันได้"""

    def __init__(self, instance: str = "main", strategy_tags: list[str] | None = None,
                 profile: str | None = None) -> None:
        spec = C.LIVE_INSTANCES[instance]
        self.instance = instance
        self.tf = spec["tf"]
        self.magic = spec["magic"]
        tags = strategy_tags or spec["strategies"]
        self.strategies = C.build_strategies(tags, timeframe=self.tf)
        if not self.strategies:
            raise SystemExit(f"ไม่มีกลยุทธ์ {tags}")
        self.symbols = sorted({s.symbol for s in self.strategies})
        self.profile = profile or spec["profile"]
        self.state_dir = C.instance_state_dir(instance)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.warmup_bars = spec.get("warmup", C.WARMUP_BARS)
        self.broker = Mt5Broker(self.symbols, C.DEFAULT_SPECS, self.tf, C.SERVER_TIME_MODE, self.magic, self.state_dir,
                                allocation=spec.get("allocation"))
        self.notifier = Notifier()
        self._risk_path = self.state_dir / "risk.json"
        self._heartbeat_path = self.state_dir / "heartbeat.json"
        self._last_time: dict[str, datetime] = {}
        self._known_tickets: set[int] = set()
        self._signals_seen = 0
        self._events_seen = 0
        self._last_heartbeat_log = datetime.min.replace(tzinfo=UTC)

    # ------------------------------------------------------------------ เริ่มต้น
    def start(self) -> None:
        self.broker.connect(_find_terminal())
        if not self.broker.algo_trading_enabled():
            log.error("ปุ่ม Algo Trading ใน MT5 ปิดอยู่ → ส่งออเดอร์ไม่ได้ (retcode 10027) กดเปิดที่แถบเครื่องมือ MT5")

        now = datetime.now(UTC)
        news = NewsCalendar.default(C.NEWS_CSV, (now.year - 1, now.year + 1), C.NEWS_BLACKOUT_MIN)
        self.risk = RiskManager(C.RISK_PROFILES[self.profile], self.broker, news,
                                real_equity=lambda: float(self.broker.mt5.account_info().equity))
        if self._risk_path.exists():
            self.risk.load_state(json.loads(self._risk_path.read_text(encoding="utf-8")))
            log.info("risk state restored: %s", self.risk.to_state())

        self.guard = OrderGuard()
        for k in self.broker.consumed_keys():
            self.guard.mark_consumed(k, now)
        self.engine = Engine(self.broker, self.broker, self.broker, self.strategies, self.risk, self.guard,
                             self.broker.last_bar_close)

        for sym in self.symbols:
            bars = self.broker.closed_bars(sym, self.warmup_bars)
            for b in bars:
                self.engine.warmup(sym, b)
            self.broker.set_last_bar(sym, bars[-1])
            self._last_time[sym] = bars[-1].time
            log.info("warmup %s: %d bars → last closed %s", sym, len(bars), bars[-1].time)

        self._known_tickets = {p.ticket for p in self.broker.positions()}
        cfg = self.risk.cfg
        self.notifier.send(
            f"🤖 บอท [{self.instance} {self.tf}] เริ่มทำงาน (DEMO)\nกลยุทธ์: {', '.join(s.tag for s in self.strategies)}\n"
            f"profile: {self.profile} ({cfg.risk_per_trade:.1%}/ไม้, หยุดวันที่ -{cfg.daily_loss_limit:.0%})\n"
            f"equity: {self.broker.equity():,.2f}  ไม้ที่เปิดอยู่: {len(self._known_tickets)}")

    # ------------------------------------------------------------------ วนรอบ
    def run_forever(self) -> None:
        # เริ่มไม่ได้ (เช่น MT5 ยังไม่เปิดตอนบูตเครื่อง) → รอแล้วลองใหม่ ไม่ดับเงียบ
        delay = 10
        while True:
            try:
                self.start()
                break
            except ConnectionError as e:          # NotDemoAccount ต้องหยุดทันที ห้ามวนลองใหม่
                log.warning("เริ่มไม่ได้: %s → ลองใหม่ใน %ds", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)
        failures = 0
        while True:
            try:
                self.tick()
                failures = 0
                time.sleep(C.POLL_SECONDS)
            except KeyboardInterrupt:
                log.info("หยุดโดยผู้ใช้ — ไม้ที่เปิดอยู่ยังมี SL/TP ฝั่ง server")
                self.notifier.send("⏹ บอทหยุดทำงาน (ไม้ที่เปิดอยู่ยังมี SL/TP)")
                return
            except Exception as e:  # noqa: BLE001 — บอทต้องไม่ตายเพราะ error ชั่วคราว
                failures += 1
                wait = min(5 * 2 ** (failures - 1), 300)
                log.exception("loop error (%d) → retry in %ds", failures, wait)
                if failures == 3:
                    self.notifier.send(f"⚠️ บอทเจอ error ติดกัน 3 ครั้ง: {e}")
                time.sleep(wait)
                try:
                    self.broker.connect(_find_terminal())
                except Exception:  # noqa: BLE001
                    log.exception("reconnect failed")

    def tick(self) -> None:
        if not self.broker.connected():
            raise ConnectionError("MT5 terminal ไม่ได้เชื่อมต่อ server")
        for sym in self.symbols:
            new = [b for b in self.broker.closed_bars(sym, 50) if b.time > self._last_time[sym]]
            for bar in new:
                self.broker.set_last_bar(sym, bar)
                self._last_time[sym] = bar.time
                self.engine.on_bar(sym, bar)
            if new:
                self._after_bars()
        self._heartbeat()

    def _after_bars(self) -> None:
        self.broker.save_meta()
        atomic_write(self._risk_path, json.dumps(self.risk.to_state(), indent=1))

        stats = self.engine.stats
        for s in stats.signals[self._signals_seen:]:
            if s.outcome == "sent":
                self.notifier.send(f"📈 เข้าไม้ {s.key}\n{s.detail}")
            elif s.outcome not in ("news_blackout",):
                log.info("signal %s not sent: %s %s", s.key, s.outcome, s.detail)
        self._signals_seen = len(stats.signals)
        for t, ev in stats.events[self._events_seen:]:
            self.notifier.send(f"🛑 RISK EVENT {ev} @ {t:%Y-%m-%d %H:%M} UTC — {self.risk.kill_reason}")
        self._events_seen = len(stats.events)

        current = {p.ticket for p in self.broker.positions()}
        closed = self._known_tickets - current
        if closed:
            deals = self.broker.bot_deals(datetime.now(UTC) - timedelta(days=3))
            for ticket in closed:
                pnl = sum(d.profit + d.commission + d.swap + d.fee for d in deals if d.position_id == ticket)
                self.notifier.send(f"✅ ปิดไม้ #{ticket}  P/L {pnl:+.2f} USD  equity {self.broker.equity():,.2f}")
        self._known_tickets = current

    def _heartbeat(self) -> None:
        now = datetime.now(UTC)
        hb = {"time": now.isoformat(), "pid": os.getpid(), "instance": self.instance, "tf": self.tf,
              "magic": self.magic, "equity": self.broker.equity(), "allocation": self.broker.allocation,
              "last_bars": {s: t.isoformat() for s, t in self._last_time.items()},
              "algo_trading": self.broker.algo_trading_enabled(), "killed": self.risk.killed,
              "kill_reason": self.risk.kill_reason, "halted_today": self.risk.halted_today,
              "profile": self.profile, "strategies": {s.tag: s.status() for s in self.strategies}}
        atomic_write(self._heartbeat_path, json.dumps(hb, indent=1))
        if now - self._last_heartbeat_log >= timedelta(minutes=30):
            self._last_heartbeat_log = now
            pos = self.broker.positions()
            log.info("heartbeat equity=%.2f positions=%d algo=%s last_bar=%s", hb["equity"], len(pos),
                     hb["algo_trading"], hb["last_bars"])


def setup_file_logging(log_dir: Path, instance: str = "main") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{'live' if instance == 'main' else instance}_{datetime.now():%Y%m%d}.log"
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    return path
