# API Specification — 77-hd.com / zmdb HLS Playback Chain

> **สถานะเอกสาร:** Reverse-engineered จากการสังเกต network traffic จริง (2026-09-18)
> **ขอบเขต:** เอกสารนี้อธิบายเฉพาะ API ที่ observe ได้จริงจากการเล่นวิดีโอบนหน้าเว็บ
> `77-hd.com` เท่านั้น ใช้ประกอบการทำงานของ `download_video_77hd.py` / `fetch_zmdb.py`
> ฟิลด์ที่ยังไม่ยืนยันจะทำเครื่องหมาย `# inferred`
>
> ⚠️ นี่เป็น API ของบุคคลที่สาม (ไม่มีเอกสารทางการ) โครงสร้างอาจเปลี่ยนได้ทุกเมื่อ


---

## 1. ภาพรวมสถาปัตยกรรม (Playback Chain)

การเล่น 1 เรื่องไล่ผ่าน 4 โดเมน แต่ละชั้นมีหน้าที่และกลไกป้องกันต่างกัน:

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 1. 77-hd.com/<slug>/            (HTML เพจ — ฝัง iframe)                       │
│      └─ <iframe src="https://zmdb.net/embed?id=<embedId>&type=<movie|tv>">   │
├───────────────────────────────────────────────────────────────────────────┤
│ 2. zmdb.net  (JSON API + player shell)                                       │
│      GET /api/embed/bootstrap   → linkToken (JWT) + metadata เรื่อง          │
│      GET /api/embed/links       → playerEmbedLinks[] (embedUrl ของ streamXXX)│
│         (ต้องแนบ Authorization: Bearer <linkToken>)                          │
├───────────────────────────────────────────────────────────────────────────┤
│ 3. streamXXX.com/play/<videoId> (player เนสต์ — Cloudflare + anti-embed)      │
│      โหลด hls.js แล้วดึง master playlist จาก g.zmdb.net                       │
│      • เปิดตรง / นอก iframe ที่อนุญาต → HTTP 403 "unauthorized sources"       │
├───────────────────────────────────────────────────────────────────────────┤
│ 4. g.zmdb.net + CDN mirror  (HLS content steering)                           │
│      GET /hls/<videoId>/t.<hash>/_master?gw_enc=<o>  → master m3u8            │
│      GET /hls/playback-routing.json?gw_enc=<o>       → CDN steering manifest  │
│      GET /hls/<videoId>/_<track>/_index?sig&exp&gw_enc → media playlist       │
│      segment .bin เสิร์ฟจาก mirror เช่น lb.osx-cdn2.space (ไม่ใช่ g.zmdb.net) │
└───────────────────────────────────────────────────────────────────────────┘
```

**หลักการสำคัญ (anti-bot):** ต้องโหลด `77-hd.com` เป็น top-level document เพื่อให้ลำดับ
iframe ancestor ถูกต้อง (77-hd → zmdb → streamXXX) มิฉะนั้น:
- `zmdb.net/embed` ที่เปิดเป็น top-level → ตรวจว่าไม่ได้อยู่ใน iframe ที่อนุญาต → redirect decoy (`baidu.com`)
- `streamXXX.com/play` ที่เปิดตรง → `HTTP 403`
- player ตรวจ `navigator.webdriver` → ต้องซ่อนร่องรอย automation

---

## 2. Authentication & Authorization Model

| กลไก | ที่ใช้ | รูปแบบ | อายุ | หมายเหตุ |
| :--- | :--- | :--- | :--- | :--- |
| `linkToken` | `GET /api/embed/links` | JWT (`header.payload.sig`) ส่งผ่าน `Authorization: Bearer` | ~ไม่กี่นาที (`exp` ใน payload) | ออกจาก `/api/embed/bootstrap`; ต่ออายุได้จาก response ของ `links` เอง |
| HLS token | `_index` / segment | query `sig` + `exp` (unix epoch) | **~15 นาที** | ผูกกับแต่ละ track (path) เดา/ประกอบเองไม่ได้ |
| `gw_enc` | ทุก request ชั้น HLS | query string (`o1`, `o3`, …) | คงที่ต่อ session | เลือก **CDN routing** ไม่ใช่คุณภาพวิดีโอ |
| Anti-embed | `streamXXX.com/play` | ตรวจ Referer/iframe ancestor | — | บล็อกด้วย `403` ถ้า origin ไม่ได้รับอนุญาต |

**linkToken payload (decoded, observed):**
```json
{ "id": "1084242", "type": "movie", "exp": 1789739328 }
```

---

## 3. Required Headers

| Header | ชั้นที่ต้องใช้ | ค่า | Requirement |
| :--- | :--- | :--- | :--- |
| `User-Agent` | ทุกชั้น | Desktop Chrome UA | Required — บาง endpoint บล็อก UA ว่าง/บอต |
| `Referer` | zmdb API, HLS | `https://zmdb.net/embed?id=...` (API), `https://zmdb.net/` (HLS) | Required |
| `Origin` | HLS (`g.zmdb.net`, CDN) | `https://zmdb.net` | Required — ไม่มี → `403` |
| `Authorization` | `GET /api/embed/links` | `Bearer <linkToken>` | Required |
| `Accept` | zmdb JSON API | `application/json` (หรือ `*/*`) | Optional (observed: `*/*` ใช้ได้) |

