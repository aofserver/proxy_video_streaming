# base เล็ก แล้วลงเฉพาะ Chromium เอง (ไม่เอา Firefox/WebKit ที่ไม่ได้ใช้ -> ลดขนาด ~2.5GB)
FROM python:3.13-slim

# ให้ browser ลงที่ /ms-playwright (world-readable) เพื่อให้ user non-root ใช้ได้
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe สำหรับ mux (fetch_zmdb หาใน PATH เพราะเราไม่ก็อป bin/ ของ macOS เข้ามา)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ติดตั้ง Python deps + Chromium (พร้อม system deps ที่ chromium ต้องใช้) เฉพาะตัวเดียว
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# โค้ด + ไฟล์ข้อมูล (bin/ , downloads/ , .venv/ ถูกกันไว้ใน .dockerignore)
COPY . .

# Chromium headless รันเป็น root ไม่ได้ (sandbox ล้ม) -> สร้าง user non-root แล้วรันด้วย user นั้น
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/downloads \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# ต้อง bind 0.0.0.0 ใน container ไม่งั้น port map จากโฮสต์เข้าไม่ถึง
# (ความปลอดภัย: publish เฉพาะ 127.0.0.1 ของโฮสต์ใน docker-compose.yml — ไม่มี auth)
# ตัว FastAPI: main() รัน uvicorn แบบ 1 worker (state อยู่ในหน่วยความจำ ห้ามหลาย worker)
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8080"]
