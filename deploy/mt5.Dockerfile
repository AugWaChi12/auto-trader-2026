# MT5 ใน Wine: image ต้นทาง + เปลี่ยน Wine เป็นรุ่น staging
# เหตุผล: MT5 รุ่นปัจจุบันขึ้น "A debugger has been found running in your system" แล้วไม่ยอมเปิด บน winehq-stable 10.0
#         (เจอจริง 18 ต.ค. 2026) — ทางแก้ที่มีคนใช้ได้ผล: winehq-staging
#         https://github.com/gmag11/MetaTrader5-Docker-Image/issues/29 (commit f097686)
FROM gmag11/metatrader5_vnc:latest
USER root
# คีย์คลัง WineHQ ใน image ต้นทางเก่าแล้ว (NO_PUBKEY 76F1A20FF987672F) → โหลดคีย์ปัจจุบันทับที่เดิม (Signed-By ชี้ที่นี่)
RUN curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor --yes -o /etc/apt/keyrings/winehq-archive.key \
 && apt-get update \
 && (apt-get remove -y winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 || true) \
 && apt-get install -y --install-recommends winehq-staging \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && wine --version
