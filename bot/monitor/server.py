"""
server.py — หน้า monitor แบบเบา (อ่านอย่างเดียว ไม่ส่งออเดอร์)

    python -m bot monitor          → เปิด http://127.0.0.1:8765 ในเบราว์เซอร์

ใช้แค่ stdlib (http.server) + MetaTrader5 ไม่ต้องใช้อินเทอร์เน็ต
ผูกกับ 127.0.0.1 เท่านั้น — เครื่องอื่นในเครือข่ายเข้าไม่ได้
หน้าเว็บดึง /api ทุก 5 วินาที, ฝั่ง server cache ผล 2 วินาที → ต่อให้เปิดหลายแท็บก็ไม่หนักเครื่อง
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import config as C
from ..core.timeutil import UTC, server_to_utc
from ..live.mt5_broker import decode_comment

log = logging.getLogger("monitor")
HTML = Path(__file__).with_name("index.html")


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
    return {"eod": "ปิดสิ้นวัน", "halt": "risk halt", "stop_moved": "stop ย้าย", "end": "end"}.get(comment, comment or "-")


class Collector:
    def __init__(self) -> None:
        import MetaTrader5 as mt5
        self.mt5 = mt5
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None
        self._ok = False

    def _connect(self) -> None:
        from ..live.runner import _find_terminal
        path = _find_terminal()
        if not (self.mt5.initialize(path=path) if path else self.mt5.initialize()):
            raise ConnectionError(f"MT5: {self.mt5.last_error()}")
        self._ok = True

    def data(self) -> dict:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < 2:
                return self._cache[1]
            try:
                if not self._ok:
                    self._connect()
                d = self._collect()
            except Exception as e:  # noqa: BLE001
                self._ok = False
                d = {"error": str(e), "bot": self._heartbeat(), "log": self._log_tail()}
            self._cache = (time.monotonic(), d)
            return d

    # ------------------------------------------------------------------
    def _heartbeat(self) -> dict:
        p = C.STATE_DIR / "heartbeat.json"
        try:
            hb = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"alive": False, "seen": None}
        age = (datetime.now(UTC) - datetime.fromisoformat(hb["time"])).total_seconds()
        return {**hb, "alive": age < 90, "age_s": round(age)}

    def _log_tail(self, n: int = 25) -> list[str]:
        files = sorted(C.LOG_DIR.glob("live_*.log"))
        if not files:
            return []
        lines = files[-1].read_text(encoding="utf-8", errors="replace").splitlines()
        return [ln[11:] for ln in lines[-n:]]   # ตัดวันที่ออก เหลือเวลา

    def _collect(self) -> dict:
        mt5 = self.mt5
        acc = mt5.account_info()
        term = mt5.terminal_info()
        if acc is None:
            raise ConnectionError(f"account_info: {mt5.last_error()}")
        hb = self._heartbeat()

        # ไม้ที่เปิดอยู่ (ของบอทเท่านั้น)
        open_pos = []
        for p in mt5.positions_get() or ():
            if p.magic != C.MAGIC:
                continue
            dec = decode_comment(p.comment)
            open_pos.append({
                "ticket": p.ticket, "symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume, "open_time": _utc(p.time), "open_price": p.price_open, "price": p.price_current,
                "sl": p.sl or None, "tp": p.tp or None, "profit": round(p.profit + p.swap, 2),
                "tag": dec[0] if dec else "?", "role": dec[3] if dec else "",
            })

        # ประวัติ: รวม deal ตาม position
        deals = mt5.history_deals_get(datetime(2020, 1, 1, tzinfo=UTC), datetime.now(UTC) + timedelta(days=1)) or ()
        by_pos: dict[int, list] = defaultdict(list)
        equity_curve = []
        bal = 0.0
        for d in sorted(deals, key=lambda d: d.time_msc):
            bal += d.profit + d.commission + d.swap + d.fee
            equity_curve.append([_utc(d.time), round(bal, 2)])
            if d.magic == C.MAGIC and d.position_id:
                by_pos[d.position_id].append(d)

        trades = []
        for pid, ds in by_pos.items():
            ins = [d for d in ds if d.entry == mt5.DEAL_ENTRY_IN]
            outs = [d for d in ds if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
            if not ins or not outs:
                continue
            i, o = ins[0], outs[-1]
            dec = decode_comment(i.comment)
            pnl = sum(d.profit + d.commission + d.swap + d.fee for d in ds)
            trades.append({
                "ticket": pid, "symbol": i.symbol, "side": "BUY" if i.type == 0 else "SELL", "volume": i.volume,
                "open_time": _utc(i.time), "open_price": i.price, "close_time": _utc(o.time), "close_price": o.price,
                "reason": _exit_reason(o.comment), "profit": round(pnl, 2),
                "tag": dec[0] if dec else "?", "group": dec[2] if dec else "",
            })
        trades.sort(key=lambda t: t["close_time"], reverse=True)

        # สถิติ (ไม่นับ smoke test)
        real = [t for t in trades if t["tag"] != "smoke"]
        wins = [t for t in real if t["profit"] > 0]
        bkk_today = (datetime.now(UTC) + timedelta(hours=7)).date().isoformat()
        today = [t for t in real if (datetime.fromisoformat(t["close_time"]) + timedelta(hours=7)).date().isoformat() == bkk_today]
        stats = {
            "trades": len(real), "wins": len(wins), "win_rate": round(len(wins) / len(real) * 100, 1) if real else None,
            "total": round(sum(t["profit"] for t in real), 2), "today": round(sum(t["profit"] for t in today), 2),
            "today_trades": len(today), "best": max((t["profit"] for t in real), default=None),
            "worst": min((t["profit"] for t in real), default=None),
        }

        # ราคาปัจจุบันของ symbol ที่กลยุทธ์ใช้ (สำหรับแผง "สัญญาณวันนี้")
        quotes = {}
        for tag, st in (hb.get("strategies") or {}).items():
            sym = st.get("symbol")
            if sym:
                from ..data.mt5_source import find_symbol
                name = find_symbol(sym, mt5)
                tick = mt5.symbol_info_tick(name)
                if tick:
                    quotes[sym] = {"bid": tick.bid, "ask": tick.ask}

        return {
            "now": datetime.now(UTC).isoformat(),
            "bot": hb,
            "account": {"login": acc.login, "server": acc.server, "balance": acc.balance, "equity": acc.equity,
                        "margin": acc.margin, "free_margin": acc.margin_free, "currency": acc.currency,
                        "demo": acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
                        "floating": round(acc.equity - acc.balance, 2)},
            "terminal": {"connected": bool(term and term.connected), "algo": bool(term and term.trade_allowed)},
            "open": open_pos, "trades": trades[:100], "stats": stats, "quotes": quotes,
            "equity_curve": equity_curve[-500:], "log": self._log_tail(),
        }


def make_handler(col: Collector) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(col.data(), ensure_ascii=False, default=str).encode()
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                body = HTML.read_bytes()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # เงียบ ไม่ต้อง log ทุก request
            pass

    return Handler


def serve(port: int = 8765, open_browser: bool = True) -> None:
    col = Collector()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(col))
    url = f"http://127.0.0.1:{port}"
    print(f"monitor: {url}  (Ctrl+C เพื่อปิด)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
