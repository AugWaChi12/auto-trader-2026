"""
CLI

    python -m bot synth                         # สร้างข้อมูลสังเคราะห์ (ทดสอบระบบเท่านั้น)
    python -m bot download --years 3            # ดึงข้อมูลจริงจาก MT5 (ต้องเปิด MT5 ล็อกอินไว้)
    python -m bot backtest --data data/mt5      # รัน backtest
    python -m bot backtest --data data/mt5 --strategies xau_bo,xau_sweep --profile aggressive
    python -m bot smoke                         # ทดสอบส่ง/แก้/ปิดออเดอร์ 0.01 lot บน demo
    python -m bot live                          # รันบอทบน demo (Ctrl+C หยุด)
    python -m bot status                        # ดูสถานะบอท / ไม้ / ผลย้อนหลัง
    python -m bot monitor                       # หน้าเว็บ monitor ที่ http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C
from .core.timeutil import UTC


def cmd_synth(a: argparse.Namespace) -> None:
    from .data.store import bars_path, save_bars, save_spec
    from .data.synthetic import generate
    out = Path(a.out)
    for sym in a.symbols:
        df = generate(sym, datetime(2023, 1, 2), a.days, seed=a.seed, drift=a.drift)
        save_bars(bars_path(out, sym, C.TIMEFRAME), df)
        save_spec(out, C.DEFAULT_SPECS[sym])
        print(f"{sym}: {len(df)} แท่ง → {bars_path(out, sym, C.TIMEFRAME)}")
    print("!! ข้อมูลสังเคราะห์ใช้ทดสอบว่าโค้ดทำงาน ไม่ใช่ทดสอบว่ากลยุทธ์ดี")


def cmd_download(a: argparse.Namespace) -> None:
    from .data import mt5_source
    mt5_source.connect()
    mt5_source.check_time_mode(C.SERVER_TIME_MODE)
    end = datetime.now(UTC) + timedelta(days=1)   # MT5 ตีความเป็นเวลา server (UTC+2/+3) → เผื่อไว้ไม่ให้ตกแท่งล่าสุด
    start = end - timedelta(days=int(a.years * 365))
    for sym in a.symbols:
        mt5_source.download(sym, a.tf, start, end, Path(a.out), C.SERVER_TIME_MODE, C.DEFAULT_SPECS[sym])


def cmd_backtest(a: argparse.Namespace) -> None:
    from .backtest.report import build_report, print_report, save_report
    from .backtest.runner import run_backtest
    from .data.store import bars_path, load_bars, load_spec
    from .risk.news import NewsCalendar

    data = Path(a.data)
    tags = a.strategies.split(",") if a.strategies else None
    strategies = C.build_strategies(tags, a.symbols, a.tf)
    if not strategies:
        raise SystemExit("ไม่มีกลยุทธ์ที่ตรงกับตัวกรอง")
    symbols = sorted({s.symbol for s in strategies})

    bars, specs = {}, {}
    for sym in symbols:
        path = bars_path(data, sym, a.tf)
        if not path.exists():
            raise SystemExit(f"ไม่พบ {path} — รัน download หรือ synth ก่อน")
        series = load_bars(path)
        if a.start:
            series = [b for b in series if b.time >= datetime.fromisoformat(a.start).replace(tzinfo=UTC)]
        if a.end:
            series = [b for b in series if b.time < datetime.fromisoformat(a.end).replace(tzinfo=UTC)]
        bars[sym] = series
        specs[sym] = load_spec(data, sym, C.DEFAULT_SPECS[sym])
        print(f"{sym}: {len(series)} แท่ง  contract={specs[sym].contract_size} min_lot={specs[sym].volume_min}")

    years = (min(b[0].time.year for b in bars.values()), max(b[-1].time.year for b in bars.values()))
    news = None if a.no_news else NewsCalendar.default(C.NEWS_CSV, years, C.NEWS_BLACKOUT_MIN)
    risk = C.RISK_PROFILES[a.profile]
    if a.no_kill:
        risk = replace(risk, max_drawdown=1.0)
    print(f"risk profile: {a.profile} ({risk.risk_per_trade:.2%}/ไม้, หยุดวันที่ -{risk.daily_loss_limit:.0%}, "
          f"kill ที่ DD {risk.max_drawdown:.0%})  กลยุทธ์: {', '.join(s.tag for s in strategies)}")

    res = run_backtest(bars, specs, strategies, risk, a.tf, a.balance, news)
    rep = build_report(res)
    print_report(rep)
    out = Path(a.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    save_report(res, rep, out)
    print(f"\nบันทึกผลไว้ที่ {out}")


def _live_broker(symbols: list[str]):
    from .live.mt5_broker import Mt5Broker
    from .live.runner import _find_terminal
    b = Mt5Broker(symbols, C.DEFAULT_SPECS, C.TIMEFRAME, C.SERVER_TIME_MODE, C.MAGIC, C.STATE_DIR)
    b.connect(_find_terminal())
    return b


def cmd_live(a: argparse.Namespace) -> None:
    from .live.runner import LiveRunner, setup_file_logging
    path = setup_file_logging(C.LOG_DIR, a.instance)
    print(f"log: {path}")
    try:
        LiveRunner(a.instance, a.strategies.split(",") if a.strategies else None, a.profile).run_forever()
    except Exception:
        logging.getLogger("live").exception("บอทหยุดเพราะ error ที่ไม่คาดคิด")   # แบบไม่มีหน้าต่าง: เห็นได้จาก log เท่านั้น
        raise


def cmd_smoke(a: argparse.Namespace) -> None:
    """ส่งออเดอร์จริงบน demo: เปิด → เลื่อน SL → ปิด เพื่อตรวจ filling mode / retcode / comment / magic"""
    from .core.types import OrderRequest, Side
    b = _live_broker(["XAUUSD"])
    if not b.algo_trading_enabled():
        raise SystemExit("ปุ่ม Algo Trading ใน MT5 ปิดอยู่ — กดเปิดก่อนแล้วรันใหม่")
    tick = b.mt5.symbol_info_tick(b._names["XAUUSD"])
    group = f"smoke:{datetime.now(UTC):%m%d%H%M}#1"
    r = b.send(OrderRequest("XAUUSD", Side.BUY, 0.01, tick.bid - 20, tick.ask + 20, "smoke", group, "single"))
    print("open  :", r)
    if not r.accepted:
        raise SystemExit(1)
    pos = [p for p in b.positions("XAUUSD", "smoke") if p.ticket == r.ticket]
    print("found :", pos[0] if pos else "NOT FOUND (comment/magic ไม่ตรง?)")
    tick = b.mt5.symbol_info_tick(b._names["XAUUSD"])
    print("modify:", b.modify(r.ticket, tick.bid - 15, tick.ask + 20))
    print("close :", b.close(r.ticket, "smoke_close"))
    import time
    time.sleep(1)
    deals = [d for d in b.bot_deals(datetime.now(UTC) - timedelta(hours=1)) if d.position_id == r.ticket]
    for d in deals:
        print(f"deal  : {'IN ' if d.entry == 0 else 'OUT'} price={d.price} profit={d.profit} commission={d.commission} "
              f"comment={d.comment!r}")
    print("keys  :", sorted(k for k in b.consumed_keys(1) if k.startswith("smoke")))
    print("SMOKE OK" if len(deals) >= 2 and not b.positions("XAUUSD", "smoke") else "SMOKE: ตรวจผลด้านบน")


def cmd_status(a: argparse.Namespace) -> None:
    import json
    hb_path = C.STATE_DIR / "heartbeat.json"
    if hb_path.exists():
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
        age = datetime.now(UTC) - datetime.fromisoformat(hb["time"])
        alive = "🟢 ทำงานอยู่" if age < timedelta(minutes=2) else f"🔴 เงียบไป {age}"
        print(f"บอท: {alive}  (pid {hb['pid']}, algo_trading={hb['algo_trading']}, killed={hb['killed']})")
        print(f"     แท่งล่าสุด: {hb['last_bars']}")
    else:
        print("บอท: ยังไม่เคยรัน")
    b = _live_broker(sorted({s.symbol for s in C.build_strategies(C.LIVE_STRATEGIES)}))
    acc = b.mt5.account_info()
    print(f"บัญชี: {acc.login}@{acc.server}  balance {acc.balance:,.2f}  equity {acc.equity:,.2f}")
    pos = b.positions()
    print(f"ไม้ที่เปิด ({len(pos)}):")
    for p in pos:
        print(f"  #{p.ticket} {p.symbol} {p.side.name} {p.volume} @{p.entry_price} sl={p.sl} tp={p.tp} [{p.group_id} {p.role}]")
    deals = [d for d in b.bot_deals(datetime.now(UTC) - timedelta(days=a.days)) if d.entry == 1]
    total = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
    print(f"ปิดไปแล้ว {len(deals)} ดีล ใน {a.days} วัน  P/L รวม {total:+.2f}")
    from .core.timeutil import server_to_utc
    for d in deals[-10:]:
        t = server_to_utc(datetime.fromtimestamp(d.time, UTC).replace(tzinfo=None), C.SERVER_TIME_MODE)
        print(f"  {t:%m-%d %H:%M} UTC #{d.position_id} {d.profit + d.commission:+.2f}  {d.comment}")


def cmd_radar_stats(a: argparse.Namespace) -> None:
    import runpy
    runpy.run_path(str(C.ROOT / "research" / "radar_stats.py"), run_name="__main__")


def cmd_monitor(a: argparse.Namespace) -> None:
    from .live.awake import keep_awake
    from .monitor.server import serve
    keep_awake(display=True)   # หน้า monitor เปิดโชว์ → ไม่ให้จอดับ
    serve(a.port, not a.no_browser, "/charts" if a.charts else "")


def main() -> None:
    # Windows console (cp874) พิมพ์ภาษาไทย/สัญลักษณ์บางตัวไม่ได้เมื่อ output ถูก pipe
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:      # pythonw.exe (รันแบบไม่มีหน้าต่าง) ไม่มี stdout/stderr
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="bot")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="สร้างข้อมูลสังเคราะห์")
    s.add_argument("--symbols", nargs="+", default=["XAUUSD", "NAS100"])
    s.add_argument("--days", type=int, default=730)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--drift", type=float, default=0.15, help="0 = random walk ล้วน (ตรวจ look-ahead)")
    s.add_argument("--out", default="data/synthetic")
    s.set_defaults(fn=cmd_synth)

    d = sub.add_parser("download", help="ดึงข้อมูลจาก MT5")
    d.add_argument("--symbols", nargs="+", default=["XAUUSD", "NAS100"])
    d.add_argument("--tf", default=C.TIMEFRAME)
    d.add_argument("--years", type=float, default=3)
    d.add_argument("--out", default="data/mt5")
    d.set_defaults(fn=cmd_download)

    b = sub.add_parser("backtest", help="รัน backtest")
    b.add_argument("--data", default="data/mt5")
    b.add_argument("--tf", default=C.TIMEFRAME)
    b.add_argument("--symbols", nargs="+")
    b.add_argument("--strategies", help="คั่นด้วย comma เช่น xau_bo,xau_sweep")
    b.add_argument("--profile", choices=list(C.RISK_PROFILES), default="normal")
    b.add_argument("--balance", type=float, default=C.INITIAL_BALANCE)
    b.add_argument("--start", help="YYYY-MM-DD")
    b.add_argument("--end", help="YYYY-MM-DD")
    b.add_argument("--no-kill", action="store_true", help="ปิด kill switch เพื่อดูผลครบทุกปี (โหมดวิจัย)")
    b.add_argument("--no-news", action="store_true", help="ไม่ใช้ช่วงงดเทรดรอบข่าว")
    b.add_argument("--out", default="reports")
    b.set_defaults(fn=cmd_backtest)

    lv = sub.add_parser("live", help="รันบอทบน demo")
    lv.add_argument("--instance", choices=list(C.LIVE_INSTANCES), default="main")
    lv.add_argument("--strategies", help="ทับรายชื่อกลยุทธ์ของ instance (คั่นด้วย comma)")
    lv.add_argument("--profile", choices=list(C.RISK_PROFILES), help="ทับ risk profile ของ instance")
    lv.set_defaults(fn=cmd_live)

    sm = sub.add_parser("smoke", help="ทดสอบส่งออเดอร์ 0.01 lot บน demo")
    sm.set_defaults(fn=cmd_smoke)

    st = sub.add_parser("status", help="สถานะบอท")
    st.add_argument("--days", type=int, default=7)
    st.set_defaults(fn=cmd_status)

    rs = sub.add_parser("radar-stats", help="คำนวณสถิติความน่าจะเป็นของหน้า Signal Radar")
    rs.set_defaults(fn=cmd_radar_stats)

    mo = sub.add_parser("monitor", help="หน้าเว็บ monitor (อ่านอย่างเดียว)")
    mo.add_argument("--port", type=int, default=8765)
    mo.add_argument("--no-browser", action="store_true")
    mo.add_argument("--charts", action="store_true", help="เปิดหน้ากราฟหลายจอ")
    mo.set_defaults(fn=cmd_monitor)

    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    a.fn(a)


if __name__ == "__main__":
    main()
