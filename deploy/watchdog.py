#!/usr/bin/env python3
"""
watchdog.py — ตัวเฝ้าระบบบนเครื่องเซิร์ฟเวอร์ (นอก container · Python มาตรฐานล้วน) — cron เรียกทุก 5 นาที

    python3 /opt/auto-trader/deploy/watchdog.py            # ตรวจสุขภาพ + ซ่อมเองเท่าที่ปลอดภัย + แจ้ง Telegram
    python3 /opt/auto-trader/deploy/watchdog.py --backup   # สำรอง state/ → backups/ (เก็บ 14 วัน) + ส่งไฟล์เข้า Telegram
    python3 /opt/auto-trader/deploy/watchdog.py --install  # ใส่ cron ให้ (ทุก 5 นาที + สำรองตี 3:30 เวลาไทย)

ตรวจ: container ครบ · monitor ตอบ · MT5 ต่อโบรก + ปุ่ม Algo · บอทที่ควรทำงานยังมีชีวิต · ผู้จัดการพอร์ตยังเดิน · ดิสก์/แรม
ซ่อมเอง (ค่อยเป็นค่อยไป ไม่รัวรีสตาร์ท):
  container หยุด → docker compose up -d ตัวนั้น
  ผู้จัดการ/บอทเงียบเกิน 15 นาที → รีสตาร์ท container บอท (ไม้ที่เปิดอยู่มี SL/TP ที่โบรกอยู่แล้ว)
  MT5 หลุดเกิน 20 นาที → รีสตาร์ท container MT5 (ล็อกอินเดิมกลับมาเอง) — ไม่เกินชั่วโมงละครั้ง
แจ้ง Telegram เฉพาะตอนปัญหาเกิดใหม่ / หายแล้ว (ไม่ส่งซ้ำทุก 5 นาที)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
STATE = ROOT / "state"
BACKUPS = ROOT / "backups"
MEMO = Path("/var/lib/trader-watchdog.json")
CONTAINERS = {"trader-mt5": "mt5", "trader-bots": "bots", "trader-monitor": "monitor", "trader-public": "public", "trader-caddy": "caddy"}
API = "http://127.0.0.1:8765/api"
QUIET_MIN = 15            # ผู้จัดการ/บอทเงียบกี่นาทีถึงซ่อม
MT5_DOWN_MIN = 20
COOLDOWN_MIN = 60         # ซ่อมสิ่งเดิมซ้ำได้ไม่ถี่กว่านี้
# ปัญหาการเชื่อมต่อที่หายเองได้ในไม่กี่วินาที (โบรกตัดสั้นๆ ช่วงปิดปรับปรุงวันเสาร์ ฯลฯ) → แจ้งเมื่อยังเจอซ้ำในรอบถัดไป (≈ 5 นาที)
CONFIRM_TWICE = {"mt5", "api", "algo", "bots", "manager"}


def env() -> dict[str, str]:
    out = {}
    try:
        for ln in (DEPLOY / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def chat_id(e: dict[str, str]) -> str | None:
    if e.get("TELEGRAM_CHAT_ID"):
        return e["TELEGRAM_CHAT_ID"]
    try:
        return str(json.loads((STATE / "telegram_chat.json").read_text(encoding="utf-8"))["chat_id"])
    except (OSError, ValueError, KeyError):
        return None


def tg(method: str, fields: dict, file: tuple[str, bytes] | None = None) -> None:
    e = env()
    token, chat = e.get("TELEGRAM_BOT_TOKEN"), chat_id(e)
    if not token or not chat:
        print("[telegram off]", fields.get("text") or fields.get("caption"))
        return
    fields = {**fields, "chat_id": chat}
    url = f"https://api.telegram.org/bot{token}/{method}"
    if file is None:
        req = urllib.request.Request(url, data=json.dumps(fields).encode(), headers={"Content-Type": "application/json"})
    else:
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{file[0]}"\r\n'
                     f"Content-Type: application/gzip\r\n\r\n".encode() + file[1] + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request(url, data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as ex:  # noqa: BLE001
        print("telegram failed:", ex)


def sh(*cmd: str, timeout: int = 120) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(DEPLOY)).stdout
    except Exception as ex:  # noqa: BLE001
        return f"ERR {ex}"


def age_min(p: Path) -> float | None:
    try:
        return (time.time() - p.stat().st_mtime) / 60
    except OSError:
        return None


FAIL_MARK = "not sent: "
FAIL_REASONS = ("rejected:connection", "rejected:unknown", "stale_data")   # stale_data ทั้งระบบ = นาฬิกาโบรกเพี้ยน (บทเรียน 27 ต.ค. 2026)   # ความผิดปกติของระบบ — ไม่นับ invalid_stops / market_closed ฯลฯ ที่เป็นเรื่องปกติของตลาด


def failed_orders(minutes: int) -> list[tuple[str, str]]:
    """ออเดอร์ที่บอทส่งแล้วระบบล้มเหลว (ไม่ใช่ถูกตลาดปฏิเสธตามปกติ) ใน N นาทีล่าสุด — บทเรียน 18 ต.ค. 2026: ล้ม 53 ครั้งเงียบๆ ทั้งวัน"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    out = []
    # ไฟล์ log ของบอทชื่อตามวันที่เปิด แล้วเขียนต่อข้ามวัน → ดูทุกไฟล์ที่แก้ล่าสุด (เดิมดูแค่ชื่อวันนี้ → ตาบอดหลังบอทรีสตาร์ท)
    for f in [p for p in (ROOT / "logs").glob("*.log") if p.stat().st_mtime >= cutoff.timestamp()]:
        try:
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]:
                i = ln.find(FAIL_MARK)
                if i < 0 or not ln[i + len(FAIL_MARK):].startswith(FAIL_REASONS):
                    continue
                try:
                    t = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if t >= cutoff:
                    out.append((f.name.rsplit("_", 1)[0], ln[i + len(FAIL_MARK):]))
        except OSError:
            pass
    return out


