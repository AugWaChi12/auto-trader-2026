"""
manager.py — ผู้จัดการพอร์ต: เฝ้าบอททดสอบ + ตลาดทุก 1 นาที แล้วสั่งการเอง (python -m bot manager)

สั่งได้แค่ 3 อย่าง (ผ่านขีดจำกัดความปลอดภัยทุกครั้ง):
  pause(บอท, นาที)   — supervisor หยุดบอทนั้นจนครบเวลา (state/manager_pauses.json)
  resume(บอท)        — ให้กลับมาทำงาน
  close(บอท)         — ปิดไม้ที่บอทนั้นถืออยู่ทันที (บัญชี DEMO เท่านั้น)
  + say(ข้อความ)      — รายงาน/วิเคราะห์ ขึ้นแชทในหน้า /sandbox
ห้ามแตะ main (ทดสอบไปข้างหน้า) · ไม่เปิดไม้เอง · สั่งได้ไม่เกิน MAX_ACTIONS_PER_HOUR

สมอง:
  RuleBrain (ค่าเริ่มต้น)  — กฎที่ตรวจสอบได้ทุกข้อ
  สมอง AI (ทางเลือก)     — GEMINI_API_KEY → Gemini (manager_gemini.py) · ANTHROPIC_API_KEY → Claude (manager_llm.py)
                           ถามทุก MANAGER_LLM_MIN นาที (ถ้ามีทั้งสองคีย์ เลือกด้วย MANAGER_AI=gemini|claude)
                           (กฎด้านความปลอดภัยของ RuleBrain ยังทำงานเสมอ)
แชท: state/manager_log.jsonl (หน้าเว็บอ่านผ่าน /api/manager)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from . import config as C

log = logging.getLogger("manager")

LOG_PATH = C.STATE_DIR / "manager_log.jsonl"
PAUSES_PATH = C.STATE_DIR / "manager_pauses.json"
TICK_SEC = 60
REPORT_EVERY_MIN = 30
MAX_ACTIONS_PER_HOUR = 120    # กันบั๊กสั่งรัวเท่านั้น (DEMO ให้ลองได้เต็มที่)
MAX_PAUSE_MIN = 24 * 60

# เพดานขาดทุน (ข้อ 1, 3, 4 ด้านล่าง) ปิดไว้ — DEMO ให้เรียนรู้จากผลจริง ถูกผิดเต็มที่ · เปิดคืนด้วย MANAGER_STOPS=1
LOSS_STOPS = os.environ.get("MANAGER_STOPS") == "1"

# เกณฑ์ของสมองตามกฎ (ดอลลาร์ = ของบอทแต่ละตัว เว้นแต่บอกว่าทั้งพอร์ต)
EQUITY_GUARD = 640.0          # equity ทั้งบัญชีต่ำกว่านี้ → พักบอททดสอบทั้งหมด (ก่อนชนเพดาน $600 ของ RiskManager)
PORTFOLIO_DAY_STOP = -150.0   # บอททดสอบรวมกันวันนี้เสียถึงนี้ → พักทั้งหมดถึงวันใหม่
BOT_DAY_STOP = -45.0          # บอทตัวเดียววันนี้ (รวมไม้ที่ลอย) เสียถึงนี้ → ปิดไม้ + พักถึงวันใหม่
LOSS_STREAK = 4               # แพ้ติดกันกี่ไม้ → พัก STREAK_PAUSE_MIN
STREAK_PAUSE_MIN = 90
GIVEBACK_PEAK = 15.0          # ไม้เคยกำไรถึงนี้ แล้วหดเหลือครึ่ง → ปิดเก็บกำไร
STALE_HOURS, STALE_LOSS = 3.0, -20.0   # ถือนานเกิน และยังขาดทุนเกิน → ตัดทิ้ง
VOL_SPIKE = 3.0               # ช่วง 15 นาทีล่าสุดกว้างกว่าค่ากลาง 3 ชม. กี่เท่า → พักบอท BTC
VOL_PAUSE_MIN = 30

# อ่านสภาพตลาด (BTC M5 ย้อนหลัง 2 ชม.) แล้วพักบอทที่สายกลยุทธ์ไม่เข้ากับตลาดตอนนี้
TREND_FAMILIES = {"ema", "donchian", "momo", "orb", "pullback"}   # ได้เปรียบตอนตลาดวิ่งเป็นทาง
RANGE_FAMILIES = {"bb", "rsi2", "levels"}                         # ได้เปรียบตอนตลาดเด้งไปมาในกรอบ
ER_TREND, ER_RANGE = 0.40, 0.20     # efficiency ratio: ระยะที่ราคาไปได้จริง ÷ ระยะที่วิ่งทั้งหมด
REGIME_CONFIRM = 3                  # ต้องเห็นสภาพเดิมติดกันกี่นาทีถึงจะเชื่อ (กันสั่งกลับไปกลับมา)
REGIME_PAUSE_MIN = 240               # พักนานสุดเท่านี้ — ปกติได้กลับก่อนเมื่อสภาพตลาดเปลี่ยน
REGIME_MIN_REST_MIN = 30            # พักเพราะสภาพตลาดอย่างน้อยกี่นาทีก่อนจะให้กลับ
REGIME_TH = {"trend": "ตลาดวิ่งเป็นเทรนด์", "range": "ตลาดออกข้างในกรอบ", "mixed": "ตลาดยังไม่ชัดทาง"}


def now_ts() -> float:
    return time.time()


def minutes_to_new_day() -> int:
    """นาทีถึงเที่ยงคืนเวลาไทย — "วันนี้" ของหน้า monitor นับตามเวลาไทย"""
    n = datetime.now(timezone.utc) + timedelta(hours=7)
    return max(int((86400 - (n.hour * 3600 + n.minute * 60 + n.second)) / 60) + 1, 1)


# ---------------------------------------------------------------- แชท + การพัก (ไฟล์ใน state/ ใช้ร่วมกับ supervisor/หน้าเว็บ)
def say(kind: str, text: str, **data: Any) -> dict:
    """kind: analysis 🧠 · action ⚡ · alert ⚠️ · info ℹ️"""
    msg = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "text": text, **data}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    log.info("[%s] %s", kind, text)
    return msg


def read_log(limit: int = 80) -> list[dict]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def trim_log(keep: int = 3000) -> None:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > keep * 1.5:
            LOG_PATH.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def load_pauses_raw() -> dict[str, dict]:
    try:
        d = json.loads(PAUSES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)}


def load_pauses() -> dict[str, dict]:
    """การพักที่ยังไม่หมดเวลา (supervisor ใช้ฟังก์ชันนี้ด้วย)"""
    return {b: p for b, p in load_pauses_raw().items() if p.get("until", 0) > now_ts()}


def save_pauses(p: dict[str, dict]) -> None:
    from .live.fileio import atomic_write
    atomic_write(PAUSES_PATH, json.dumps(p, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- ภาพรวมพอร์ต
@dataclass
class Snapshot:
    t: float
    equity: float
    balance: float
    bots: dict[str, dict]             # ชื่อ → {today, total, trades, win_rate, open:[...], alive, family, symbol, state, streak}
    btc_bars: list[list] = field(default_factory=list)
    btc: float | None = None

    def test_bots(self) -> dict[str, dict]:
        return {n: b for n, b in self.bots.items() if n != "main"}


def loss_streak(rows: list[dict]) -> int:
    n = 0
    for r in rows:                      # ใหม่ → เก่า
        if r["profit"] < 0:
            n += 1
        else:
            break
    return n


def build_snapshot(col: Any) -> Snapshot:
    d = col.data()
    if d.get("error"):
        raise ConnectionError(d["error"])
    acc = d.get("account") or {}
    bots: dict[str, dict] = {}
    for name, pb in (d.get("per_bot") or {}).items():
        hb = (d.get("bots") or {}).get(name, {})
        st = next(iter((hb.get("strategies") or {}).values()), {})
        res = (d.get("research") or {}).get(name, {})
        opens = [o for o in d.get("open") or [] if o["bot"] == name]
        rows = col.trades_page(name, 1, 10).get("rows", []) if name != "main" else []
        bots[name] = {"today": round((pb.get("today") or 0) + (pb.get("floating") or 0), 2), "closed_today": pb.get("today") or 0,
                      "total": pb.get("total") or 0, "trades": pb.get("trades") or 0, "win_rate": pb.get("win_rate"),
                      "open": opens, "alive": bool(hb.get("alive")), "state": st.get("state"),
                      "family": res.get("family"), "symbol": st.get("symbol") or res.get("symbol"),
                      "streak": loss_streak(rows), "target_hit": bool(hb.get("target_hit"))}
    bars = (col.bars("BTCUSD", "M1", 200) or {}).get("bars") or []
    return Snapshot(now_ts(), float(acc.get("equity") or 0), float(acc.get("balance") or 0), bots, bars,
                    ((d.get("quotes") or {}).get("BTCUSD") or {}).get("bid"))


def range_ratio(bars: list[list], window: int = 15) -> float | None:
    """ช่วงราคา 15 นาทีล่าสุด เทียบค่ากลางของช่วง 15 นาทีย้อนหลัง ~3 ชม."""
    if len(bars) < window * 8:
        return None
    ranges = []
    for i in range(len(bars) - window * 12, len(bars) - window + 1, window):
        chunk = bars[max(i, 0): i + window]
        if chunk:
            ranges.append(max(b[2] for b in chunk) - min(b[3] for b in chunk))
    last = bars[-window:]
    base = median(ranges[:-1]) if len(ranges) > 1 else None
    return (max(b[2] for b in last) - min(b[3] for b in last)) / base if base else None


def to_m5(bars: list[list], n: int = 36) -> list[list]:
    """แท่ง M1 → M5 ล่าสุด n แท่ง [เวลา, เปิด, สูง, ต่ำ, ปิด]"""
    out = []
    for i in range(len(bars) - (len(bars) % 5), 0, -5)[:n]:
        ch = bars[i - 5:i]
        if ch:
            out.append([ch[0][0], ch[0][1], max(b[2] for b in ch), min(b[3] for b in ch), ch[-1][4]])
    return out[::-1]


def market_regime(bars: list[list]) -> tuple[str | None, float | None]:
    """trend | range | mixed (None = ข้อมูลไม่พอ) + efficiency ratio ของ BTC M5 24 แท่งล่าสุด"""
    m5 = to_m5(bars, 25)
    if len(m5) < 25:
        return None, None
    closes = [b[4] for b in m5]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    er = abs(closes[-1] - closes[0]) / path if path else 0.0
    return ("trend" if er >= ER_TREND else "range" if er <= ER_RANGE else "mixed"), round(er, 2)


def suits(family: str | None, regime: str) -> bool | None:
    """สายกลยุทธ์นี้เข้ากับสภาพตลาดไหม (None = ไม่ขึ้นกับสภาพตลาด / ตลาดยังไม่ชัด)"""
    if regime == "trend":
        return False if family in RANGE_FAMILIES else True if family in TREND_FAMILIES else None
    if regime == "range":
        return False if family in TREND_FAMILIES else True if family in RANGE_FAMILIES else None
    return None


# ---------------------------------------------------------------- สมองตามกฎ
@dataclass
class Action:
    kind: str                 # pause | resume | close | say
    bot: str | None = None
    minutes: int = 0
    reason: str = ""
    src: str = "rules"
    tag: str = ""             # regime = พักเพราะสภาพตลาด (ให้กลับเองเมื่อตลาดเปลี่ยน)        # rules | claude — หน้าเว็บโชว์ว่าสมองไหนสั่ง


class RuleBrain:
    name = "สมองตามกฎ"

    def __init__(self) -> None:
        self.peaks: dict[int, float] = {}       # ticket → กำไรลอยสูงสุดที่เคยเห็น
        self.last_report = 0.0
        self.vol_alerted = 0.0
        self.regimes: list[str | None] = []      # สภาพตลาดรายนาทีล่าสุด
        self.regime: str | None = None           # สภาพที่ยืนยันแล้ว
        self.acted: set[str] = set()             # บอทที่สั่งเพราะสภาพตลาดไปแล้วในรอบสภาพนี้ (ไม่สั่งซ้ำ ถ้า AI สั่งกลับ)
        for m in reversed(read_log(300)):       # รีสตาร์ทแล้วไม่ประกาศสภาพตลาดเดิมซ้ำในแชท
            if m.get("text", "").startswith("สภาพตลาดเปลี่ยน: "):
                self.regime = next((k for k, v in REGIME_TH.items() if m["text"].startswith(f"สภาพตลาดเปลี่ยน: {v}")), None)
                break

    def decide(self, s: Snapshot, pauses: dict[str, dict]) -> list[Action]:
        acts: list[Action] = []
        tests = s.test_bots()
        paused = set(pauses)

        # 1) ทั้งพอร์ต: equity ใกล้เพดาน / เสียรวมเกินเพดานรายวัน → พักทุกตัว
        if LOSS_STOPS and s.equity and s.equity < EQUITY_GUARD:
            for b in tests:
                if b not in paused:
                    acts.append(Action("pause", b, 240, f"equity ${s.equity:,.2f} ใกล้เพดาน $600 — พักบอททดสอบทั้งหมด 4 ชม."))
            return acts
        day = sum(b["today"] for b in tests.values())
        if LOSS_STOPS and day <= PORTFOLIO_DAY_STOP:
            for b in tests:
                if b not in paused:
                    acts.append(Action("pause", b, minutes_to_new_day(),
                                       f"บอททดสอบรวมวันนี้ {day:+.2f}$ ถึงเพดาน {PORTFOLIO_DAY_STOP:+.0f}$ — พักทั้งหมดถึงวันใหม่"))
            return acts

        # 2) ความผันผวน BTC พุ่งผิดปกติ → พักบอท BTC ชั่วคราว
        rr = range_ratio(s.btc_bars)
        if rr and rr >= VOL_SPIKE and s.t - self.vol_alerted > VOL_PAUSE_MIN * 60:
            self.vol_alerted = s.t
            for n, b in tests.items():
                if b["symbol"] == "BTCUSD" and n not in paused and not b["open"]:
                    acts.append(Action("pause", n, VOL_PAUSE_MIN, f"BTC แกว่งแรงผิดปกติ ({rr:.1f}× ของปกติใน 15 นาที) — หลบ {VOL_PAUSE_MIN} นาที"))

        acts += self.regime_moves(s, tests, pauses)

        for n, b in tests.items():
            # 3) บอทตัวเดียวเสียหนักวันนี้
            if LOSS_STOPS and b["today"] <= BOT_DAY_STOP and n not in paused:
                if b["open"]:
                    acts.append(Action("close", n, reason=f"{n} วันนี้ {b['today']:+.2f}$ — ตัดไม้ที่ค้าง"))
                acts.append(Action("pause", n, minutes_to_new_day(), f"{n} วันนี้ {b['today']:+.2f}$ ถึงเพดาน {BOT_DAY_STOP:+.0f}$ — พักถึงวันใหม่"))
                continue
            # 4) แพ้ติดกัน
            if LOSS_STOPS and b["streak"] >= LOSS_STREAK and n not in paused and not b["open"]:
                acts.append(Action("pause", n, STREAK_PAUSE_MIN, f"{n} แพ้ติดกัน {b['streak']} ไม้ — พัก {STREAK_PAUSE_MIN} นาที ให้ตลาดเปลี่ยนจังหวะ"))
            # 5) ไม้ที่ถืออยู่: กำไรหดคืน / ขาดทุนค้างนาน
            for o in b["open"]:
                tk, pnl = o["ticket"], o["profit"]
                self.peaks[tk] = max(self.peaks.get(tk, pnl), pnl)
                peak = self.peaks[tk]
                held_h = (s.t - datetime.fromisoformat(o["open_time"]).timestamp()) / 3600
                if peak >= GIVEBACK_PEAK and pnl <= peak * 0.5:
                    acts.append(Action("close", n, reason=f"{n} กำไรเคยถึง +{peak:.2f}$ เหลือ {pnl:+.2f}$ — ปิดเก็บก่อนหดหมด"))
                elif held_h >= STALE_HOURS and pnl <= STALE_LOSS:
                    acts.append(Action("close", n, reason=f"{n} ถือมา {held_h:.1f} ชม. ยังขาดทุน {pnl:+.2f}$ — ตัดทิ้ง ไม่ฝากความหวัง"))
        live = {o["ticket"] for b in tests.values() for o in b["open"]}
        self.peaks = {k: v for k, v in self.peaks.items() if k in live}

        # 6) รายงานตลาด/พอร์ตเป็นระยะ
        if s.t - self.last_report >= REPORT_EVERY_MIN * 60:
            self.last_report = s.t
            acts.append(Action("say", reason=self.report(s, pauses, rr)))
        return acts

    def regime_moves(self, s: Snapshot, tests: dict[str, dict], pauses: dict[str, dict]) -> list[Action]:
        """สภาพตลาดยืนยันแล้ว → พักบอทที่สายไม่เข้ากัน · ให้ตัวที่พักเพราะสภาพตลาดกลับเมื่อเข้ากันอีกครั้ง"""
        now, er = market_regime(s.btc_bars)
        self.regimes = (self.regimes + [now])[-REGIME_CONFIRM:]
        if now is None or len(self.regimes) < REGIME_CONFIRM or len(set(self.regimes)) != 1:
            return []
        acts: list[Action] = []
        if now != self.regime:
            self.regime, self.acted = now, set()
            acts.append(Action("say", reason=f"สภาพตลาดเปลี่ยน: {REGIME_TH[now]} (efficiency {er:.2f} จาก BTC M5 2 ชม.)"))
        for n, b in tests.items():
            if n in self.acted or b["symbol"] != "BTCUSD":
                continue
            fit, p = suits(b.get("family"), now), pauses.get(n)
            if fit is False and p is None and not b["open"]:
                self.acted.add(n)
                acts.append(Action("pause", n, REGIME_PAUSE_MIN, f"{n} สาย {b.get('family')} ไม่ถนัด{REGIME_TH[now]} — พักรอ", tag="regime"))
            elif fit is not False and p and p.get("tag") == "regime" and s.t - p.get("since", 0) >= REGIME_MIN_REST_MIN * 60:
                self.acted.add(n)
                acts.append(Action("resume", n, reason=f"{REGIME_TH[now]} — สาย {b.get('family')} กลับมาลุยได้", tag="regime"))
        return acts

    @staticmethod
    def report(s: Snapshot, pauses: dict[str, dict], rr: float | None) -> str:
        tests = s.test_bots()
        day = sum(b["today"] for b in tests.values())
        ranked = sorted(tests.items(), key=lambda kv: kv[1]["today"], reverse=True)
        best, worst = ranked[0] if ranked else None, ranked[-1] if ranked else None
        move = ""
        if len(s.btc_bars) >= 61:
            chg = s.btc_bars[-1][4] - s.btc_bars[-61][4]
            move = f"BTC {s.btc_bars[-1][4]:,.0f} ({chg:+,.0f} ใน 1 ชม.)"
        vol = "" if rr is None else (" · ตลาดเงียบ" if rr < 0.7 else " · ตลาดแกว่งแรง" if rr > 1.8 else " · ความผันผวนปกติ")
        reg, er = market_regime(s.btc_bars)
        if reg:
            vol += f" · {REGIME_TH[reg]} (ER {er:.2f})"
        opens = sum(len(b["open"]) for b in tests.values())
        parts = [f"รายงาน: {move}{vol}", f"บอททดสอบวันนี้รวม {day:+.2f}$ · ถือไม้ {opens} ไม้ · equity ${s.equity:,.2f}"]
        if best and worst and best[0] != worst[0]:
            parts.append(f"ดีสุด {best[0]} {best[1]['today']:+.2f}$ · แย่สุด {worst[0]} {worst[1]['today']:+.2f}$")
        if pauses:
            parts.append("พักอยู่: " + ", ".join(sorted(pauses)))
        return " · ".join(parts)


# ---------------------------------------------------------------- ผู้จัดการ
class Manager:
    def __init__(self, col: Any = None) -> None:
        if col is None:
            from .monitor.server import Collector
            col = Collector()
        self.col = col
        self.brain = RuleBrain()
        self.llm = None
        which = os.environ.get("MANAGER_AI") or ("gemini" if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                                                 else "claude" if os.environ.get("ANTHROPIC_API_KEY") else "")
        if which == "gemini":
            from .manager_gemini import GeminiBrain
            self.llm = GeminiBrain()
        elif which == "claude":
            from .manager_llm import ClaudeBrain
            self.llm = ClaudeBrain()
        self.recent: list[float] = []           # เวลาที่สั่งการ (นับเพดานต่อชั่วโมง)

    def allowed(self, a: Action, s: Snapshot) -> str:
        """ขีดจำกัดความปลอดภัย — คืนเหตุผลถ้าไม่ให้ทำ"""
        if a.kind == "say":
            return ""
        if a.bot == "main" or a.bot not in s.bots:
            return "สั่งได้เฉพาะบอททดสอบ"
        self.recent = [t for t in self.recent if s.t - t < 3600]
        if len(self.recent) >= MAX_ACTIONS_PER_HOUR:
            return f"สั่งการครบ {MAX_ACTIONS_PER_HOUR} ครั้งในชั่วโมงนี้แล้ว"
        if a.kind == "close" and not s.bots[a.bot]["open"]:
            return "ไม่มีไม้ให้ปิด"
        return ""

    def execute(self, a: Action, s: Snapshot, pauses: dict[str, dict]) -> None:
        why = self.allowed(a, s)
        if why:
            log.info("skip %s %s: %s", a.kind, a.bot, why)
            return
        if a.kind == "say":
            say("analysis", a.reason, src=a.src)
            return
        self.recent.append(s.t)
        if a.kind == "pause":
            m = max(1, min(int(a.minutes), MAX_PAUSE_MIN))
            pauses[a.bot] = {"until": s.t + m * 60, "since": s.t, "reason": a.reason, "tag": a.tag, "src": a.src}
            save_pauses(pauses)
            say("action", f"⏸ สั่งพัก {a.bot} {m} นาที — {a.reason}", bot=a.bot, act="pause", minutes=m, src=a.src)
        elif a.kind == "resume":
            if pauses.pop(a.bot, None) is not None:
                save_pauses(pauses)
                say("action", f"▶ ให้ {a.bot} กลับมาทำงาน — {a.reason}", bot=a.bot, act="resume", src=a.src)
        elif a.kind == "close":
            res = self.col.close_bot_positions(a.bot)
            ok = sum(1 for r in res if r.get("ok"))
            say("action", f"✋ สั่งปิดไม้ {a.bot} ({ok}/{len(res)}) — {a.reason}", bot=a.bot, act="close", src=a.src)

    def brain_name(self) -> str:
        return f"{self.llm.name} + {self.brain.name}" if self.llm else self.brain.name

    def tick(self) -> None:
        s = build_snapshot(self.col)
        raw = load_pauses_raw()
        pauses = {b: p for b, p in raw.items() if p.get("until", 0) > s.t}
        expired = sorted(set(raw) - set(pauses))
        for b in expired:
            say("action", f"▶ ครบเวลาพัก — {b} กลับมาทำงาน", bot=b, act="resume")
        if expired:
            save_pauses(pauses)                 # ล้างตัวที่หมดเวลาออกจากไฟล์
        actions = self.brain.decide(s, pauses)
        if self.llm is not None:
            actions += self.llm.maybe_decide(s, pauses)
        for a in actions:
            try:
                self.execute(a, s, pauses)
            except Exception as e:  # noqa: BLE001
                log.exception("action failed")
                say("alert", f"⚠️ ทำ {a.kind} {a.bot or ''} ไม่สำเร็จ: {e}")

    @staticmethod
    def release_loss_stops() -> None:
        """ปิดเพดานขาดทุนแล้ว → ปล่อยบอทที่ถูกพักเพราะเพดาน/แพ้ติดกันก่อนหน้านี้"""
        p = load_pauses()
        freed = sorted(b for b, x in p.items() if not x.get("tag") and x.get("src", "rules") == "rules"
                       and ("เพดาน" in x.get("reason", "") or "แพ้ติดกัน" in x.get("reason", "")))
        if freed:
            save_pauses({b: x for b, x in p.items() if b not in freed})
            say("action", f"▶ ปิดเพดานขาดทุนแล้ว (DEMO ลองเต็มที่) — ปล่อย {', '.join(freed)} กลับมาทำงาน", act="resume")

    def run(self) -> None:
        say("info", f"ผู้จัดการพอร์ตเริ่มงาน ({self.brain_name()}) — เฝ้าบอททดสอบ 9 ตัวทุก {TICK_SEC} วิ · ไม่แตะ main",
            src=self.llm.src if self.llm else "rules")
        if not LOSS_STOPS:
            self.release_loss_stops()
        n = 0
        while True:
            try:
                self.tick()
            except ConnectionError as e:
                log.warning("snapshot: %s", e)
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            n += 1
            if n % 60 == 0:
                trim_log()
            time.sleep(TICK_SEC)
