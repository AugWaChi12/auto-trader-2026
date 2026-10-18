#!/usr/bin/env bash
# Deploy auto trader ขึ้นเซิร์ฟเวอร์ Linux (รันจากเครื่อง dev ด้วย Git Bash): ส่งโค้ดที่ commit แล้ว → build → รัน
#
#   deploy/deploy.sh                              # เครื่องเริ่มต้น
#   DEPLOY_HOST=root@1.2.3.4 deploy/deploy.sh
#
# ครั้งแรก: ส่ง state/bot_settings.json + สุ่มรหัส VNC ให้ (ไม่ทับของเดิมในรอบถัดไป) · ไม่แตะ state/ logs/ บนเซิร์ฟเวอร์
set -euo pipefail

HOST=${DEPLOY_HOST:-root@95.216.145.123}
REMOTE=/opt/auto-trader
cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain -- bot config deploy research/techniques.json)" ]]; then
  echo "มีไฟล์ที่ยังไม่ commit ใน bot/ config/ deploy/ — commit (และรัน test) ก่อน" >&2
  exit 1
fi

echo ">> ส่งโค้ด $(git rev-parse --short HEAD) → $HOST:$REMOTE"
ssh "$HOST" "mkdir -p $REMOTE/state $REMOTE/logs"
git archive HEAD bot config deploy research/techniques.json | ssh "$HOST" "tar -x -C $REMOTE"
ssh "$HOST" "chmod +x $REMOTE/deploy/*.sh"

echo ">> ครั้งแรก: ค่าตั้งบอท + รหัสหน้าจอ MT5"
if ! ssh "$HOST" "test -f $REMOTE/state/bot_settings.json" && [[ -f state/bot_settings.json ]]; then
  scp -q state/bot_settings.json "$HOST:$REMOTE/state/bot_settings.json"
fi
# เช็กว่ามีรหัสจริง (ไม่ใช่แค่มีไฟล์) — ครั้งแรกเคยได้ .env ที่มีแต่ RPYC_VERSION แล้วหน้าจอ MT5 ขึ้น 401
ssh "$HOST" 'cd '"$REMOTE"'/deploy && umask 077 && touch .env && if ! grep -q "^PASSWORD=" .env; then printf "CUSTOM_USER=trader\nPASSWORD=%s\n" "$(openssl rand -base64 18 | tr -d "/+=")" >> .env; echo "   สร้างรหัส VNC ใน deploy/.env แล้ว"; fi'

echo ">> เปิด MT5 ก่อน แล้วอ่านเวอร์ชัน rpyc ของมัน (บอทต้องใช้เวอร์ชันเดียวกัน)"
ssh "$HOST" "cd $REMOTE/deploy && docker compose up -d mt5"
RPYC=$(ssh "$HOST" "for i in \$(seq 1 60); do v=\$(docker exec trader-mt5 sh -c 'find / -path /proc -prune -o -type d -name \"rpyc-*.dist-info\" -print 2>/dev/null | head -1' | sed -E 's/.*rpyc-([0-9.]+)\\.dist-info/\\1/'); [ -n \"\$v\" ] && { echo \$v; exit 0; }; sleep 10; done; echo 5.3.1")
echo "   rpyc = $RPYC"
ssh "$HOST" "cd $REMOTE/deploy && grep -q '^RPYC_VERSION=' .env && sed -i 's/^RPYC_VERSION=.*/RPYC_VERSION=$RPYC/' .env || echo 'RPYC_VERSION=$RPYC' >> .env"

echo ">> build + รันบอทและ monitor"
ssh "$HOST" "cd $REMOTE/deploy && docker compose --env-file .env up -d --build && docker image prune -f >/dev/null && docker compose ps"
echo
echo "ต่อไป (ครั้งแรกเท่านั้น): ล็อกอินบัญชี demo ใน MT5 บนเซิร์ฟเวอร์"
echo "  1) ssh -L 3000:127.0.0.1:3000 -L 8765:127.0.0.1:8765 $HOST"
echo "  2) เปิด http://localhost:3000  (user/รหัส: ssh $HOST cat $REMOTE/deploy/.env)"
echo "  3) ใน MT5: File → Login to Trade Account → FxPro-MT5 Demo → ใส่เลขบัญชี+รหัสเอง · กดปุ่ม Algo Trading ให้เป็นสีเขียว"
echo "  4) monitor: http://localhost:8765"