def check() -> dict[str, str]:
    """คืน {รหัสปัญหา: คำอธิบาย} — ว่าง = ปกติ"""
    probs: dict[str, str] = {}
    running = set(sh("docker", "ps", "--format", "{{.Names}}").split())
    for name in CONTAINERS:
        if name not in running:
            probs[f"down:{name}"] = f"container {name} ไม่ทำงาน"
    try:
        d = json.loads(urllib.request.urlopen(API, timeout=25).read().decode())
    except Exception as ex:  # noqa: BLE001
        d = None
        probs["api"] = f"หน้า monitor ไม่ตอบ ({type(ex).__name__})"
    if d is not None:
        if d.get("error"):
            probs["mt5"] = f"ต่อ MT5 ไม่ได้: {str(d['error'])[:120]}"
        else:
            term = d.get("terminal") or {}
            if not term.get("connected"):
                probs["mt5"] = "MT5 ไม่ได้ต่อกับโบรก"
            sk = term.get("clock_skew_min")
            if sk is not None and abs(sk) >= 30:
                probs["clock"] = (f"เวลาโบรกต่างจากที่บอทคำนวณ {sk:+d} นาที — แท่งราคาจะดูเก่าและสัญญาณถูกทิ้ง (stale_data) "
                                  "ตรวจ SERVER_TIME_MODE ใน bot/config.py")
            elif not term.get("algo"):
                probs["algo"] = "ปุ่ม Algo Trading ใน MT5 ปิดอยู่ — บอทส่งออเดอร์ไม่ได้"
            disabled = set(d.get("disabled") or [])
            try:
                pauses = {b for b, p in json.loads((STATE / "manager_pauses.json").read_text(encoding="utf-8")).items()
                          if p.get("until", 0) > time.time()}
            except (OSError, ValueError):
                pauses = set()
            dead = [n for n, hb in (d.get("bots") or {}).items()
                    if n not in disabled and n not in pauses and not (hb or {}).get("alive")]
            if dead:
                probs["bots"] = "บอทไม่ส่งสัญญาณชีพ: " + ", ".join(sorted(dead))
    bad = failed_orders(30)
    if len(bad) >= 3:
        probs["orders"] = (f"ส่งออเดอร์ไม่ผ่าน {len(bad)} ครั้งใน 30 นาที (" + ", ".join(sorted({b for b, _ in bad}))
                           + f") — ล่าสุด: {bad[-1][1][:90]}")
    hb = age_min(STATE / "manager_heartbeat")
    if hb is None or hb > QUIET_MIN:
        probs["manager"] = f"ผู้จัดการพอร์ตเงียบ {'–' if hb is None else int(hb)} นาที"
    st = os.statvfs("/")
    if st.f_bavail / st.f_blocks < 0.10:
        probs["disk"] = f"ดิสก์เหลือ {st.f_bavail / st.f_blocks:.0%}"
    try:
        mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines()}
        if mem["MemAvailable"] < 200_000:
            probs["mem"] = f"แรมเหลือ {mem['MemAvailable'] // 1024} MB"
    except (OSError, KeyError, ValueError):
        pass
    return probs