> หมายเหตุ: API ชุดนี้ **ไม่** ใช้ `X-Correlation-ID` / `X-Idempotency-Key` ตามมาตรฐาน
> enterprise ใน steering — endpoint ทั้งหมดเป็น `GET` ที่ safe/idempotent จึงไม่มี mutation contract

---

## 4. OpenAPI 3.1 — zmdb.net JSON API

```yaml
openapi: 3.1.0
info:
  title: zmdb Embed Playback API (reverse-engineered)
  version: 2026-09-18
  description: >
    Read-only JSON API ที่ player shell ของ zmdb เรียกเพื่อ resolve ลิงก์เล่นวิดีโอ
    ของเนื้อหาที่ถูกฝังผ่าน 77-hd.com เอกสารนี้อ้างอิงพฤติกรรมที่ observe ได้จริง
    ไม่ใช่สัญญาทางการของผู้ให้บริการ
servers:
  - url: https://zmdb.net
    description: Player shell + JSON API host

paths:
  /api/embed/bootstrap:
    get:
      operationId: getEmbedBootstrap
      summary: ขอ linkToken และ metadata ของเนื้อหาสำหรับ initialize player
      description: >
        จุดเริ่มต้นของ playback chain คืน `linkToken` (JWT อายุสั้น) ที่ต้องใช้ต่อ
        ในการเรียก `/api/embed/links` พร้อม metadata ของเรื่อง (ชื่อ, ปี, poster),
        รายการโฆษณา (`ads`), และประกาศ (`announcement`).
        Side-effects: ไม่มี (safe, cacheable ช่วงสั้น). ไม่ต้อง auth.
        Failure mitigation: ถ้า `success=false` หรือไม่มี `linkToken` ให้หยุด chain
        และตรวจว่า `id`/`type` ตรงกับ iframe บนหน้าเว็บหรือไม่.
      parameters:
        - name: id
          in: query
          required: true
          description: เลข content id จาก iframe `zmdb.net/embed?id=<id>` (ไม่ใช่ videoId ของ HLS)
          schema: { type: string, example: "1084242" }
        - name: type
          in: query
          required: true
          description: ชนิดเนื้อหา
          schema: { type: string, enum: [movie, tv], example: movie }
      responses:
        '200':
          description: สำเร็จ — คืน metadata + linkToken
          content:
            application/json:
              schema: { $ref: '#/components/schemas/BootstrapResponse' }
        '404':
          description: ไม่พบเนื้อหา (id/type ไม่ถูกต้อง)   # inferred

  /api/embed/links:
    get:
      operationId: getEmbedLinks
      summary: คืนรายการลิงก์เล่น (embedUrl) ต่อภาษา/เซิร์ฟเวอร์
      description: >
        คืน `playerEmbedLinks[]` แต่ละตัวชี้ไปยัง player ปลายทาง (`streamXXX.com/play/<videoId>`)
        พร้อม label ภาษา/คุณภาพ/เซิร์ฟเวอร์ และคืน `linkToken` ตัวใหม่ (rotation).
        หมายเหตุเชิงสถาปัตยกรรม: ลิงก์ทุกภาษามักชี้ไป **videoId เดียวกัน** เพราะ master
        playlist บรรจุทุก audio/subtitle track อยู่แล้ว — การเลือกภาษาเกิดตอนเล่น/ดาวน์โหลด
        Side-effects: ไม่มี. Requires `Authorization: Bearer <linkToken>`.
        Failure mitigation: `401` → linkToken หมดอายุ ให้เรียก `bootstrap` ใหม่แล้ว retry
        (player จริง retry 1 ครั้งเมื่อเจอ 401).
      parameters:
        - name: id
          in: query
          required: true
          schema: { type: string, example: "1084242" }
        - name: type
          in: query
          required: true
          schema: { type: string, enum: [movie, tv], example: movie }
        - name: season
          in: query
          required: false
          description: ใช้เมื่อ type=tv   # inferred จาก player.js
          schema: { type: integer }
        - name: episode
          in: query
          required: false
          description: ใช้เมื่อ type=tv   # inferred จาก player.js
          schema: { type: integer }
      security:
        - bearerLinkToken: []
      responses:
        '200':
          description: สำเร็จ — รายการลิงก์เล่น
          content:
            application/json:
              schema: { $ref: '#/components/schemas/LinksResponse' }
        '401':
          description: linkToken ไม่ถูกต้อง/หมดอายุ — ให้ refresh ผ่าน bootstrap แล้ว retry

components:
  securitySchemes:
    bearerLinkToken:
      type: http
      scheme: bearer
      bearerFormat: JWT
      description: linkToken ที่ได้จาก /api/embed/bootstrap

  schemas:
    BootstrapResponse:
      type: object
      required: [content, linkToken]
      properties:
        success:
          type: boolean
          description: บาง response omit ฟิลด์นี้เมื่อสำเร็จ   # inferred
        linkToken:
          type: string
          description: JWT อายุสั้น ใช้เป็น Bearer ในการเรียก /api/embed/links
        content:
          $ref: '#/components/schemas/Content'
        announcement:
          type: object
          description: ประกาศที่แสดงใน player (มักปิดอยู่)
          properties:
            enabled: { type: boolean }
            dismissible: { type: boolean }
            blocking: { type: boolean }
            templates: { type: array, items: { type: object } }
        ads:
          type: object
          description: การตั้งค่าโฆษณา (overlay / unlock gate)
        topCueNoticesEnabled: { type: boolean }
        query:
          type: object
          description: echo ของ query parameters ที่ส่งเข้ามา
          properties:
            id: { type: string }
            type: { type: string }
            season: { type: string }
            episode: { type: string }
            audio: { type: string }
            subtitle: { type: string }

    Content:
      type: object
      required: [id, mediaType]
      properties:
        id:
          type: string
          description: content id ภายใน (hex 24 ตัว) — คนละตัวกับ videoId ของ HLS
          example: "6931908bac8d37c42564fffb"
        mediaType:
          type: string
          enum: [movie, tv]
        title: { type: string, example: "Zootopia 2" }
        titleTh: { type: string, example: "นครสัตว์มหาสนุก 2" }
        year: { type: integer, example: 2025 }
        posterPath:
          type: string
          description: path สัมพัทธ์บน TMDB image CDN
          example: "/bjUWGw0Ao0qVWxagN3VCwBJHVo6.jpg"
        isComingSoon: { type: boolean }
        isVipOnly: { type: boolean }
        playerEmbedLinks:
          type: array
          items: { $ref: '#/components/schemas/EmbedLink' }

    LinksResponse:
      type: object
      required: [playerEmbedLinks, success]
      properties:
        success: { type: boolean }
        linkToken:
          type: string
          description: linkToken ตัวใหม่ (rotation) — ใช้แทนตัวเดิมในคำขอถัดไป
        playerEmbedLinks:
          type: array
          items: { $ref: '#/components/schemas/EmbedLink' }

    EmbedLink:
      type: object
      required: [language, qualityLabel, serverLabel]
      properties:
        embedUrl:
          type: [string, "null"]
          description: >
            URL player ปลายทาง เช่น
            `https://stream037.com/play/<videoId>?audio=tha&subtitle=none`.
            เป็น null/ว่างได้เมื่อยังไม่มีลิงก์หรือเป็น VIP-only
          example: "https://stream037.com/play/6a0e8a21600e1e8a46343282?audio=tha&subtitle=none"
        language:
          type: string
          description: label ภาษา (ข้อความไทย)
          example: "พากย์ไทย"
        qualityLabel:
          type: string
          description: >
            label คุณภาพจาก CMS — **เป็นเพียงป้ายกำกับ ไม่ใช่ความละเอียดจริงของสตรีม**
            (เช่น ระบุ "4K" แต่ master จริงอาจเป็น 1920x800)
          example: "4K"
        serverLabel:
          type: string
          example: "Server 1"
        isVipOnly: { type: boolean }
