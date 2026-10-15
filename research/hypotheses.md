# Pre-registered hypotheses — Playbook Engine (v2)

ลงทะเบียน 2026-10-15 ก่อนรันข้อมูลใดๆ · งบ 8 สมมติฐาน · เกณฑ์นัยสำคัญ (Bonferroni, 8 ตัว) t ≥ 2.7
ช่วงวิจัย: 2023-10-16 → 2026-03-31 (walk-forward 12m optimize / 3m test)
Hold-out: 2026-04-01 → 2026-10-15 — ห้ามแตะจนกว่าจะล็อก

## ด่าน 0 — Regime classifier (เงื่อนไขก่อน H1–H6)
ค่าคงที่กำหนดล่วงหน้า (ไม่จูน): H1 bars, ADX(14) ≥ 25, ความชัน EMA50 ใน 10 แท่ง ≥ 0.5 ATR(14),
ATR(24)-H1 percentile ใน 2,400 แท่ง: ≥ 90% = HIGH_VOL, ≤ 10% = DEAD
ผ่านเมื่อ (non-overlapping 24h samples):
  a) TREND มี efficiency ratio 24h ข้างหน้า สูงกว่า RANGE (t ≥ 2.7)
  b) ผลตอบแทน 24h ตามทิศ TREND ดีกว่า drift เฉลี่ย ทั้งฝั่งขึ้นและฝั่งลง
  c) RANGE: กลับเข้าหาค่าเฉลี่ย (fade) ได้ผลบวก และดีกว่าใน TREND
ถ้า (a)/(b) ไม่ผ่าน → H1, H2 ยกเลิก · ถ้า (c) ไม่ผ่าน → H3, H4 ยกเลิก

## สมมติฐาน
| # | Setup | Regime | ตลาด | สถานะ |
|---|---|---|---|---|
| H1 | Trend pullback (โซน EMA20–50 H1 + แท่งยืนยัน M15) | TREND | XAUUSD | **ยกเลิก** (ด่าน 0 ตก) |
| H2 | Trend pullback | TREND | NAS100 | **ยกเลิก** (ด่าน 0 ตก) |
| H3 | Range fade ที่ key level + rejection wick | RANGE | XAUUSD | **ยกเลิก** (ด่าน 0 ตก) |
| H4 | Range fade | RANGE | NAS100 | **ยกเลิก** (ด่าน 0 ตก) |
| H5 | Volatility expansion breakout หลัง DEAD | DEAD→ทะลุ | XAUUSD | รอ |
| H6 | Volatility expansion breakout | DEAD→ทะลุ | NAS100 | รอ |
| H7 | Liquidity sweep ที่ PDH/PDL/weekly level | ทุก regime ยกเว้น HIGH_VOL | XAUUSD | รอ |
| H8 | Liquidity sweep ที่ key level | ทุก regime ยกเว้น HIGH_VOL | NAS100 | รอ |

## บันทึกผล
(เติมตามลำดับ — ห้ามลบแถวที่ล้ม)

### 2026-10-15 — ด่าน 0: Regime classifier (`python research/regime_test.py`)
ช่วงที่ใช้ได้ 2024-03 → 2026-03 (5 เดือนแรกเป็น warmup ของ percentile 2,400 แท่ง), ~500 ตัวอย่าง 24h ไม่ทับซ้อนต่อตลาด

| เกณฑ์ | XAUUSD | NAS100 |
|---|---|---|
| a) ER ข้างหน้า TREND > RANGE | ตก (0.223 vs 0.227, t=-0.23) | ตก — **กลับทิศ** (0.211 vs 0.270, t=-3.25) |
| b) TREND_UP ชนะ drift | ตก (t=-0.50) | ตก (t=-0.89) |
| b) TREND_DOWN ชนะ -drift | ตก (t=1.05) | ตก (t=0.06) |
| c) RANGE fade > 0 | ตก (t=-0.09) | ตก (t=-0.11) |
| c) fade RANGE > TREND | ตก (t=0.91) | ตก (t=0.55) |
| (ประกอบ) HIGH_VOL → แกว่งมากกว่า DEAD | ผ่าน (2.84% vs 1.23%, t=6.40) | ผ่าน (2.90% vs 1.18%, t=7.63) |

สรุป: TREND/RANGE ของ classifier นี้ **ไม่มีข้อมูลเกี่ยวกับอนาคต** — regime TREND อยู่ต่อครบ 24 ชม. แค่ 29–39%
มีแต่ระดับความผันผวน (vol clustering) ที่คงอยู่จริง → ใช้ได้กับขนาด SL/ไม้ และเป็นเงื่อนไขของ H5/H6
H1–H4 ยกเลิก (ไม่นับเป็นการทดสอบ setup) · ข้อสังเกต NAS ที่กลับทิศ ห้ามนำไปสร้างสมมติฐานใหม่จากข้อมูลชุดนี้
