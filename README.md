# Gold Bot — XAUUSD + NAS100 บน MT5

บอทเทรดอัตโนมัติแบบ session-based ที่ยกโครงสร้างมาจากโปรเจค `leaning-pubg`
(interface injection, Mock→Sandbox→Real, backoff table, กันส่งซ้ำ, stale-data guard)

> **สถานะ: เฟส 2 — รันบน DEMO** (บอทปฏิเสธบัญชีที่ไม่ใช่ demo โดยโค้ด) · ผลวิจัย: [research/RESULTS.md](research/RESULTS.md)

## โครงสร้าง

```
bot/
  core/        types, interfaces (IMarketData / IOrderExecutor / IAccount), เวลา, indicator
  strategy/    session_breakout (Asian Range / Opening Range), sweep_reversal
  risk/        risk_manager (sizing, เพดานรายวัน, kill switch), news (ช่วงงดเทรด)
  execution/   order_guard (กันซ้ำ + backoff), position_manager (BE / trailing)
  backtest/    sim_broker (โบรกจำลอง), runner, report
  data/        mt5_source (ดึงข้อมูล), store (CSV), synthetic (ข้อมูลทดสอบ)
  engine.py    ประกอบทุกชิ้น — ใช้ร่วมกันทั้ง backtest และ live
  config.py    ค่าตั้งค่าทั้งหมด
config/news.csv  วัน FOMC (+ เพิ่ม CPI เองได้)
tests/           pytest
```

| PUBG | Gold Bot |
|---|---|
| `IItemScanner` / `ILootExecutor` / `IPlayerState` | `IMarketData` / `IOrderExecutor` / `IAccount` |
| `MockWorld` → `SandboxWorld` | `SimBroker` → MT5 Demo → MT5 Live |
| `queuedIds_` / IN_FLIGHT | consumed signal key (สัญญาณเดียวส่งได้ครั้งเดียว) |
| `BackoffTable` (InventoryFull / Denied) | `OrderGuard` (NO_MONEY / PRICE_CHANGED / INVALID_STOPS) |
| Stale-frame guard | Stale-data guard |
| `maxAmmo`, `PickupCooldown` | `RiskManager` |

## เริ่มใช้งาน

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pytest

# 1) ทดสอบระบบด้วยข้อมูลสังเคราะห์ (ผลไม่มีความหมายทางการเทรด)
.\.venv\Scripts\python -m bot synth
.\.venv\Scripts\python -m bot backtest --data data/synthetic

# 2) ข้อมูลจริง: ติดตั้ง MT5 ของโบรก + ล็อกอิน demo แล้ว
#    Tools → Options → Charts → Max bars in chart = Unlimited (แล้วรีสตาร์ท MT5)
$env:MT5_PATH = "C:\Program Files\<โบรก> MetaTrader 5\terminal64.exe"   # ถ้าจำเป็น
.\.venv\Scripts\python -m bot download --years 3
.\.venv\Scripts\python -m bot backtest --data data/mt5 --no-kill
.\.venv\Scripts\python -m bot backtest --data data/mt5 --strategies xau_bo --profile aggressive
.\.venv\Scripts\python -m bot backtest --data data/mt5 --start 2023-01-01 --end 2024-12-31
```

ผลถูกบันทึกที่ `reports/<เวลา>/` — summary.json, trades.csv, signals.csv, equity.csv, equity.png

## รันบน demo (เฟส 2)

```powershell
# MT5 ต้องเปิดอยู่ ล็อกอินบัญชี DEMO และกดปุ่ม "Algo Trading" ให้เป็นสีเขียว
.\.venv\Scripts\python -m bot smoke      # ทดสอบ เปิด → เลื่อน SL → ปิด 0.01 lot
.\start_bot.ps1                           # เปิดบอทหลัก (xau_momo) ในหน้าต่างแยก
.\start_bot.ps1 -Instance all             # เปิดทุกบอท: main + lab_m5 + lab_m1 (แล็บสแกลป์ — ตก backtest, รันเพื่อดูผลจริง)
.\.venv\Scripts\python -m bot status     # บอทยังทำงานไหม / ไม้ที่เปิด / P&L
.\monitor.ps1                             # หน้าเว็บ monitor http://127.0.0.1:8765 (อ่านอย่างเดียว)
.\monitor.ps1 -Charts                     # กราฟหลายจอ: ทอง/NAS × M1/M5/M15/H1 + เส้นจุดเข้า/SL/TP
.\.venv\Scripts\python -m bot radar-stats # คำนวณสถิติความน่าจะเป็นใหม่ (หน้า /radar)
```

บอทบน demo (bot/config.py `LIVE_INSTANCES`) — แต่ละตัวมีทุนเสมือน $1,000 และเพดานขาดทุนของตัวเอง:
`main` (xau_momo M15) · `lab_m5` / `lab_m1` (แล็บสแกลป์) · `gamble` (ทุกกลยุทธ์ M15 ที่ตกวิจัย เข้าทุกเงื่อนไข ไม้ละ 0.5%)

**Signal Radar** (`/radar`): ทุกกลยุทธ์ที่บอทเฝ้า — เกจใกล้เข้าเงื่อนไข, โอกาสชนะ/ค่าคาดหวัง/การกระจายผลจาก backtest 12 เดือน
พร้อม CI95 (คร่อม 0 = แยกจากโชคไม่ได้) และผลจริงบน demo

หน้า monitor: สถานะบอท/MT5, equity, กำไรวันนี้/รวม, สัญญาณวันนี้ (ทองวิ่งไปเท่าไรเทียบเกณฑ์),
ไม้ที่เปิด, ประวัติเข้า-ออก-กำไร, กราฟ equity, log ล่าสุด — stdlib ล้วน ไม่ใช้เน็ต ผูก 127.0.0.1 เท่านั้น

- กลยุทธ์ที่รัน: `LIVE_STRATEGIES` ใน config.py (ตอนนี้ `xau_momo` — ยังไม่ผ่านเกณฑ์ ทดสอบไปข้างหน้าบน demo)
- profile `demo_1k`: ยอมเปิด 0.01 lot แม้เสี่ยงเกิน 1% (สูงสุด 6%) เพื่อให้ทุน $1,000 เก็บผลเป็น R ได้ — ห้ามใช้กับเงินจริง
- สถานะที่ต้องรอดการรีสตาร์ท: `state/` (meta ของไม้, kill switch) + comment ของออเดอร์ (signal key)
- log: `logs/live_YYYYMMDD.log` · แจ้งเตือน Telegram: ตั้ง env `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `xau_momo` เข้าได้วันละครั้งตอน 12:00 UTC (19:00 ไทย) ปิดหมด 20:00 UTC (03:00 ไทย)