```

---

## 5. HLS / CDN Layer (นอกขอบเขต OpenAPI — เป็น media artifacts)

Endpoint กลุ่มนี้คืน `m3u8` / `json` / binary segment ไม่ใช่ REST resource จึงอธิบายเป็นตาราง:

| Method & Path | คืนค่า | Auth / Query | หมายเหตุ |
| :--- | :--- | :--- | :--- |
| `GET g.zmdb.net/hls/<videoId>/t.<hash>/_master?gw_enc=<o>` | `application/vnd.apple.mpegurl` (master m3u8) | `gw_enc`; headers `Origin`/`Referer` | ลิสต์ `#EXT-X-STREAM-INF` (วิดีโอ) + `#EXT-X-MEDIA` (audio/subs) + `#EXT-X-CONTENT-STEERING`. master URL เองไม่มี `sig`/`exp` |
| `GET g.zmdb.net/hls/playback-routing.json?gw_enc=<o>` | `application/json` (steering manifest) | `gw_enc` | ระบุ CDN mirror host จริงสำหรับดึง segment (host เปลี่ยนตาม `gw_enc`) |
| `GET g.zmdb.net/hls/<videoId>/_<track>/_index?sig=<>&exp=<>&gw_enc=<o>` | media playlist (m3u8) | `sig`+`exp` (~15 นาที) | `<track>` = `_v` (วิดีโอ), `_a_1`,`_a_2` (เสียง), `_s`/`_s/_tha` (ซับ) |
| `GET <mirror>/hls/<videoId>/_<track>/hdr__<track>.bin` | binary (init/map segment) | — | mirror เช่น `lb.osx-cdn2.space` (จาก routing.json) |
| `GET <mirror>/hls/<videoId>/_<track>/s_<NNNNN>.bin` | binary (media segment) | — | เสิร์ฟจาก mirror — **`403` ถ้าขอจาก `g.zmdb.net` โดยตรง** |
| `GET <mirror>/hls/<videoId>/_s/<lang>.vtt` | `text/vtt` (subtitle) | — | ซับเป็น WebVTT |

