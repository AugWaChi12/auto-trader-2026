# บอท + หน้า monitor (Linux) — คุยกับ MT5 ใน container "mt5" ผ่าน RPyC (bot/mt5api.py)
FROM python:3.11-slim

# rpyc ต้องเป็นเวอร์ชันเดียวกับฝั่ง MT5 (image gmag11/metatrader5_vnc) — deploy.sh ตรวจแล้วส่งค่ามาให้
ARG RPYC_VERSION=5.3.1
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC

WORKDIR /app
RUN pip install --no-cache-dir "numpy>=2.0" "pandas>=2.2" tzdata "rpyc==${RPYC_VERSION}" anthropic google-genai
COPY bot ./bot
COPY config ./config
COPY research/techniques.json ./research/techniques.json
COPY research/m5_lab.py ./research/m5_lab.py

# state/ และ logs/ ผูกกับโฟลเดอร์บนเครื่อง (docker-compose.yml) — ค่าตั้ง/สถานะบอทอยู่รอดตอน deploy ใหม่
CMD ["python", "-m", "bot", "supervise"]
