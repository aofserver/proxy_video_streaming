#!/usr/bin/env python3
"""
main.py — เซิร์ฟเวอร์หลัก (FastAPI/uvicorn) สำหรับเล่น/ดาวน์โหลดวิดีโอ 77-hd/zmdb

ใช้ logic เดิม "ทั้งหมด" จาก tools_77hd (import มาใช้ ไม่ก็อปโค้ด) เปลี่ยนแค่ชั้น HTTP:
  * endpoint เป็น def ธรรมดา (ไม่ใช่ async def) -> Starlette รันใน threadpool ให้เอง
    ทำให้ blocking calls เดิม (urllib http_get, subprocess, sync_playwright) ไม่บล็อก event loop
  * /file ใช้ FileResponse -> ได้ HTTP Range (seek/resume) ฟรี (ของเดิมไม่รองรับ)
  * client disconnect Starlette จัดการเอง (ไม่ต้อง guard BrokenPipe เอง)

⚠️ ต้องรัน worker เดียวเท่านั้น: state เป็น global dict ในหน่วยความจำ (STATE/DOWNLOADS/MEDIA_PL)
   ถ้ารันหลาย worker (หลาย process) แต่ละตัวเห็น state คนละชุด -> ดาวน์โหลด/pin master พัง

รัน:
  python main.py --host 0.0.0.0 --port 8080
  # หรือ
  uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.parse
import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

import tools_77hd as S   # reuse ทั้งหมด: STATE, locks, helpers, proxy/download/resolve logic
import movies_db         # ที่เก็บรายการหนัง (SQLite) — ใช้นับแถวตอนเริ่มเซิร์ฟเวอร์

M3U8 = "application/vnd.apple.mpegurl"
JSONH = "application/json; charset=utf-8"

app = FastAPI(title="77hd local proxy + player (FastAPI)")

_CONFIGURED = False       # main() ตั้งค่า STATE ให้แล้วหรือยัง (กัน startup event ตั้งซ้ำ)


# --------------------------------------------------------------------------- #
# helpers
def _json(obj, status: int = 200) -> Response:
    """ตอบ JSON แบบ ensure_ascii=False คงลำดับ key เหมือนตัว stdlib เดิมเป๊ะ ๆ"""
    return Response(json.dumps(obj, ensure_ascii=False),
                    status_code=status, media_type=JSONH)


def _q(request: Request, key: str, default: str = "") -> str:
    """ค่า query param ตัวแรก (เทียบเท่า parse_qs(...)[key][0])"""
    v = request.query_params.get(key)
    return v if v is not None else default


def _configure_browse() -> None:
    """โหมด browse: โหลดรายการหนังจาก movies.db (เหมือน main() ของตัวเดิม)"""
    S.STATE["browse"] = True
    n = movies_db.count()
    if not n:
        print(f"เตือน: ยังไม่มีรายการหนังใน {movies_db.DB_PATH.name} — รัน "
              f"`python3 list_77hd.py` ก่อน", file=sys.stderr)
        return
    print(f"โหมด browse: โหลดรายการหนัง {n} เรื่องจาก {movies_db.DB_PATH.name}",
          file=sys.stderr)


@app.on_event("startup")
def _on_startup() -> None:
    # เผื่อรันผ่าน `uvicorn main:app` ตรง ๆ (ไม่ผ่าน main()) -> default browse
    if not _CONFIGURED:
        _configure_browse()


# --------------------------------------------------------------------------- #
# routes (map 1:1 กับ do_GET ของ tools_77hd.py)
@app.get("/")
def root() -> Response:
    if S.STATE.get("browse"):
        return Response(S.BROWSE_TEMPLATE.read_bytes(), media_type="text/html; charset=utf-8")
    return Response(S.render_player(), media_type="text/html; charset=utf-8")


@app.get("/player")
def player() -> Response:
    return Response(S.render_player(), media_type="text/html; charset=utf-8")


@app.get("/movies.json")
def movies_json() -> Response:
    movies = S.merged_movies()
    if not movies:
        return Response(b"[]", status_code=404, media_type=JSONH)
    return _json(movies)


@app.get("/movie.json")
def movie_json(request: Request) -> Response:
    # หน้า player เปิดด้วย ?id= แล้วมาถามเรื่องนี้จาก DB
    try:
        movie = S.find_movie(int(_q(request, "id")))
    except ValueError:
        movie = None
    if movie is None:
        return _json({"error": "not found"}, 404)
    return _json(movie)


@app.get("/resolve")
def resolve(request: Request) -> Response:
    page = _q(request, "url")
    title = _q(request, "title")
    if not page:
        return _json({"ok": False, "error": "missing url"}, 400)

    # ---- 24hd: master อยู่ในหน้า embed (HTTP ธรรมดา) ไม่ต้องเปิด Chromium/ไม่ต้อง lock ----
    if S.is_24hd_page(page):
        try:
            S.STATE["source"] = page
            S.STATE["title"] = title or None
            S.STATE["description"] = _q(request, "desc") or None
            S.STATE["image"] = _q(request, "image") or None
            S.STATE["quality"] = _q(request, "quality") or None
            S.STATE["sound"] = _q(request, "sound") or None
            info = S.resolve_24hd(page)
            meta = {
                "title": S.STATE.get("title") or "",
                "source": S.STATE.get("source") or "",
                "description": S.STATE.get("description") or "",
                "image": S.STATE.get("image") or "",
                "quality": S.STATE.get("quality") or "",
                "sound": S.STATE.get("sound") or "",
            }
            return _json({"ok": True, "meta": meta, **info})
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": str(e)}, 502)

    # กันยิงซ้อน: กำลังเปิด Chromium อีกเรื่องอยู่ -> ปฏิเสธ (กันแรมหมด)
    if not S.RESOLVE_LOCK.acquire(blocking=False):
        return _json({"ok": False,
                      "error": "กำลังเปิดอีกเรื่องอยู่ กรุณารอให้เรื่องก่อนหน้าเสร็จก่อน"}, 429)
    try:
        S.STATE["source"] = page
        S.STATE["title"] = title or None
        S.STATE["description"] = _q(request, "desc") or None
        S.STATE["image"] = _q(request, "image") or None
        S.STATE["quality"] = _q(request, "quality") or None
        S.STATE["sound"] = _q(request, "sound") or None
        full_syn = S.fetch_full_synopsis(page)
        if full_syn:
            prefix = ""
            old = S.STATE["description"] or ""
            if "เรื่องย่อ" in old:
                prefix = old.split("เรื่องย่อ")[0]
            S.STATE["description"] = (
                f"{prefix}เรื่องย่อ: {full_syn}" if prefix.strip() else full_syn)
        info = S.resolve_master(page)
        meta = {
            "title": S.STATE.get("title") or "",
            "source": S.STATE.get("source") or "",
            "description": S.STATE.get("description") or "",
            "image": S.STATE.get("image") or "",
            "quality": S.STATE.get("quality") or "",
            "sound": S.STATE.get("sound") or "",
        }
        return _json({"ok": True, "meta": meta, **info})
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": str(e)}, 502)
    finally:
        S.RESOLVE_LOCK.release()


@app.get("/update")
def update(request: Request) -> Response:
    with_desc = _q(request, "desc", "0") == "1"
    started = S.start_update(with_desc)
    with S.UPDATE_LOCK:
        snap = dict(S.UPDATE)
    return _json({"ok": True, "started": started, **snap})


@app.get("/update-status")
def update_status() -> Response:
    with S.UPDATE_LOCK:
        snap = dict(S.UPDATE)
    return _json(snap)


@app.get("/download")
def download(request: Request) -> Response:
    fmt = _q(request, "fmt", "mp4")
    height = _q(request, "height")
    source = _q(request, "source") or S.STATE.get("source") or ""
    master = _q(request, "master") or S.STATE.get("master") or ""
    title = _q(request, "title") or S.STATE.get("title") or "video"
    started, msg, key = S.start_download(source, master, fmt, title, height)
    with S.DOWNLOAD_LOCK:
        snap = dict(S.DOWNLOADS.get(key, S._idle_job()))
    return _json({"ok": started, "message": msg, "source": key, **snap},
                 200 if started else 409)


@app.get("/download-status")
def download_status(request: Request) -> Response:
    source = _q(request, "source") or S.STATE.get("source") or "__default__"
    with S.DOWNLOAD_LOCK:
        if _q(request, "all") == "1":
            snap = {"jobs": {k: dict(v) for k, v in S.DOWNLOADS.items()}}
        else:
            snap = dict(S.DOWNLOADS.get(source, S._idle_job()))
            snap["source"] = source
    return _json(snap)


@app.get("/file")
def file(request: Request):
    from pathlib import Path
    raw = _q(request, "name")
    # อนุญาตโฟลเดอร์ย่อยต่อเรื่อง (<เรื่อง>/หนัง.mp4) แต่กัน path traversal
    comps = [p for p in Path(raw).parts if p not in ("", ".", "..")]
    fp = S.DOWNLOAD_DIR.joinpath(*comps).resolve() if comps else S.DOWNLOAD_DIR
    safe = comps[-1] if comps else ""
    base = S.DOWNLOAD_DIR.resolve()
    if (not comps or not fp.is_file() or (fp != base and base not in fp.parents)):
        return Response(b"not found", status_code=404, media_type="text/plain; charset=utf-8")
    # FileResponse รองรับ HTTP Range (seek/resume) + ตั้ง Content-Disposition attachment ให้เอง
    return FileResponse(fp, media_type="application/octet-stream",
                        filename=safe)


@app.get("/master.m3u8")
def master_m3u8(request: Request) -> Response:
    try:
        master = _q(request, "m") or S.STATE["master"]
        text = S.master_text(master)                 # sig สดทุกครั้ง / referer ตามแหล่ง
        body = S.rewrite_m3u8(text, master, master_pl=True)
        return Response(body, media_type=M3U8)
    except Exception as e:  # noqa: BLE001
        print(f"proxy error: /master.m3u8 -> {e}", file=sys.stderr)
        return Response(f"proxy error: {e}", status_code=502, media_type="text/plain")


@app.get("/p")
def proxy(request: Request) -> Response:
    u_b64 = request.query_params.get("u")
    if not u_b64:
        return Response(b"missing u", status_code=400, media_type="text/plain")
    try:
        u = S.b64d(u_b64)
        seg = _q(request, "seg", "0") == "1"
        data = S.fetch_upstream(u, seg)
        if data[:7] == b"#EXTM3U":                   # media playlist -> rewrite
            seg_dir = urllib.parse.urlparse(u).path.rpartition("/")[0]
            with S.MEDIA_PL_LOCK:
                S.MEDIA_PL[seg_dir] = u
            body = S.rewrite_m3u8(data.decode("utf-8", "replace"), u, master_pl=False)
            return Response(body, media_type=M3U8)
        if u.split("?")[0].endswith(".vtt"):
            return Response(data, media_type="text/vtt; charset=utf-8")
        return Response(data, media_type="application/octet-stream")
    except Exception as e:  # noqa: BLE001
        print(f"proxy error: /p -> {e}", file=sys.stderr)
        return Response(f"proxy error: {e}", status_code=502, media_type="text/plain")


# --------------------------------------------------------------------------- #
def main() -> int:
    global _CONFIGURED
    import uvicorn

    ap = argparse.ArgumentParser(
        description="local proxy + player (FastAPI/uvicorn) สำหรับเล่นวิดีโอ 77-hd/zmdb")
    ap.add_argument("source", nargs="?",
                    help="URL หน้าหนัง 77-hd หรือ master URL ตรง ๆ (ไม่ใส่ = โหมด browse)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="เปิดเบราว์เซอร์อัตโนมัติ")
    ap.add_argument("--headful", action="store_true", help="[URL 77-hd] เปิด Chrome เห็นหน้าต่างตอนหา master")
    ap.add_argument("--timeout", type=int, default=60, help="[URL 77-hd] วินาทีรอดัก master")
    args = ap.parse_args()

    if args.source:
        S.STATE["browse"] = False
        if S.is_master_source(args.source):
            print("ใช้ master URL ที่ให้มาโดยตรง", file=sys.stderr)
        else:
            print("หา master URL จากหน้า 77-hd (Playwright) ...", file=sys.stderr)
        info = S.resolve_master(args.source, headful=args.headful, timeout_s=args.timeout)
        print(f"  master: {info['master']}", file=sys.stderr)
        print(f"  CDN mirror: {info['seg_hosts']}", file=sys.stderr)
        if info["resolutions"]:
            print(f"  ความละเอียด: {', '.join(info['resolutions'])}", file=sys.stderr)
    else:
        _configure_browse()
    _CONFIGURED = True

    url = f"http://{args.host}:{args.port}/"
    label = "รายการหนัง (browse)" if S.STATE["browse"] else "player"
    print(f"\n▶ เปิด {label} ที่: {url}  (FastAPI/uvicorn, 1 worker)  Ctrl+C เพื่อหยุด\n",
          file=sys.stderr)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # 1 worker เท่านั้น (state อยู่ในหน่วยความจำ) — ส่ง app object ตรง ๆ = single process
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"ผิดพลาด: {e}", file=sys.stderr)
        sys.exit(1)
