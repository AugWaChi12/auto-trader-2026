"""
server.py — หน้า monitor แบบเบา (อ่านอย่างเดียว ไม่ส่งออเดอร์)

    python -m bot monitor          → http://127.0.0.1:8765          ภาพรวมทุกบอท
                                      http://127.0.0.1:8765/charts   กราฟหลายจอ

ใช้แค่ stdlib (http.server) + MetaTrader5 ไม่ต้องใช้อินเทอร์เน็ต
ผูกกับ 127.0.0.1 เท่านั้น — เครื่องอื่นในเครือข่ายเข้าไม่ได้
ทุก endpoint cache ผล 2 วินาที → เปิดหลายแท็บ/หลายกราฟก็ไม่หนักเครื่อง
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import config as C
from .. import settings as _settings
from ..core.timeutil import UTC, server_to_utc
from ..live.mt5_broker import decode_comment

log = logging.getLogger("monitor")
HERE = Path(__file__).parent
PAGES = {"/": HERE / "index.html", "/index.html": HERE / "index.html", "/charts": HERE / "charts.html",
         "/radar": HERE / "radar.html", "/techniques": HERE / "techniques.html", "/live": HERE / "live.html",
         "/sandbox": HERE / "sandbox.html", "/settings": HERE / "settings.html", "/stream": HERE / "stream.html"}
TECHNIQUES = C.ROOT / "research" / "techniques.json"
RADAR_STATS = C.ROOT / "research" / "out" / "radar_stats.json"
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def _utc(server_epoch: int) -> str:
    return server_to_utc(datetime.fromtimestamp(server_epoch, UTC).replace(tzinfo=None), C.SERVER_TIME_MODE).isoformat()


def _exit_reason(comment: str) -> str:
    c = comment.lower()
    if c.startswith("[sl"):
        return "SL"
    if c.startswith("[tp"):
        return "TP"
    if c.startswith("so ") or "stop out" in c:
        return "STOP OUT"
    return {"eod": "ปิดสิ้นวัน", "halt": "risk halt", "stop_moved": "stop ย้าย", "end": "end", "ema5": "EMA5",
            "time_stop": "หมดเวลา", "weekend": "ก่อนสุดสัปดาห์"}.get(comment, comment or "-")


class Collector:
    def __init__(self) -> None:
        self._mt5 = None                  # ต่อ MT5 ตอนใช้จริง: บน Docker container MT5 อาจยังไม่พร้อมตอนหน้าเว็บเปิด
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._ok = False
        self._names: dict[str, str] = {}
        self.bots = {name: spec["magic"] for name, spec in C.LIVE_INSTANCES.items()}
        self.by_magic = {m: n for n, m in self.bots.items()}

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            from ..mt5api import load
            self._mt5 = load()
        return self._mt5

    # ------------------------------------------------------------------ cache + การเชื่อมต่อ
    def _cached(self, key: str, fn) -> Any:  # noqa: ANN001
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < 2:
                return hit[1]
            try:
                if not self._ok:
                    self._connect()
                d = fn()
            except Exception as e:  # noqa: BLE001
                self._ok = False
                d = {"error": str(e), "bots": self._heartbeats(), "log": self._log_tail()}
            self._cache[key] = (time.monotonic(), d)
            return d

    def _connect(self) -> None:
        from ..live.runner import _find_terminal
        path = _find_terminal()
        if not (self.mt5.initialize(path=path) if path else self.mt5.initialize()):
            raise ConnectionError(f"MT5: {self.mt5.last_error()}")
        self._ok = True

    def _name(self, canonical: str) -> str:
        if canonical not in self._names:
            from ..data.mt5_source import find_symbol
            self._names[canonical] = find_symbol(canonical, self.mt5)
            self.mt5.symbol_select(self._names[canonical], True)
        return self._names[canonical]

    def _canonical(self, broker_name: str) -> str:
        for c in C.DEFAULT_SPECS:
            if self._name(c) == broker_name:
                return c
        return broker_name

    # ------------------------------------------------------------------ สั่งงานจากหน้าเว็บ (บอททดสอบเท่านั้น · DEMO เท่านั้น)
    def close_bot_positions(self, bot: str) -> list[dict]:
        """ปิดไม้ทุกไม้ของบอทนี้ทันทีด้วยราคาตลาด — ตรวจว่าเป็นบัญชี DEMO ก่อนทุกครั้ง"""
        with self._lock:
            if not self._ok:
                self._connect()
            mt5 = self.mt5
            acc = mt5.account_info()
            if acc is None or acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
                raise ValueError("ไม่ใช่บัญชี DEMO — ไม่ปิดไม้ให้")
            out = []
            for p in mt5.positions_get() or ():
                if p.magic != self.bots[bot]:
                    continue
                tick, info = mt5.symbol_info_tick(p.symbol), mt5.symbol_info(p.symbol)
                buy = p.type == 0
                fill = mt5.ORDER_FILLING_IOC if info.filling_mode & 2 else (
                    mt5.ORDER_FILLING_FOK if info.filling_mode & 1 else mt5.ORDER_FILLING_RETURN)
                r = mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": p.ticket,
                    "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
                    "deviation": 50, "magic": p.magic, "comment": "manual_close", "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": fill})
                ok = r is not None and r.retcode in (10008, 10009)
                out.append({"ticket": p.ticket, "ok": ok, "message": r.comment if r is not None else str(mt5.last_error())})
            self._cache.clear()
            return out

    # ------------------------------------------------------------------ ไฟล์ของบอท
    def _heartbeats(self) -> dict[str, dict]:
        out = {}
        for name in self.bots:
            p = C.instance_state_dir(name) / "heartbeat.json"
            try:
                hb = json.loads(p.read_text(encoding="utf-8"))
                age = (datetime.now(UTC) - datetime.fromisoformat(hb["time"])).total_seconds()
                out[name] = {**hb, "alive": age < 90, "age_s": round(age)}
            except (OSError, ValueError):
                out[name] = {"alive": False, "age_s": None, "tf": C.LIVE_INSTANCES[name]["tf"],
                             "strategies": {}, "never_started": True}
        return out

    def _log_tail(self, n: int = 30) -> list[str]:
        lines = []
        for name in self.bots:
            prefix = "live" if name == "main" else name
            files = sorted(C.LOG_DIR.glob(f"{prefix}_*.log"))
            if files:
                for ln in files[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
                    lines.append((ln[:23], f"{ln[11:19]} [{name}] {ln[24:]}"))
        lines.sort()
        return [t for _, t in lines[-n:]]

    # ------------------------------------------------------------------ ภาพรวม
    def data(self) -> dict:
        return self._cached("data", self._collect)

    def _deals(self) -> list:
        return list(self.mt5.history_deals_get(datetime(2020, 1, 1, tzinfo=UTC), datetime.now(UTC) + timedelta(days=1)) or ())

    def _trades(self, deals: list) -> list[dict]:
        mt5 = self.mt5
        by_pos: dict[int, list] = defaultdict(list)
        for d in deals:
            if d.magic in self.by_magic and d.position_id:
                by_pos[d.position_id].append(d)
        trades = []
        for pid, ds in by_pos.items():
            ins = [d for d in ds if d.entry == mt5.DEAL_ENTRY_IN]
            outs = [d for d in ds if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
            if not ins or not outs:
                continue
            i, o = ins[0], outs[-1]
            dec = decode_comment(i.comment)
            trades.append({
                "ticket": pid, "bot": self.by_magic[i.magic], "symbol": self._canonical(i.symbol),
                "side": "BUY" if i.type == 0 else "SELL", "volume": i.volume,
                "open_time": _utc(i.time), "open_price": i.price, "close_time": _utc(o.time), "close_price": o.price,
                "reason": _exit_reason(o.comment), "profit": round(sum(d.profit + d.commission + d.swap + d.fee for d in ds), 2),
                "tag": dec[0] if dec else "?", "group": dec[2] if dec else "",
            })
        trades.sort(key=lambda t: t["close_time"], reverse=True)
        return trades

    def _open(self) -> list[dict]:
        out = []
        for p in self.mt5.positions_get() or ():
            if p.magic not in self.by_magic:
                continue
            dec = decode_comment(p.comment)
            out.append({
                "ticket": p.ticket, "bot": self.by_magic[p.magic], "symbol": self._canonical(p.symbol),
                "side": "BUY" if p.type == 0 else "SELL", "volume": p.volume, "open_time": _utc(p.time),
                "open_price": p.price_open, "price": p.price_current, "sl": p.sl or None, "tp": p.tp or None,
                "profit": round(p.profit + p.swap, 2), "tag": dec[0] if dec else "?", "role": dec[3] if dec else "",
            })
        return out

    @staticmethod
    def _stats(trades: list[dict]) -> dict:
        real = [t for t in trades if t["tag"] != "smoke"]
        wins = [t for t in real if t["profit"] > 0]
        bkk_today = (datetime.now(UTC) + timedelta(hours=7)).date()
        today = [t for t in real if (datetime.fromisoformat(t["close_time"]) + timedelta(hours=7)).date() == bkk_today]
        return {"trades": len(real), "wins": len(wins),
                "win_rate": round(len(wins) / len(real) * 100, 1) if real else None,
                "total": round(sum(t["profit"] for t in real), 2), "today": round(sum(t["profit"] for t in today), 2),
                "today_trades": len(today), "today_wins": sum(1 for t in today if t["profit"] > 0), "best": max((t["profit"] for t in real), default=None),
                "worst": min((t["profit"] for t in real), default=None)}

    def _collect(self) -> dict:
        mt5 = self.mt5
        acc, term = mt5.account_info(), mt5.terminal_info()
        if acc is None:
            raise ConnectionError(f"account_info: {mt5.last_error()}")
        hbs = self._heartbeats()
        deals = self._deals()
        trades = self._trades(deals)
        open_pos = self._open()
        bal, curve = 0.0, []
        for d in sorted(deals, key=lambda d: d.time_msc):
            bal += d.profit + d.commission + d.swap + d.fee
            curve.append([_utc(d.time), round(bal, 2)])
        quotes = {}
        for sym in {s.get("symbol") for hb in hbs.values() for s in (hb.get("strategies") or {}).values()} - {None}:
            tick = mt5.symbol_info_tick(self._name(sym))
            if tick:
                quotes[sym] = {"bid": tick.bid, "ask": tick.ask}
        per_bot = {}
        for name in self.bots:
            mine = [t for t in trades if t["bot"] == name]
            by_tag: dict[str, dict] = {}            # ไม่นับไม้ทดสอบระบบ (smoke) — เหมือน _stats
            for t in mine:
                if t["tag"] == "smoke":
                    continue
                x = by_tag.setdefault(t["tag"], {"n": 0, "wins": 0, "pnl": 0.0})
                x["n"] += 1
                x["wins"] += t["profit"] > 0
                x["pnl"] = round(x["pnl"] + t["profit"], 2)
            per_bot[name] = {**self._stats(mine), "by_tag": by_tag,
                             "last_close": mine[0]["close_time"] if mine else None,
                             "open": sum(1 for p in open_pos if p["bot"] == name),
                             "floating": round(sum(p["profit"] for p in open_pos if p["bot"] == name), 2)}
        return {
            "now": datetime.now(UTC).isoformat(),
            "bot": hbs.get("main", {}), "bots": hbs, "per_bot": per_bot,
            "research": {b["bot"]: b for b in C.load_m5_bots()},
            "disabled": _settings.load()["disabled_bots"],
            "account": {"login": acc.login, "server": acc.server, "balance": acc.balance, "equity": acc.equity,
                        "margin": acc.margin, "free_margin": acc.margin_free, "currency": acc.currency,
                        "demo": acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
                        "floating": round(acc.equity - acc.balance, 2)},
            "terminal": {"connected": bool(term and term.connected), "algo": bool(term and term.trade_allowed)},
            "open": open_pos, "trades": trades[:50], "stats": self._stats(trades), "quotes": quotes,
            "equity_curve": curve[-500:], "log": self._log_tail(),
        }

    # ------------------------------------------------------------------ Signal Radar
    def radar(self) -> dict:
        return self._cached("radar", self._radar)

    def _radar(self) -> dict:
        hbs = self._heartbeats()
        try:
            stats_doc = json.loads(RADAR_STATS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stats_doc = {"generated_at": None, "strategies": {}}
        stats = stats_doc.get("strategies", {})
        trades = self._trades(self._deals())
        open_pos = self._open()
        cards = []
        for inst, hb in hbs.items():
            spec = C.LIVE_INSTANCES[inst]
            prof = C.RISK_PROFILES[spec["profile"]]
            sts = hb.get("strategies") or {}
            for tag in spec["strategies"]:
                st = sts.get(tag, {})
                mine = [t for t in trades if t["tag"] == tag]
                live_r = [t["profit"] for t in mine]
                cards.append({
                    "tag": tag, "instance": inst, "tf": spec["tf"], "alive": hb.get("alive", False),
                    "symbol": st.get("symbol") or stats.get(tag, {}).get("symbol"),
                    "status": st, "stats": stats.get(tag, {}),
                    "risk_usd": round((spec.get("allocation") or 1000.0) * prof.risk_per_trade, 2),
                    "min_lot_cap": prof.min_lot_max_risk,
                    "live": {"trades": len(mine), "wins": sum(1 for x in live_r if x > 0),
                             "pnl": round(sum(live_r), 2)},
                    "open": [o for o in open_pos if o["tag"] == tag],
                })
        cards.sort(key=lambda c: (-(1 if c["open"] else 0), -(c["status"].get("proximity") or 0)))
        return {"now": datetime.now(UTC).isoformat(), "stats_generated_at": stats_doc.get("generated_at"),
                "cards": cards}

    # ------------------------------------------------------------------ ประวัติแบ่งหน้า
    def trades_page(self, bot: str, page: int, size: int) -> dict:
        def build() -> dict:
            rows = [t for t in self._trades(self._deals()) if t["bot"] == bot]
            pages = max((len(rows) + size - 1) // size, 1)
            p = min(max(page, 1), pages)
            return {"bot": bot, "total": len(rows), "page": p, "pages": pages, "size": size,
                    "rows": rows[(p - 1) * size: p * size]}
        return self._cached(f"trades:{bot}:{page}:{size}", build)

    # ------------------------------------------------------------------ กราฟ
    def bars(self, symbol: str, tf: str, n: int) -> dict:
        return self._cached(f"bars:{symbol}:{tf}:{n}", lambda: self._bars(symbol, tf, n))

    def _bars(self, symbol: str, tf: str, n: int) -> dict:
        mt5 = self.mt5
        if symbol not in C.DEFAULT_SPECS or tf not in TIMEFRAMES:
            return {"error": "bad symbol/tf"}
        rates = mt5.copy_rates_from_pos(self._name(symbol), getattr(mt5, f"TIMEFRAME_{tf}"), 0, n)
        if rates is None:
            raise ConnectionError(f"copy_rates: {mt5.last_error()}")
        bars = [[_utc(int(r["time"])), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
                for r in rates]
        first = bars[0][0] if bars else ""
        trades = [t for t in self._trades(self._deals()) if t["symbol"] == symbol and t["close_time"] >= first]
        tick = mt5.symbol_info_tick(self._name(symbol))
        return {"symbol": symbol, "tf": tf, "bars": bars, "bid": tick.bid if tick else None,
                "open": [p for p in self._open() if p["symbol"] == symbol], "trades": trades[:60]}


def settings_payload() -> dict:
    return {"values": _settings.load(), "defaults": _settings.DEFAULTS, "timeframes": _settings.TIMEFRAMES,
            "limits": _settings.LIMITS, "bots": [n for n in C.LIVE_INSTANCES if n != "main"]}


SUPERVISED = sys.platform != "win32"   # Linux/Docker: python -m bot supervise อ่าน state/bot_settings.json แล้วเปิด/ปิดเอง


def _ps(command: str, wait: bool) -> None:
    import subprocess
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    kw = {"cwd": str(C.ROOT), "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    subprocess.run(args, timeout=60, **kw) if wait else subprocess.Popen(args, **kw)


def stop_bot(name: str) -> None:
    if SUPERVISED:
        time.sleep(3)              # supervisor เช็กไฟล์ทุก 1 วิ แล้วหยุดบอท (รอให้หยุดก่อนปิดไม้ กันบอทเปิดไม้ใหม่แข่ง)
        return
    _ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot live --instance %s(\\s|$)' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % name, wait=True)


def start_bot(name: str) -> None:
    if SUPERVISED:
        return
    _ps(f"& '{C.ROOT / 'start_bot.ps1'}' -Instance {name}", wait=False)


def bot_action(col: "Collector", body: dict) -> dict:
    """ปุ่มต่อบอทในหน้าเว็บ: close = ปิดไม้ตอนนี้ · disable = ปิดบอท (+ปิดไม้) · enable = เปิดบอท — main ไม่รับคำสั่ง"""
    name, action = body.get("bot"), body.get("action")
    if name == "main" or name not in C.LIVE_INSTANCES:
        raise ValueError("สั่งได้เฉพาะบอททดสอบ m5_1 … m5_9")
    if action not in ("close", "disable", "enable"):
        raise ValueError("คำสั่งไม่ถูกต้อง")
    s = _settings.load()
    if action == "disable":
        _settings.save({**s, "disabled_bots": s["disabled_bots"] + [name]})
        stop_bot(name)                              # หยุดบอทก่อน ไม่ให้เปิดไม้ใหม่แข่งกับการปิด
        return {"ok": True, "closed": col.close_bot_positions(name), "message": f"ปิด {name} แล้ว"}
    if action == "enable":
        _settings.save({**s, "disabled_bots": [b for b in s["disabled_bots"] if b != name]})
        start_bot(name)
        return {"ok": True, "message": f"กำลังเปิด {name} (~10 วิ)"}
    return {"ok": True, "closed": col.close_bot_positions(name), "message": f"ปิดไม้ของ {name} แล้ว"}


def stream_action(body: dict) -> dict:
    """หน้า /stream: save = บันทึก URL/key · start = ▶ Play · stop = ⏹ Stop"""
    from . import stream
    if stream.STREAMER is None:
        raise ValueError("ไลฟ์ได้เฉพาะบน Windows")
    action = body.get("action")
    if action == "save":
        stream.save_cfg(body)
        return {"ok": True, **stream.STREAMER.status()}
    if action == "start":
        return {"ok": True, **stream.STREAMER.start()}
    if action == "stop":
        return {"ok": True, **stream.STREAMER.stop()}
    raise ValueError("คำสั่งไม่ถูกต้อง")


def restart_test_bots() -> None:
    """รัน restart_bots.ps1 แบบไม่รอ (ใช้เวลาราว 10–30 วิ) — บอทอ่านค่าใหม่ตอนเริ่ม"""
    if SUPERVISED:
        return                     # supervisor เห็นไฟล์ค่าตั้งเปลี่ยน → รีสตาร์ทบอททดสอบเอง
    import subprocess
    script = C.ROOT / "restart_bots.ps1"
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=str(C.ROOT),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# โหมดสาธารณะ (เปิดให้คนทั่วไปดูผ่านโดเมน): หน้า /sandbox อย่างเดียว อ่านอย่างเดียว ไม่มีปุ่มสั่งงาน ไม่โชว์เลขบัญชี/log
PUBLIC = __import__("os").environ.get("PUBLIC_READONLY") == "1"
PUBLIC_SYMBOLS = ("BTCUSD", "XAUUSD")


def public_view(d: dict) -> dict:
    """ตัดข้อมูลที่ไม่ควรออกสู่สาธารณะ: เลขบัญชี/server ของโบรก, log, pid/magic"""
    acc = d.get("account") or {}
    bots = {n: {k: v for k, v in (b or {}).items() if k not in ("pid", "magic", "kill_reason")}
            for n, b in (d.get("bots") or {}).items()}
    return {
        "public": True, "now": d.get("now"), "bots": bots, "per_bot": d.get("per_bot"), "research": d.get("research"),
        "disabled": d.get("disabled"), "open": d.get("open"), "trades": (d.get("trades") or [])[:20],
        "quotes": d.get("quotes"), "stats": d.get("stats"),
        "account": {k: acc.get(k) for k in ("balance", "equity", "currency", "demo", "floating")},
        "error": "ระบบกำลังเชื่อมต่อ" if d.get("error") else None,
    }


def manager_payload() -> bytes:
    """แชทของผู้จัดการพอร์ต + บอทที่สั่งพักอยู่ (สาธารณะเห็นได้ — ไม่มีข้อมูลบัญชี)"""
    from .. import manager as M
    return json.dumps({"messages": M.read_log(80), "pauses": M.load_pauses()}, ensure_ascii=False).encode()


def make_handler(col: Collector) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if PUBLIC:
                self._public_get(url)
                return
            if url.path == "/api":
                self._send(json.dumps(col.data(), ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/techniques":
                self._send(TECHNIQUES.read_bytes(), "application/json")
            elif url.path == "/api/trades":
                q = parse_qs(url.query)
                d = col.trades_page(q.get("bot", ["main"])[0], int(q.get("page", ["1"])[0]),
                                    max(5, min(int(q.get("size", ["20"])[0]), 100)))
                self._send(json.dumps(d, ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/radar":
                self._send(json.dumps(col.radar(), ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/bars":
                q = parse_qs(url.query)
                n = max(20, min(int(q.get("n", ["150"])[0]), 500))
                d = col.bars(q.get("symbol", ["XAUUSD"])[0], q.get("tf", ["M5"])[0], n)
                self._send(json.dumps(d, ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/manager":
                self._send(manager_payload(), "application/json")
            elif url.path == "/api/version":
                # หน้าเว็บเช็กทุก 10 วิ — ไฟล์หน้าเปลี่ยน = รีโหลดตัวเอง (หน้าต่างไลฟ์ไม่ต้องปิด LIVE Studio จึงจับภาพต่อได้)
                page = PAGES.get(parse_qs(url.query).get("page", ["/sandbox"])[0])
                v = int(page.stat().st_mtime) if page else 0
                self._send(json.dumps({"v": v}).encode(), "application/json")
            elif url.path == "/api/stream":
                from .stream import STREAMER
                d = STREAMER.status() if STREAMER else {"error": "ไลฟ์ได้เฉพาะบน Windows"}
                self._send(json.dumps(d, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/api/settings":
                self._send(json.dumps(settings_payload(), ensure_ascii=False).encode(), "application/json")
            elif url.path in PAGES:
                self._send(PAGES[url.path].read_bytes(), "text/html")
            else:
                self.send_error(404)

        def _public_get(self, url) -> None:  # noqa: ANN001
            """โหมดสาธารณะ: เปิดได้เฉพาะสิ่งที่หน้า /sandbox ใช้ — ที่เหลือ 404"""
            if url.path == "/":
                self.send_response(302)
                self.send_header("Location", "/sandbox?stream=1")
                self.end_headers()
            elif url.path == "/sandbox":
                self._send(PAGES["/sandbox"].read_bytes(), "text/html")
            elif url.path == "/api":
                self._send(json.dumps(public_view(col.data()), ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/bars":
                q = parse_qs(url.query)
                sym, tf = q.get("symbol", ["BTCUSD"])[0], q.get("tf", ["M1"])[0]
                if sym not in PUBLIC_SYMBOLS or tf not in ("M1", "M5", "M15"):
                    self.send_error(404)
                    return
                d = col.bars(sym, tf, max(20, min(int(q.get("n", ["180"])[0]), 200)))
                d = {k: d.get(k) for k in ("symbol", "tf", "bars", "bid")}
                self._send(json.dumps(d, ensure_ascii=False, default=str).encode(), "application/json")
            elif url.path == "/api/manager":
                self._send(manager_payload(), "application/json")
            elif url.path == "/api/version":
                self._send(json.dumps({"v": int(PAGES["/sandbox"].stat().st_mtime)}).encode(), "application/json")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            """บันทึกค่าตั้ง + รีสตาร์ทบอททดสอบ — ช่องเดียวที่หน้าเว็บเขียนได้ (main ไม่ถูกแตะ)
            กันเว็บอื่นยิงมาทางเบราว์เซอร์ (CSRF): ต้องเป็น JSON + header เฉพาะ → เบราว์เซอร์ต้อง preflight ซึ่งเราไม่ตอบรับ"""
            if PUBLIC:
                self.send_error(403)        # สาธารณะ = อ่านอย่างเดียว ไม่มีทางสั่งงาน
                return
            path = urlparse(self.path).path
            if path not in ("/api/settings", "/api/bot", "/api/stream"):
                self.send_error(404)
                return
            if self.headers.get("X-Bot-Settings") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                self.send_error(403)
                return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                body = json.loads(self.rfile.read(n) or b"{}")
                if path == "/api/bot":
                    res = bot_action(col, body if isinstance(body, dict) else {})
                elif path == "/api/stream":
                    res = stream_action(body if isinstance(body, dict) else {})
                else:
                    current = _settings.load()          # หน้าตั้งค่าไม่ได้ส่งรายชื่อบอทที่ปิด → คงค่าเดิมไว้
                    res = {"ok": True, "saved": _settings.save({"disabled_bots": current["disabled_bots"], **body})}
                    restart_test_bots()
            except (ValueError, TypeError, ConnectionError, RuntimeError, OSError) as e:
                res = {"ok": False, "error": str(e)}
            self._send(json.dumps(res, ensure_ascii=False, default=str).encode(), "application/json")

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # เงียบ ไม่ต้อง log ทุก request
            pass

    return Handler


def serve(port: int = 8765, open_browser: bool = True, page: str = "") -> None:
    col = Collector()
    import os
    srv = ThreadingHTTPServer((os.environ.get("MONITOR_HOST", "127.0.0.1"), port), make_handler(col))
    url = f"http://127.0.0.1:{port}"
    print(f"monitor: {url}   กราฟหลายจอ: {url}/charts   Signal Radar: {url}/radar   คลังเทคนิค: {url}/techniques   (Ctrl+C เพื่อปิด)")
    if open_browser:
        import webbrowser
        webbrowser.open(url + page)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