**master m3u8 ตัวอย่าง (observed — Zootopia):**
```
#EXT-X-STREAM-INF:BANDWIDTH=…,RESOLUTION=1920x800,FRAME-RATE=24.000,VIDEO-RANGE=SDR,
  CODECS="avc1.640028,mp4a.40.2",AUDIO="group_audio",SUBTITLES="subs"
/hls/<videoId>/_v/_index?sig=…&exp=…&gw_enc=o3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",LANGUAGE="tha",NAME="…",DEFAULT=YES,URI="…"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",LANGUAGE="eng",…
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="tha",…
#EXT-X-CONTENT-STEERING:SERVER-URI="…playback-routing.json?gw_enc=o3"
```

**videoId** (ในชั้น HLS) = hex 24 ตัว เช่น `6a0e8a21600e1e8a46343282` — สกัดได้จาก `embedUrl`
(`.../play/<videoId>?...`) คนละค่ากับ `content.id` ในชั้น bootstrap

---

## 6. Error Handling (observed)

API ชุดนี้ **ไม่** ใช้ RFC 9457 (`application/problem+json`) — คืนรูปแบบเฉพาะตัว:

| ชั้น | สถานะ | body / อาการ | การรับมือ |
| :--- | :--- | :--- | :--- |
| zmdb JSON API | `200` + `{"success": false, "error": "..."}` | error ระดับ application | อ่าน `error`, หยุด chain |
| `/api/embed/links` | `401` | linkToken หมดอายุ | เรียก `/api/embed/bootstrap` ใหม่ → retry (1 ครั้ง) |
| `streamXXX.com/play` | `403` | HTML `"Access Denied \| Security Protection … cannot be embedded from unauthorized sources"` | โหลดผ่านหน้า `77-hd.com` จริง (iframe ancestor ถูกต้อง) |
| HLS segment @ `g.zmdb.net` | `403` | segment ถูกปฏิเสธที่ origin host | ดึง segment จาก CDN mirror ใน `playback-routing.json` |
| `_index` / segment | หมดอายุ token | `403`/`410` (sig/exp เกิน ~15 นาที) | re-fetch master เพื่อขอ `sig` สด (ดู `FreshResolver` ใน `fetch_zmdb.py`) |
| player ถูกเปิดนอก iframe | — | main frame redirect ไป `https://www.baidu.com/` (decoy) | โหลดผ่านหน้า 77-hd จริง + ซ่อน `navigator.webdriver` |

