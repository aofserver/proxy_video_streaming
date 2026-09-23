# proxy_vdo — โปรแกรมเล่น/ดาวน์โหลดหนัง 77-hd (zmdb) ในเครื่องตัวเอง

Local **proxy + web player** สำหรับดูและดาวน์โหลดวิดีโอ HLS จาก 77-hd.com / zmdb.net
โดยไม่ต้องพึ่งส่วนขยายเบราว์เซอร์ และไม่ติดโฆษณา/anti-debug ของเว็บต้นทาง

ทำไมต้องมี proxy: สตรีมของ zmdb ใช้ **HLS Content Steering** (playlist กับ segment คนละ host)
และบังคับ header `Origin`/`Referer`/desktop-UA ไม่งั้น 403 — ซึ่ง `hls.js` ในเบราว์เซอร์
เซ็ต header พวกนี้เองไม่ได้ และ `yt-dlp` ตรง ๆ ก็โหลดไม่ได้ (ติด 403)
โปรแกรมนี้จึงรัน proxy ฝั่ง Python มาฉีด header + rewrite URL ใน m3u8 + สลับ host ของ segment
ไป CDN mirror อัตโนมัติ

---

## ความสามารถ

- **หน้า browse** — รายการหนัง 3667 เรื่อง คลิกเพื่อเล่นได้ทันที
- **หน้า player** — เล่น HLS ในเบราว์เซอร์ผ่าน proxy, เลือกเสียง/ซับ/ความชัดได้, กด **F5 แล้ว re-resolve master ใหม่** (กัน token หมดอายุ)
- **ดาวน์โหลดหลายเรื่องพร้อมกัน** — เปิดคนละแท็บ กด download แยกเรื่องได้ (สูงสุด 4 เรื่องพร้อมกัน) แต่ละเรื่องเก็บในโฟลเดอร์ย่อยของตัวเอง `downloads/<ชื่อเรื่อง>/`
- **ต่อ token สดอัตโนมัติ** — segment token หมดอายุ ~15 นาที ถ้าเจอ 403 proxy จะขอ URL ที่ sig สดผ่าน master ให้เอง
- **API docs** — เปิด `/docs` (FastAPI/OpenAPI)

---

## โครงสร้างไฟล์

| ไฟล์ | หน้าที่ |
|------|---------|
| `main.py` | **เซิร์ฟเวอร์หลัก (FastAPI/uvicorn)** — proxy + หน้า browse/player + ดาวน์โหลด |
| `tools_77hd.py` | core logic ทั้งหมด (proxy/resolve/download) + เซิร์ฟเวอร์ stdlib สำรอง (`main.py` import ไปใช้) |
| `fetch_zmdb.py` | ตัวดาวน์โหลดหลัก — จัดการ content steering, token สด, mux เสียง/ซับ |
| `download_video_77hd.py` | หา master URL ด้วย Playwright (`grab_master`) + CLI wrapper |
| `list_77hd.py` | crawler ไล่เก็บรายชื่อหนังจาก 77-hd.com → `77hd_movies.json` |
| `list_24hd.py` | crawler ไล่เก็บหนังจาก 24hd.media → `24hd_movies.json` (เก็บ **Master URL** vdohls ให้เลย ไม่มี token) |
| `player.html`, `browse.html` | หน้าเว็บ (player / รายการหนัง) |
| `77hd_movies.json` | รายการหนัง (สร้างด้วย `list_77hd.py` หรือปุ่มอัปเดตในหน้า browse) |
| `bin/ffmpeg`, `bin/ffprobe` | ffmpeg static (arm64) สำหรับรันในเครื่อง — สคริปต์เรียกจากที่นี่ก่อน PATH |
| `Dockerfile`, `docker-compose.yml` | รันแบบ container (base slim + Chromium อย่างเดียว) |
| `requirements.txt` | dependencies: `fastapi`, `uvicorn[standard]`, `playwright` |

---

## วิธีรัน

### แบบ Docker (แนะนำ — ไม่ต้องติดตั้งอะไรในเครื่อง)

```sh
docker compose up -d --build      # build + รัน background
# เปิด http://127.0.0.1:8080   (API docs: /docs)
docker compose logs -f
docker compose down
```

> ต้องมี Docker Desktop เปิดอยู่ และ **port 8080 บนโฮสต์ต้องว่าง**
> (ถ้าไม่ว่าง แก้ค่าที่ publish ใน `docker-compose.yml` เช่น `"127.0.0.1:8081:8080"`)
> ไฟล์ที่โหลดจะออกมาที่โฟลเดอร์ `downloads/` บนเครื่องคุณ (mount volume)

