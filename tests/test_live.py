"""ทดสอบ Mt5Broker ด้วย MT5 ปลอม — ตรวจ demo guard, comment, retcode, การกู้สถานะหลังรีสตาร์ท"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import json

import pytest

from bot import config as C
from bot.core.types import OrderRequest, RejectReason, Side
from bot.live.mt5_broker import Mt5Broker, NotDemoAccount, decode_comment, encode_comment
from bot.risk.risk_manager import RiskConfig, RiskManager

MAGIC = 123


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    TIMEFRAME_M15 = 15
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1

    def __init__(self, trade_mode: int = 0, retcode: int = 10009) -> None:
        self.trade_mode = trade_mode
        self.retcode = retcode
        self.sent: list[dict] = []
        self.positions: list[NS] = []
        self.deals: list[NS] = []
        self.next_ticket = 1000

    def initialize(self, path=None):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return NS(login=1, server="Demo", trade_mode=self.trade_mode, margin_mode=2, balance=1000.0,
                  equity=1000.0, margin_free=1000.0, currency="USD", leverage=200)

    def terminal_info(self):
        return NS(trade_allowed=True, connected=True)

    def symbols_get(self):
        return [NS(name="GOLD")]

    def symbol_select(self, name, flag):
        return True

    def symbol_info(self, name):
        return NS(point=0.01, digits=2, trade_contract_size=100.0, volume_min=0.01, volume_step=0.01,
                  volume_max=100.0, trade_stops_level=0, filling_mode=2)

    def symbol_info_tick(self, name):
        return NS(bid=4000.0, ask=4000.2, time=0)

    def order_send(self, req):
        self.sent.append(req)
        if self.retcode != 10009:
            return NS(retcode=self.retcode, order=0, price=0.0, comment="rejected")
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.next_ticket += 1
            self.positions.append(NS(ticket=self.next_ticket, magic=req["magic"], symbol=req["symbol"],
                                     type=req["type"], volume=req["volume"], price_open=req["price"], sl=req["sl"],
                                     tp=req["tp"], time=0, comment=req["comment"]))
            return NS(retcode=10009, order=self.next_ticket, price=req["price"], comment="done")
        return NS(retcode=10009, order=req.get("position", 0), price=0.0, comment="done")

    def positions_get(self, ticket=None, symbol=None):
        return tuple(p for p in self.positions if ticket is None or p.ticket == ticket)

    def history_deals_get(self, a, b):
        return tuple(self.deals)


def broker(mt5: FakeMT5, tmp: Path) -> Mt5Broker:
    b = Mt5Broker(["XAUUSD"], C.DEFAULT_SPECS, "M15", "ny_close", MAGIC, tmp, mt5=mt5)
    b.connect(None)
    return b


def test_comment_roundtrip_and_length() -> None:
    c = encode_comment("xau_momo:2026-10-15#12", "runner")
    assert c == "xau_momo:2026-10-15#12|r" and len(c) <= 31
    assert decode_comment(c) == ("xau_momo", "xau_momo:2026-10-15", "xau_momo:2026-10-15#12", "runner")
    assert decode_comment("[sl 4000.00]") is None
    assert decode_comment("manual trade") is None


def test_refuses_real_account(tmp_path: Path) -> None:
    with pytest.raises(NotDemoAccount):
        broker(FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL), tmp_path)


def test_send_uses_magic_comment_and_server_side_stops(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    b = broker(mt5, tmp_path)
    r = b.send(OrderRequest("XAUUSD", Side.BUY, 0.01, 3955.0, None, "xau_momo", "xau_momo:2026-10-15#1", "single"))
    assert r.accepted and r.fill_price == 4000.2
    req = mt5.sent[0]
    assert req["magic"] == MAGIC and req["sl"] == 3955.0 and req["tp"] == 0.0
    assert req["symbol"] == "GOLD" and req["type_filling"] == FakeMT5.ORDER_FILLING_IOC


def test_retcode_mapping(tmp_path: Path) -> None:
    for code, reason in ((10019, RejectReason.NO_MONEY), (10027, RejectReason.TRADE_DISABLED),
                         (10016, RejectReason.INVALID_STOPS), (10004, RejectReason.PRICE_CHANGED)):
        b = broker(FakeMT5(retcode=code), tmp_path)
        r = b.send(OrderRequest("XAUUSD", Side.BUY, 0.01, 3955.0, None, "t", "t:2026-10-15#1", "single"))
        assert not r.accepted and r.reason is reason


def test_rejects_stop_on_wrong_side_before_sending(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    b = broker(mt5, tmp_path)
    r = b.send(OrderRequest("XAUUSD", Side.SELL, 0.01, 3990.0, None, "t", "t:2026-10-15#1", "single"))
    assert r.reason is RejectReason.INVALID_STOPS and not mt5.sent


def test_restart_restores_positions_meta_and_keys(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    b1 = broker(mt5, tmp_path)
    b1.send(OrderRequest("XAUUSD", Side.BUY, 0.01, 3955.0, None, "xau_momo", "xau_momo:2026-10-15#1", "single"))
    pos = b1.positions()[0]
    pos.moved_to_be, pos.best_price = True, 4050.0
    b1.modify(pos.ticket, 4000.0, None)          # เลื่อน SL ฝั่ง server
    mt5.positions[0].sl = 4000.0
    b1.save_meta()
    mt5.positions.append(NS(ticket=9, magic=999, symbol="GOLD", type=0, volume=1.0, price_open=1, sl=0, tp=0,
                            time=0, comment="manual"))   # ไม้ที่คนเปิดเอง

    b2 = broker(mt5, tmp_path)                   # "รีสตาร์ท"
    ps = b2.positions()
    assert len(ps) == 1                          # ไม่เห็นไม้ manual
    p = ps[0]
    assert p.initial_sl == 3955.0 and p.sl == 4000.0 and p.moved_to_be and p.best_price == 4050.0
    assert p.tag == "xau_momo" and p.role == "single"
    assert "xau_momo:2026-10-15" in b2.consumed_keys()


def test_min_lot_mode_and_risk_state(tmp_path: Path) -> None:
    b = broker(FakeMT5(), tmp_path)
    spec = b.spec("XAUUSD")
    from bot.core.types import EnterSignal
    sig = EnterSignal("XAUUSD", Side.BUY, 3955.0, None, "t", "k")       # SL $45 → 0.01 lot เสี่ยง ~4.6%
    assert not RiskManager(RiskConfig(), b).size(sig, 4000.2, spec).ok
    d = RiskManager(RiskConfig(min_lot_max_risk=0.06), b).size(sig, 4000.2, spec)
    assert d.volume == 0.01 and "min_lot" in d.reason
    assert not RiskManager(RiskConfig(min_lot_max_risk=0.03), b).size(sig, 4000.2, spec).ok

    rm = RiskManager(RiskConfig(), b)
    rm.killed, rm.kill_reason, rm.peak_equity = True, "test", 1500.0
    rm2 = RiskManager(RiskConfig(), b)
    rm2.load_state(rm.to_state())
    assert rm2.killed and rm2.peak_equity == 1500.0


def test_virtual_allocation_isolates_bots(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    mt5.deals = [NS(magic=MAGIC, profit=-30.0, commission=-0.5, swap=0.0, fee=0.0),
                 NS(magic=999, profit=-500.0, commission=0.0, swap=0.0, fee=0.0)]   # บอทอื่นขาดทุนหนัก
    b = Mt5Broker(["XAUUSD"], C.DEFAULT_SPECS, "M15", "ny_close", MAGIC, tmp_path, mt5=mt5, allocation=1000.0)
    b.connect(None)
    assert b.balance() == pytest.approx(969.5)            # ไม่โดนขาดทุนของ magic อื่น
    mt5.positions.append(NS(ticket=5, magic=MAGIC, symbol="GOLD", type=0, volume=0.01, price_open=1, sl=0, tp=0,
                            time=0, comment="t:2026-10-15#1|s", profit=12.0, swap=0.0))
    mt5.positions.append(NS(ticket=6, magic=999, symbol="GOLD", type=0, volume=1, price_open=1, sl=0, tp=0,
                            time=0, comment="x", profit=-200.0, swap=0.0))
    assert b.equity() == pytest.approx(981.5)
    assert b.free_margin() == pytest.approx(981.5)        # ไม่เกิน equity เสมือน


def test_state_write_failure_never_breaks_trading(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """บั๊กจริง 2026-10-15: os.replace โดน PermissionError → ต้องไม่หลุดออกมาจาก send()"""
    import bot.live.fileio as fio
    mt5 = FakeMT5()
    b = broker(mt5, tmp_path)

    def locked(src, dst):  # noqa: ANN001, ANN202
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(fio.os, "replace", locked)
    monkeypatch.setattr(fio.time, "sleep", lambda s: None)
    r = b.send(OrderRequest("XAUUSD", Side.BUY, 0.01, 3955.0, None, "t", "t:2026-10-15#1", "single"))
    assert r.accepted                                   # ออเดอร์สำเร็จ แม้บันทึกไฟล์ไม่ได้
    assert not list(tmp_path.glob("*.tmp"))             # ไม่ทิ้งไฟล์ขยะ


def test_atomic_write_retries_then_succeeds(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    import os
    import bot.live.fileio as fio
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(fio.os, "replace", flaky)
    monkeypatch.setattr(fio.time, "sleep", lambda s: None)
    assert fio.atomic_write(tmp_path / "x.json", "{}")
    assert (tmp_path / "x.json").read_text() == "{}" and calls["n"] == 3


def test_connect_keeps_broker_min_stop_distance(tmp_path: Path) -> None:
    """spec ที่สร้างตอน connect ต้องมีระยะ SL ขั้นต่ำของโบรก — ไม่งั้น Engine ไม่ขยาย SL แล้วโบรกปฏิเสธทุกไม้ (บั๊ก 17 ต.ค.)"""
    mt5 = FakeMT5()
    mt5.symbol_info = lambda name: NS(point=0.01, digits=2, trade_contract_size=100.0, volume_min=0.01, volume_step=0.01,
                                      volume_max=100.0, trade_stops_level=20000, filling_mode=2)
    b = broker(mt5, tmp_path)
    assert b.spec("XAUUSD").min_stop == pytest.approx(200.0)


def test_monitor_bot_action_only_for_test_bots_and_disable_closes(tmp_path: Path, monkeypatch) -> None:
    from bot import settings
    from bot.monitor import server
    monkeypatch.setattr(settings, "PATH", tmp_path / "bot_settings.json")
    calls = []
    monkeypatch.setattr(server, "stop_bot", lambda n: calls.append(("stop", n)))
    monkeypatch.setattr(server, "start_bot", lambda n: calls.append(("start", n)))
    col = type("Col", (), {"close_bot_positions": lambda self, n: calls.append(("close", n)) or [{"ticket": 1, "ok": True}]})()
    for bad in ({"bot": "main", "action": "close"}, {"bot": "m5_1", "action": "rm"}, {"bot": "x", "action": "close"}):
        with pytest.raises(ValueError):
            server.bot_action(col, bad)
    assert server.bot_action(col, {"bot": "m5_2", "action": "disable"})["ok"]
    assert settings.load()["disabled_bots"] == ["m5_2"] and calls == [("stop", "m5_2"), ("close", "m5_2")]
    server.bot_action(col, {"bot": "m5_2", "action": "enable"})
    assert settings.load()["disabled_bots"] == [] and calls[-1] == ("start", "m5_2")


def test_stream_config_validates_and_never_exposes_key(tmp_path: Path, monkeypatch) -> None:
    from bot.monitor import stream
    monkeypatch.setattr(stream, "CFG_PATH", tmp_path / "stream.json")
    with pytest.raises(ValueError):
        stream.save_cfg({"server": "http://evil.example/x", "key": "abc"})
    with pytest.raises(ValueError):
        stream.save_cfg({"server": "rtmp://a/b", "key": "has space"})
    stream.save_cfg({"server": "rtmp://push.example/live", "key": "SECRET1", "orientation": "portrait", "quality": "720p"})
    stream.save_cfg({"server": "rtmp://push.example/live", "key": ""})          # key ว่าง = คงค่าเดิม
    assert stream.load_cfg()["key"] == "SECRET1"
    st = stream.Streamer().status() if hasattr(stream, "Streamer") else {}
    assert "SECRET1" not in json.dumps(st) and st["has_key"] is True
    cmd = stream.build_cmd(stream.load_cfg(), (0, 0, 540, 960), "out.flv")
    assert "anullsrc" in " ".join(cmd) and "720:1280" in " ".join(cmd)
