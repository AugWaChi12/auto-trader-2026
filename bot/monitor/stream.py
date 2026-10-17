"""
stream.py — ไลฟ์หน้า /sandbox ขึ้น TikTok (หรือที่ไหนก็ได้ที่รับ RTMP) จากหน้าเว็บ /stream: วาง Server URL + Stream Key แล้วกด Play

ทำงานยังไง:
  1) เปิดหน้าต่าง Chrome แยก "Bot Village" (/sandbox?stream=1) ขนาดคงที่ แล้วตั้งให้อยู่บนสุดเสมอ (topmost)
  2) ffmpeg จับภาพ "พื้นที่บนจอ" ตรงหน้าต่างนั้น (ตัดแถบชื่อหน้าต่าง) → H.264 + เสียงเงียบ → ส่ง RTMP
     (จับภาพ "หน้าต่าง" ตรงๆ ไม่ได้: Chrome วาดผ่าน GPU → ffmpeg ได้แต่จอดำ — ทดสอบแล้ว 17 ต.ค.)
  ⚠️ เพราะจับ "พื้นที่บนจอ" อะไรที่ลอยทับหน้าต่างนี้ (popup / overlay ที่ topmost ด้วย) จะติดไปในไลฟ์

Stream Key = รหัสผ่าน: เก็บที่ state/stream.json (ไม่ขึ้น git) · API ไม่ส่งค่ากลับ · ตัดออกจาก log ของ ffmpeg ทุกบรรทัด
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .. import config as C

CFG_PATH = C.STATE_DIR / "stream.json"
WINDOW_TITLE = "Bot Village"
PROFILE_DIR = C.STATE_DIR / "stream-window"
URL = "http://127.0.0.1:8765/sandbox?stream=1"
# ขนาดหน้าต่าง (พื้นที่เนื้อหา) และขนาดภาพที่ส่ง
LAYOUTS = {
    # ขนาดหน้าต่าง (ไม่รวมแถบชื่อ +32) คำนวณให้พื้นที่จับภาพ (หักขอบ 16 + แถบชื่อ) ได้ 9:16 / 16:9 พอดี → ไม่ต้องครอปทิ้ง
    "portrait": {"window": (560, 975), "out": {"720p": (720, 1280), "1080p": (1080, 1920)}},
    "landscape": {"window": (1280, 719), "out": {"720p": (1280, 720), "1080p": (1920, 1080)}},
}
BITRATE = {"720p": "3000k", "1080p": "6000k"}
DEFAULTS = {"server": "", "key": "", "orientation": "portrait", "quality": "720p"}


def _ffmpeg() -> str:
    for p in (os.environ.get("FFMPEG", ""), r"C:\ffmpeg\bin\ffmpeg.exe"):
        if p and Path(p).exists():
            return p
    return "ffmpeg"


def _chrome() -> str:
    for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"):
        if p.exists():
            return str(p)
    raise RuntimeError("ไม่พบ Google Chrome")


# ---------------------------------------------------------------- หน้าต่าง (Win32)
def _user32():
    u = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # พิกัดจริงของจอ (ไม่โดน scale)
    except OSError:
        pass
    return u


def find_window() -> int | None:
    from ctypes import wintypes
    u, found = _user32(), []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):  # noqa: ANN001
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if b.value == WINDOW_TITLE:
                found.append(h)
        return True

    u.EnumWindows(cb, 0)
    return found[0] if found else None


def open_window(orientation: str) -> int:
    """เปิดหน้าต่างไลฟ์ (ถ้ายังไม่มี) แล้วตั้งให้อยู่บนสุด · คืน hwnd"""
    h = find_window()
    if h is None:
        w, hh = LAYOUTS[orientation]["window"]
        subprocess.Popen([_chrome(), f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check",
                          "--disable-features=Translate,TranslateUI", f"--window-size={w},{hh + 32}", "--window-position=0,0",
                          f"--app={URL}"])
        for _ in range(40):
            time.sleep(.5)
            h = find_window()
            if h:
                break
        if h is None:
            raise RuntimeError("เปิดหน้าต่าง Bot Village ไม่ได้")
        time.sleep(4)                                   # รอฉาก 3D โหลด
    u = _user32()
    u.ShowWindow(h, 9)                                  # SW_RESTORE (ถ้าย่ออยู่)
    u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)   # HWND_TOPMOST, NOSIZE|NOMOVE|SHOWWINDOW
    return h


def assert_topmost(h: int) -> None:
    u = _user32()
    if u.IsIconic(h):
        u.ShowWindow(h, 9)
    u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)   # TOPMOST, NOSIZE|NOMOVE|NOACTIVATE|SHOW


def unobstructed(h: int, rect: tuple[int, int, int, int]) -> bool:
    """ทุกจุดตรวจในพื้นที่จับภาพต้องเป็นหน้าต่างของเรา (ไม่มีอะไรบัง ไม่ถูกย่อ/ย้าย) — ไม่งั้นภาพจอส่วนตัวจะหลุดขึ้นไลฟ์"""
    from ctypes import wintypes
    u = _user32()
    if not u.IsWindow(h) or u.IsIconic(h) or capture_rect(h) != rect:
        return False
    x, y, w, hh = rect
    for fx, fy in ((.5, .5), (.03, .03), (.97, .03), (.03, .97), (.97, .97)):
        at = u.WindowFromPoint(wintypes.POINT(int(x + w * fx), int(y + hh * fy)))
        if u.GetAncestor(at, 2) != h:                   # GA_ROOT
            return False
    return True


def release_window() -> None:
    h = find_window()
    if h:
        _user32().SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST


def capture_rect(h: int) -> tuple[int, int, int, int]:
    """พื้นที่เนื้อหาของหน้าต่าง (ตัดแถบชื่อของ Chrome app ที่วาดอยู่ในพื้นที่ client) → x, y, w, h (เลขคู่)"""
    from ctypes import wintypes
    u = _user32()
    c, pt = wintypes.RECT(), wintypes.POINT(0, 0)
    u.GetClientRect(h, ctypes.byref(c))
    u.ClientToScreen(h, ctypes.byref(pt))
    try:
        scale = u.GetDpiForWindow(h) / 96
    except (AttributeError, OSError):
        scale = 1.0
    bar = round(32 * scale)                            # แถบชื่อหน้าต่างแบบ app ของ Chrome
    x, y, w, hh = pt.x, pt.y + bar, c.right, c.bottom - bar
    return x, y, w - w % 2, hh - hh % 2


# ---------------------------------------------------------------- ตั้งค่า + สถานะ
def load_cfg() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CFG_PATH.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_cfg(body: dict) -> dict:
    cfg = load_cfg()
    server = str(body.get("server", cfg["server"])).strip()
    if server and not re.match(r"^rtmps?://[^\s]+$", server):
        raise ValueError("Server URL ต้องขึ้นต้นด้วย rtmp:// หรือ rtmps://")
    key = str(body.get("key") or "").strip()            # ว่าง = ใช้ key เดิม
    if key and (len(key) > 512 or re.search(r"\s", key)):
        raise ValueError("Stream Key ไม่ถูกต้อง")
    orientation = body.get("orientation", cfg["orientation"])
    quality = body.get("quality", cfg["quality"])
    if orientation not in LAYOUTS or quality not in BITRATE:
        raise ValueError("ค่าแนวจอ/คุณภาพไม่ถูกต้อง")
    cfg.update(server=server, orientation=orientation, quality=quality, **({"key": key} if key else {}))
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def build_cmd(cfg: dict, rect: tuple[int, int, int, int], output: str) -> list[str]:
    x, y, w, h = rect
    ow, oh = LAYOUTS[cfg["orientation"]]["out"][cfg["quality"]]
    br = BITRATE[cfg["quality"]]
    # ครอปกลางภาพให้ได้สัดส่วนเป๊ะ (9:16 / 16:9) แล้วค่อยย่อ/ขยาย — ไม่มีขอบว่างข้างภาพ
    vf = (f"crop='min(iw,ih*{ow}/{oh})':'min(ih,iw*{oh}/{ow})',scale={ow}:{oh},format=yuv420p")
    return [_ffmpeg(), "-hide_banner", "-loglevel", "warning", "-stats",
            "-thread_queue_size", "512", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0",
            "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-b:v", br, "-maxrate", br,
            "-bufsize", str(int(br[:-1]) * 2) + "k", "-g", "60", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-f", "flv", output]


class Streamer:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.log: deque[str] = deque(maxlen=40)
        self.stats = ""
        self.error = ""
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        cfg = load_cfg()
        if self.proc is not None and self.proc.poll() is not None and not self.error:
            self.error = f"ffmpeg หยุดเอง (code {self.proc.returncode}) — ดู log ด้านล่าง"
        return {"running": self.running(), "since": self.started if self.running() else None, "stats": self.stats,
                "error": self.error, "log": list(self.log)[-12:], "server": cfg["server"], "has_key": bool(cfg["key"]),
                "orientation": cfg["orientation"], "quality": cfg["quality"], "window": find_window() is not None}

    def start(self) -> dict:
        with self._lock:
            if self.running():
                return self.status()
            cfg = load_cfg()
            if not cfg["server"] or not cfg["key"]:
                raise ValueError("ใส่ Server URL และ Stream Key แล้วกดบันทึกก่อน")
            h = open_window(cfg["orientation"])
            for _ in range(3):                          # ต้องผ่านการตรวจ "ไม่มีอะไรบัง" ติดกัน 3 ครั้งก่อนเริ่มส่ง
                assert_topmost(h)
                time.sleep(.5)
                rect = capture_rect(h)
                if not unobstructed(h, rect):
                    raise RuntimeError("หน้าต่าง Bot Village ถูกบังหรือยังเปิดไม่เสร็จ — ไม่เริ่มไลฟ์ (กันภาพจออื่นหลุด) ลองกด Play อีกครั้ง")
            url = cfg["server"].rstrip("/") + "/" + cfg["key"]
            self.log.clear()
            self.stats, self.error = "", ""
            self.proc = subprocess.Popen(build_cmd(cfg, rect, url), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.started = time.time()
            threading.Thread(target=self._read, args=(self.proc, cfg["key"]), daemon=True).start()
            threading.Thread(target=self._guard, args=(self.proc, h, rect), daemon=True).start()
            return self.status()

    def _guard(self, proc: subprocess.Popen, h: int, rect: tuple[int, int, int, int]) -> None:
        """ระหว่างไลฟ์: ตรวจทุก 0.1 วิ ว่าไม่มีอะไรบัง (ตรวจก่อน แล้วค่อยตั้ง topmost ซ้ำ — ถ้าคืนหน้าต่างก่อนจะไม่เห็นช่วงที่หลุด)
        เจอเมื่อไร ตัดไลฟ์ทันที"""
        while proc.poll() is None:
            try:
                ok = unobstructed(h, rect)
                if ok:
                    assert_topmost(h)
            except OSError:
                ok = False
            if not ok:
                proc.kill()
                self.error = ("หยุดไลฟ์อัตโนมัติ: มีหน้าต่างอื่นบัง / หน้าต่าง Bot Village ถูกย่อ ย้าย หรือปิด "
                              "— ตัดทันทีเพื่อไม่ให้ภาพจออื่นหลุดขึ้นไลฟ์ · กด Play ใหม่ได้")
                release_window()
                return
            time.sleep(.1)

    def stop(self) -> dict:
        with self._lock:
            if self.running():
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
            self.error = ""
            release_window()
            return self.status()

    def _read(self, proc: subprocess.Popen, key: str) -> None:
        buf = b""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in b"\r\n":
                line = buf.decode("utf-8", "replace").replace(key, "••••") if key else buf.decode("utf-8", "replace")
                buf = b""
                if not line.strip():
                    continue
                if line.startswith("frame="):
                    self.stats = re.sub(r"\s+", " ", line)    # frame= fps= bitrate= time= speed=
                else:
                    self.log.append(line)
            else:
                buf += ch


STREAMER = Streamer() if sys.platform == "win32" else None