### แบบรันในเครื่องโดยตรง

ต้องมี: Python 3, ffmpeg/ffprobe (มีใน `bin/` หรือใน PATH), Chromium ของ Playwright

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/playwright install chromium

# โหมด browse (รายการหนังทั้งหมด)
.venv/bin/python main.py --port 8080 --open

# หรือเปิดหนังเรื่องเดียวเลย (จาก URL หน้าเว็บ หรือ master URL ตรง ๆ)
.venv/bin/python main.py "https://77-hd.com/zootopia-2-2025/" --open
```

ถ้ายังไม่มี `77hd_movies.json` ให้สร้างก่อน (ใช้เวลาสักพัก crawl ทุกหน้า):

```sh
.venv/bin/python list_77hd.py
```

#### รันด้วย uvicorn ตรง ๆ ก็ได้ (ต้อง 1 worker เท่านั้น)

```sh
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
```

> ⚠️ **ห้ามรันหลาย worker** — state (คิวดาวน์โหลด/master ของแต่ละแท็บ) เก็บในหน่วยความจำของ process เดียว
> ถ้าหลาย worker แต่ละตัวจะเห็น state คนละชุด ทำให้ track ดาวน์โหลด/เล่นเพี้ยน

---

## ตัวเลือกตอนรันเซิร์ฟเวอร์ (`main.py`)

| Flag | ความหมาย |
|------|----------|
| `source` (ไม่บังคับ) | URL หน้าหนัง 77-hd หรือ master URL — ไม่ใส่ = โหมด browse |
| `--port N` | พอร์ต (ค่าเริ่มต้น 8080) |
| `--host H` | ค่าเริ่มต้น `127.0.0.1` (ใน Docker ใช้ `0.0.0.0`) |
| `--open` | เปิดเบราว์เซอร์ให้อัตโนมัติ |
| `--headful` | [กรณี URL 77-hd] เปิด Chrome แบบเห็นหน้าต่างตอนหา master |
| `--timeout N` | [กรณี URL 77-hd] วินาทีรอดัก master (ค่าเริ่มต้น 60) |

---

## HTTP endpoints (สำหรับดู/ดีบั๊ก)

| Endpoint | หน้าที่ |
|----------|---------|
| `GET /` | หน้า browse (หรือ player ถ้าเปิดแบบเรื่องเดียว) |
| `GET /player` | หน้า player |
| `GET /movies.json` | รายการหนัง (JSON) |
| `GET /resolve?url=` | หา master + CDN mirror ของเรื่องนั้น (ใช้ Playwright ถ้าเป็น URL หน้าเว็บ) |
| `GET /master.m3u8?m=` | master playlist ที่ rewrite แล้ว (pin master ต่อแท็บด้วย `?m=`) |
| `GET /p?u=&seg=` | proxy ดึง segment/playlist (สลับ host ไป CDN mirror ให้) |
| `GET /download?source=&master=&fmt=&height=&title=` | เริ่มดาวน์โหลดเรื่องหนึ่ง |
| `GET /download-status?source=` \| `?all=1` | สถานะดาวน์โหลด |
| `GET /file?name=<เรื่อง>/<ไฟล์>` | ดาวน์โหลดไฟล์ที่โหลดเสร็จ (รองรับ HTTP Range/seek) |
| `GET /update`, `/update-status` | สั่ง/ดูสถานะการอัปเดตรายการหนัง |
| `GET /docs` | Swagger UI |

---

## ดาวน์โหลดผ่าน CLI ตรง ๆ (`fetch_zmdb.py`)

ถ้ามี master URL อยู่แล้ว (เช่นดักจาก Video DownloadHelper — ช่อง "Master URL" หน้าตา
`https://g.zmdb.net/hls/<id>/t.<hash>/_master?gw_enc=..`) โหลดตรงได้เลย:

```sh
# เสียง+ซับหลายภาษา (mkv) — เลือกเปิดปิดใน player
.venv/bin/python fetch_zmdb.py "MASTER_URL" --format mkv -o "หนัง.mkv"

# เสียง+ซับ th/en, เล่นเสียงไทยอัตโนมัติ + ปิดซับ
.venv/bin/python fetch_zmdb.py "MASTER_URL" --format mkv \
  --audio-langs th,en --sub-langs th,en --default-audio th --subs-off -o "หนัง.mkv"

# เสียงเดียว เป็น mp4
.venv/bin/python fetch_zmdb.py "MASTER_URL" --format mp4 --audio-lang th -o "หนัง.mp4"
```