> รูปแบบ error ที่ **แนะนำ** ให้ client ภายในของเราสร้างต่อ (เพื่อ trace) ควรใช้ RFC 9457
> `application/problem+json` (`type`,`title`,`status`,`detail`,`instance`) ครอบพฤติกรรมข้างต้น
> แม้ต้นทางจะไม่ได้ส่งมาในรูปแบบนั้น

---

## 7. ลำดับการเรียก (Sequence) สำหรับ resolve master URL

```
Client                77-hd.com        zmdb.net                 streamXXX / g.zmdb.net
  │  GET /<slug>/  ───────►│
  │◄── HTML + iframe src ──│
  │  (โหลดหน้าเป็น browser จริง ให้ iframe chain ถูกต้อง)
  │                        │
  │  GET /api/embed/bootstrap?id&type ──────►│
  │◄──────────── linkToken + content ────────│
  │                                          │
  │  GET /api/embed/links?id&type            │
  │      Authorization: Bearer <linkToken> ─►│
  │◄──────────── playerEmbedLinks[] ─────────│
  │                                          │
  │  (player เนสต์ streamXXX โหลด hls.js)     │
  │  GET /hls/<videoId>/t.<hash>/_master?gw_enc  ──────────────►│
  │◄──────────────────────── master m3u8 ──────────────────────│
  │  GET /hls/playback-routing.json?gw_enc   ──────────────────►│
  │◄──────────────────────── CDN mirror manifest ──────────────│
  │  GET /hls/<videoId>/_<track>/_index?sig&exp&gw_enc ────────►│
  │◄──────────────────────── media playlist ───────────────────│
  │  GET <mirror>/hls/<videoId>/_<track>/s_NNNNN.bin ──────────►│
  │◄──────────────────────── segment bytes ────────────────────│
```

---

## 8. Client Reference Mapping (โปรเจกต์นี้)

| ขั้นตอนใน spec | โค้ดที่ทำจริง |
| :--- | :--- |
| §1 ชั้น 1-3 (page → embed id → bootstrap → links) | `download_video_77hd.py` : `resolve_embed()`, `get_bootstrap()`, `get_links()` |
| §1 ชั้น 4 (โหลดหน้าจริง + ดัก master) | `download_video_77hd.py` : `grab_master()` (Playwright + stealth) |
| §5 master → media playlist → segment | `fetch_zmdb.py` : `parse_videos()`, `parse_media()`, `download_playlist()` |
| §2 HLS token refresh (~15 นาที) | `fetch_zmdb.py` : `FreshResolver` |
| §6 segment 403 → CDN mirror | `fetch_zmdb.py` : `resolve_seg_hosts()` (อ่าน `playback-routing.json`) |

---

## 9. หมายเหตุกฎหมาย / ความเสี่ยง

- เอกสารนี้เป็นการ document พฤติกรรมที่สังเกตได้ของ API บุคคลที่สามที่ไม่มีสัญญาทางการ
- โครงสร้าง endpoint, ชื่อ CDN mirror, และรูปแบบ token อาจเปลี่ยนได้ทุกเมื่อ
- ป้าย `qualityLabel` (เช่น "4K") เป็น metadata การตลาด **ไม่รับประกันความละเอียดจริง**
  ให้ตรวจ `RESOLUTION` จาก master m3u8 เท่านั้นเป็นค่าจริง
- ใช้กับเนื้อหาที่คุณมีสิทธิ์ดาวน์โหลด/เข้าถึงเท่านั้น
