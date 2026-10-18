#!/usr/bin/env bash
# ใส่/เปลี่ยน API key ของผู้จัดการพอร์ตใน deploy/.env แล้วรีสตาร์ทเฉพาะ container บอท (MT5 ไม่ถูกแตะ)
#   จากเครื่องตัวเอง:  ssh -t root@<server> /opt/auto-trader/deploy/set-key.sh            (Gemini)
#                      ssh -t root@<server> /opt/auto-trader/deploy/set-key.sh ANTHROPIC_API_KEY
# คีย์พิมพ์/วางที่เครื่องผู้ใช้เท่านั้น — ไม่ผ่านแชท ไม่ถูกพิมพ์ออกจอหลังบันทึก
set -euo pipefail
NAME="${1:-GEMINI_API_KEY}"
case "$NAME" in GEMINI_API_KEY|ANTHROPIC_API_KEY) ;; *) echo "ชื่อคีย์ต้องเป็น GEMINI_API_KEY หรือ ANTHROPIC_API_KEY"; exit 1 ;; esac
cd "$(dirname "$0")"
read -r -p "วางคีย์ $NAME แล้วกด Enter: " K
K="$(printf '%s' "$K" | tr -d '[:space:]')"
if [ -z "$K" ]; then echo "ยังไม่ได้วางคีย์ — ไม่ได้บันทึกอะไร"; exit 1; fi
touch .env && chmod 600 .env
sed -i "/^$NAME=/d" .env
printf '%s=%s\n' "$NAME" "$K" >> .env
echo "บันทึก $NAME แล้ว (ยาว ${#K} ตัวอักษร) — กำลังรีสตาร์ทบอท…"
docker compose --env-file .env up -d --no-deps bots
echo "เสร็จแล้ว"