## กลยุทธ์ (ค่าเริ่มต้นเป็นสมมติฐาน ยังไม่ผ่านการพิสูจน์)

| tag | ตลาด | กรอบ | เข้า | ออก |
|---|---|---|---|---|
| `xau_bo` | XAUUSD | 00:00–07:00 UTC | ปิดทะลุกรอบ 07:00–16:00 | ครึ่งหนึ่งที่ 1.5R, ที่เหลือ BE + ATR trail, ปิดหมด 20:00 |
| `xau_sweep` | XAUUSD | 00:00–07:00 UTC | ไส้เจาะขอบแล้วปิดกลับเข้ากรอบ | TP อีกฝั่งกรอบ (ต้อง ≥ 2R) |
| `nas_orb` | NAS100 | 09:30–10:00 NY | ปิดทะลุกรอบก่อนเที่ยง | เหมือน xau_bo, ปิดหมด 15:45 NY |
| `nas_sweep` | NAS100 | 09:30–10:00 NY | sweep แล้วปิดกลับ | เหมือน xau_sweep |

Risk profile: `normal` 1%/ไม้, `aggressive` 2%/ไม้, `prop` 0.75%/ไม้ (สำหรับสอบ prop firm)

## สมมติฐานของ backtest (ตั้งใจให้แย่กว่าความจริง)

- แท่งเดียวโดนทั้ง SL และ TP → นับว่าโดน SL
- gap ข้าม SL → ออกที่ราคาเปิด
- สเปรด = max(สเปรดของแท่ง, `min_spread` ใน config) + slippage ทุกครั้งที่เข้า/ออกแบบ market
- ไม่คิด swap (ปิดไม้ภายในวัน)
- ตรวจ look-ahead bias แล้ว: บน random walk (`synth --drift 0`) ทุกกลยุทธ์ได้ ≈ 0R ลบต้นทุน

## ก่อนเชื่อผล backtest

1. แต่ละกลยุทธ์ต้องมี ≥ 100 เทรด และ expectancy > 0 **หลายปี** ไม่ใช่ปีเดียว
2. แบ่งข้อมูล: จูนบนปีแรกๆ (`--end`) แล้วทดสอบปีหลัง (`--start`) โดยไม่จูนเพิ่ม
3. ถ้าต้องจูนละเอียดมากถึงจะกำไร = overfit
4. Demo อย่างน้อย 1–2 เดือนก่อน live

## Roadmap

- [x] เฟส 1: backtest + risk + guard + รายงาน
- [x] เฟส 2: `Mt5Broker` + live loop บน demo, กู้สถานะหลังรีสตาร์ท, smoke test, status, Telegram
- [ ] เทียบผล demo กับ backtest ช่วงเดียวกัน (slippage / สเปรดจริง)
- [ ] เฟส 3: live lot เล็ก + แจ้งเตือน (LINE/Telegram) + dashboard