def heal(probs: dict[str, str], memo: dict) -> list[str]:
    """ซ่อมเองตามลำดับความปลอดภัย · คืนรายการสิ่งที่ทำ"""
    did, now = [], time.time()
    first = memo.setdefault("since", {})

    def allowed(key: str, after_min: float) -> bool:
        if now - first.get(key, now) < after_min * 60:
            return False
        if now - memo.setdefault("healed", {}).get(key, 0) < COOLDOWN_MIN * 60:
            return False
        memo["healed"][key] = now
        return True

    for key in probs:
        first.setdefault(key, now)
    for key in list(first):
        if key not in probs:
            del first[key]
    for key in probs:
        if key.startswith("down:") and allowed(key, 0):
            svc = CONTAINERS[key[5:]]
            sh("docker", "compose", "--env-file", ".env", "up", "-d", svc, timeout=300)
            did.append(f"เปิด container {svc} ใหม่")
    if ("manager" in probs or "bots" in probs) and "mt5" not in probs and "api" not in probs and allowed("bots-restart", QUIET_MIN):
        sh("docker", "restart", "trader-bots", timeout=120)
        did.append("รีสตาร์ท container บอท")
    if "mt5" in probs and allowed("mt5", MT5_DOWN_MIN):
        sh("docker", "restart", "trader-mt5", timeout=180)
        did.append("รีสตาร์ท MT5 (ล็อกอินเดิมจะกลับมาเอง)")
    return did


def run_check() -> None:
    try:
        memo = json.loads(MEMO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        memo = {}
    probs = check()
    did = heal(probs, memo)
    known = set(memo.get("active", {}))
    seen_before = set(memo.get("seen", []))
    new = {k: v for k, v in probs.items() if k not in known and (k not in CONFIRM_TWICE or k in seen_before)}
    gone = {k: v for k, v in memo.get("active", {}).items() if k not in probs}
    if new or did:
        lines = [f"• {v}" for v in new.values()] + [f"🔧 {x}" for x in did]
        tg("sendMessage", {"text": "🚨 ระบบเทรดมีปัญหา\n" + "\n".join(lines)})
    if gone:
        tg("sendMessage", {"text": "✅ กลับมาปกติ: " + " · ".join(gone.values())})
    memo["active"] = {k: v for k, v in probs.items() if k in known or k in new}   # เฉพาะที่แจ้งไปแล้ว → "กลับมาปกติ" ตรงกับที่แจ้ง
    memo["seen"] = sorted(probs)
    memo["checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    MEMO.parent.mkdir(parents=True, exist_ok=True)
    MEMO.write_text(json.dumps(memo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(memo["checked"], "ok" if not probs else probs)


def run_backup() -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    day = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y%m%d")
    path = BACKUPS / f"state-{day}.tgz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(STATE, arcname="state")
    for old in sorted(BACKUPS.glob("state-*.tgz"))[:-14]:
        old.unlink()
    size = path.stat().st_size
    if size < 45 * 1024 * 1024:
        tg("sendDocument", {"caption": f"💾 สำรองข้อมูลระบบเทรด {day} ({size / 1024:.0f} KB) — ตารางที่เรียนรู้ บันทึก ค่าตั้ง"},
           (path.name, path.read_bytes()))
    print("backup", path, size)


def install() -> None:
    py, me = sys.executable or "/usr/bin/python3", str(Path(__file__).resolve())
    lines = [f"*/5 * * * * {py} {me} >> /var/log/trader-watchdog.log 2>&1 # trader-watchdog",
             f"30 20 * * * {py} {me} --backup >> /var/log/trader-watchdog.log 2>&1 # trader-watchdog"]
    cur = [ln for ln in sh("crontab", "-l").splitlines() if "# trader-watchdog" not in ln and not ln.startswith("ERR")]
    subprocess.run(["crontab", "-"], input="\n".join(cur + lines) + "\n", text=True, check=True)
    print("\n".join(lines))


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    elif "--backup" in sys.argv:
        run_backup()
    else:
        run_check()