| Flag | โหมด | ความหมาย |
|------|------|----------|
| `--format mp4\|mkv` | ทั้งคู่ | `mp4`=แทร็กเดียว, `mkv`=หลายเสียง+ซับ (ค่าเริ่มต้น `mp4`) |
| `--height N` | ทั้งคู่ | ความสูงสูงสุด เช่น `1080`, `720` (ค่าเริ่มต้น 1080) |
| `--audio-lang th` | mp4 | เลือกภาษาเสียง 1 ภาษา |
| `--audio-langs th,en` | mkv | เลือกเฉพาะภาษาเสียง (ไม่ระบุ=ทุกภาษา) |
| `--sub-langs th,en` | mkv | เลือกเฉพาะภาษาซับ (`none`=ไม่เอาซับ) |
| `--default-audio th` | mkv | ภาษาเสียงที่เล่นอัตโนมัติ |
| `--subs-off` | mkv | เปิดมาไม่โชว์ซับ (ยังเลือกได้ใน player) |
| `-o NAME` | ทั้งคู่ | ชื่อไฟล์ปลายทาง |
| `--out-dir DIR` | ทั้งคู่ | โฟลเดอร์ปลายทาง (ค่าเริ่มต้น `downloads/`) |
| `--workers N` | ทั้งคู่ | จำนวน thread โหลด segment (ค่าเริ่มต้น 4 — สูงไปเสี่ยง 429) |

---

## หลักการทำงาน (สรุป)

1. วิดีโอเป็น HLS — `blob:` ดึงตรงไม่ได้ ต้องหา **Master URL** (proxy หาให้ด้วย Playwright)
2. Master ใช้ **Content Steering** — playlist เสิร์ฟจาก `g.zmdb.net` แต่ segment 403 ที่ host นั้น
   ต้องโหลด segment จาก CDN mirror ตาม `playback-routing.json`
3. **Token** (`sig`/`exp`) หมดอายุ ~15 นาที — ทั้ง proxy และตัวโหลด re-fetch master เพื่อขอ token สด
4. โหลด segment แบบ streaming เขียนลงดิสก์ทีละชิ้น แล้ว mux ด้วย ffmpeg → mp4/mkv (ซับ WebVTT → SRT)

Header ที่จำเป็นทุก request: `Origin: https://zmdb.net`, `Referer: https://zmdb.net/`, desktop Chrome UA

---

## เตรียม ffmpeg (กรณีรันในเครื่อง ไม่ผ่าน Docker)

ต้องมี `bin/ffmpeg`, `bin/ffprobe` (arm64 static) หรือมี `ffmpeg` ใน PATH

```sh
mkdir -p bin && cd bin
curl -L -o ffmpeg.zip  https://www.osxexperts.net/ffmpeg71arm.zip  && unzip -o ffmpeg.zip  && rm ffmpeg.zip
curl -L -o ffprobe.zip https://www.osxexperts.net/ffprobe71arm.zip && unzip -o ffprobe.zip && rm ffprobe.zip
chmod +x ffmpeg ffprobe && xattr -d com.apple.quarantine ffmpeg ffprobe 2>/dev/null
cd ..
```

> ใน Docker ไม่ต้องทำขั้นนี้ — image ลง `ffmpeg` ผ่าน apt ให้แล้ว (และ `bin/` ของ macOS ถูกกันไม่ให้เข้า image)

---

## ข้อควรรู้ / ข้อจำกัด

- **bind localhost เท่านั้น ไม่มี auth** — อย่า expose ออกเน็ต
- หนังเต็มเรื่อง (~90–110 นาที) โหลด **~15–18 นาที** ต่อไฟล์ ควรปล่อยรัน background
- ต้องมีพื้นที่ดิสก์ว่าง ~2 เท่าของไฟล์สุดท้าย (มี tmp ระหว่างทาง)
- บางเรื่องหน้าเว็บมีแค่ตัวอย่าง/ยังไม่ปล่อยจริง — resolve จะแจ้ง "ไม่พบวิดีโอ" (ไม่ใช่บั๊ก)
- ถ้า segment เริ่ม 403 ทั้งที่ token สด อาจเป็นเพราะ CDN mirror เปลี่ยน — เช็ค `playback-routing.json` ใหม่
- **ใช้ส่วนตัว/ศึกษาเท่านั้น** เคารพลิขสิทธิ์เนื้อหาต้นทาง
