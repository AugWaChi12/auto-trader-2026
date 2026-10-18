"""
mt5api.py — ที่เดียวที่โหลด MetaTrader5 ให้ทั้งระบบ

  Windows (ปกติ)        : ไลบรารี MetaTrader5 ตัวจริง คุยกับ terminal ในเครื่อง
  Linux / Docker        : ตั้ง MT5_HOST (+ MT5_PORT) → คุยกับ MT5 ที่รันใน Wine (image gmag11/metatrader5_vnc)
                          ผ่าน RPyC server ของ image นั้น (port 8001)

ตัวเชื่อมต่อ remote ส่งข้อมูลไป-กลับเป็น JSON ล้วน (ไม่ส่งอ็อบเจกต์ข้ามเครื่อง) → ไม่ต้องให้ Python/numpy สองฝั่งเวอร์ชันตรงกัน
  · namedtuple ของ MT5 (TradePosition, AccountInfo, …) → SimpleNamespace (ใช้ p.ticket ได้เหมือนเดิม)
  · numpy structured array (copy_rates_*) → สร้าง structured array ใหม่ฝั่งนี้ (r['time'] ได้เหมือนเดิม)
  · datetime ในอาร์กิวเมนต์ → timestamp UTC
  · shutdown() = ปิดแค่การเชื่อมต่อของโปรเซสนี้ (MT5 ฝั่ง Wine ใช้ร่วมกันทุกบอท ห้ามให้ตัวเดียวปิดของทุกตัว)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

_REMOTE_CODE = r'''
import json, datetime
import MetaTrader5 as mt5

def _enc(x):
    if hasattr(x, "_asdict"):
        return {"__nt__": {k: _enc(v) for k, v in x._asdict().items()}}
    if isinstance(x, (tuple, list)):
        return [_enc(v) for v in x]
    dt = getattr(x, "dtype", None)
    if dt is not None and getattr(dt, "names", None):
        return {"__arr__": [list(d) for d in dt.descr], "rows": x.tolist()}
    if hasattr(x, "tolist"):
        return x.tolist()
    if isinstance(x, dict):
        return {k: _enc(v) for k, v in x.items()}
    if isinstance(x, datetime.datetime):
        return x.timestamp()
    return x

def _dec(x):
    if isinstance(x, dict):
        if "__dt__" in x:
            return datetime.datetime.fromtimestamp(x["__dt__"], datetime.timezone.utc)
        return {k: _dec(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_dec(v) for v in x]
    return x

def _mt5call(name, payload):
    a = _dec(json.loads(payload))
    return json.dumps(_enc(getattr(mt5, name)(*a["args"], **a["kwargs"])))

def _mt5consts():
    return json.dumps({k: getattr(mt5, k) for k in dir(mt5)
                       if k.isupper() and isinstance(getattr(mt5, k), (int, float, str))})
'''


def _encode(x: Any) -> Any:
    if isinstance(x, datetime):
        return {"__dt__": (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).timestamp()}
    if isinstance(x, dict):
        return {k: _encode(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_encode(v) for v in x]
    return x


def _decode(x: Any) -> Any:
    if isinstance(x, dict):
        if "__nt__" in x:
            return SimpleNamespace(**{k: _decode(v) for k, v in x["__nt__"].items()})
        if "__arr__" in x:
            import numpy as np
            return np.array([tuple(r) for r in x["rows"]], dtype=[tuple(d) for d in x["__arr__"]])
        return {k: _decode(v) for k, v in x.items()}
    if isinstance(x, list):
        return tuple(_decode(v) for v in x)        # MT5 คืน tuple (positions_get ฯลฯ)
    return x


class RemoteMT5:
    """หน้าตาเหมือนโมดูล MetaTrader5: mt5.initialize(), mt5.positions_get(), mt5.ORDER_TYPE_BUY …"""

    def __init__(self, host: str, port: int = 8001, timeout: float = 120.0) -> None:
        self._addr, self._timeout = (host, port), timeout
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        import rpyc
        self._conn = rpyc.classic.connect(*self._addr, keepalive=True)
        self._conn._config["sync_request_timeout"] = self._timeout   # copy_rates 99k แท่งใช้เวลาหลายวินาที
        self._conn.execute(_REMOTE_CODE)
        self._call = self._conn.namespace["_mt5call"]
        self.__dict__.update(json.loads(self._conn.namespace["_mt5consts"]()))

    def __getattr__(self, name: str):  # เรียกเฉพาะชื่อที่ไม่ใช่ค่าคงที่ → ฟังก์ชันของ MT5
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            payload = json.dumps(_encode({"args": list(args), "kwargs": kwargs}))
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                return _decode(json.loads(self._call(name, payload)))
            except (EOFError, OSError) as e:          # container MT5 รีสตาร์ท / เน็ตหลุด → ต่อใหม่ครั้งถัดไป
                self._conn = None
                raise ConnectionError(f"MT5 remote {self._addr}: {e!r}") from e
        return call

    def shutdown(self) -> bool:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
        return True


_cached: Any = None


def load() -> Any:
    """โมดูล MetaTrader5 (Windows) หรือ RemoteMT5 (เมื่อตั้ง MT5_HOST) — โปรเซสละ 1 การเชื่อมต่อ"""
    global _cached
    if _cached is None:
        host = os.environ.get("MT5_HOST")
        if host:
            _cached = RemoteMT5(host, int(os.environ.get("MT5_PORT", "8001")))
        else:
            import MetaTrader5 as mt5  # import ช้า → โหลดเมื่อใช้จริงเท่านั้น
            _cached = mt5
    return _cached
