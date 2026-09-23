#!/usr/bin/env python3
"""
tools_77hd.py — core logic (proxy/resolve/download) + เซิร์ฟเวอร์ stdlib สำรอง สำหรับ 77-hd/zmdb
                main.py (FastAPI) import โมดูลนี้ไปใช้ทั้งหมด — รันไฟล์นี้ตรง ๆ ก็ได้ (stdlib http.server)

ทำไมต้องมี proxy:
  ชั้น HLS ของ g.zmdb.net / CDN mirror บังคับ header `Origin: https://zmdb.net` +
  `Referer` + desktop UA (ไม่งั้น 403) แต่ hls.js ในเบราว์เซอร์เซ็ต header พวกนี้เองไม่ได้
  → รัน proxy ฝั่ง Python มาฉีด header ให้ แล้ว rewrite URL ใน m3u8 ให้วิ่งกลับ proxy
  (segment host จะถูก swap ไป CDN mirror ตาม playback-routing.json อัตโนมัติ)

  master m3u8 และ segment เข้าถึงได้ด้วย header อย่างเดียว (พิสูจน์แล้วด้วย probe.sh/fetch_zmdb)
  ส่วน anti-embed อยู่แค่ที่หน้า streamXXX/zmdb-embed ซึ่ง proxy นี้ "ข้ามทั้งเลเยอร์"
  โดยเล่น master โดยตรง จึงไม่ติด 403/redirect baidu

รับ source ได้ 2 แบบ:
  * URL หน้าหนัง 77-hd  → ใช้ Playwright (download_video_77hd.grab_master) หา master ให้
  * master URL ตรง ๆ   (g.zmdb.net/.../_master?gw_enc=..) → ใช้เลย ไม่ต้องเปิดเบราว์เซอร์

ตัวอย่าง:
  ./tools_77hd.py "https://77-hd.com/zootopia-2-2025/" --open
  ./tools_77hd.py "https://g.zmdb.net/hls/<id>/t.<hash>/_master?gw_enc=o3" --port 8080

ความปลอดภัย: bind 127.0.0.1 เท่านั้น ไม่มี auth
"""
from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_zmdb as fz              # reuse: http_get, get_text, swap_host, resolve, ฯลฯ
import download_video_77hd as dv     # reuse: grab_master (Playwright)
import list_77hd as lister           # reuse: run_crawl (ปุ่มอัปเดตรายการหนัง)
import list_24hd as lister24         # reuse: run_crawl (รายการหนัง 24hd)

# --- แหล่ง 24hd (vdohls) : master URL อยู่ใน JSON แล้ว ไม่มี token/steering ---
# ทุก request ไป vdohls/CDN ของมันต้องมี Referer นี้ (ไม่งั้น 403) — ไม่ต้องมี Origin
VDOHLS_REFERER = "https://player77hdfree.xyz/"


def is_vdohls_url(u: str) -> bool:
    """เป็น URL ของสตรีม 24hd (vdohls) หรือไม่ — segment host เปลี่ยนได้ (vhNNN.xyz)
    จึงเช็คจาก playlist host (vdohls) + host segment ที่รู้ (vh*.xyz)"""
    if not u:
        return False
    host = urllib.parse.urlparse(u).netloc.lower()
    return "vdohls.com" in host or re.match(r"vh\d+\.", host) is not None


def _vdohls_headers() -> dict:
    """header สำหรับยิงไป vdohls/CDN ของมัน (ต้องมี Referer ไม่งั้น 403)"""
    return {
        "User-Agent": fz.HEADERS["User-Agent"],
        "Referer": VDOHLS_REFERER,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    }


def vdohls_get(url: str) -> bytes:
    """ดึงข้อมูลจาก vdohls ด้วย referer ที่ถูกต้อง (playlist หรือ segment)"""
    return fz.http_get_with(url, _vdohls_headers(), tries=4)


def master_text(master: str) -> str:
    """โหลดข้อความ master playlist ด้วย header ที่ถูกต้องตามแหล่ง
    (vdohls ต้องใช้ referer เฉพาะ ไม่งั้น 403; zmdb ใช้ header เดิม + sig สดทุกครั้ง)"""
    if is_vdohls_url(master):
        return vdohls_get(master).decode("utf-8", "replace")
    return fz.get_text(master)


def is_24hd_page(u: str) -> bool:
    """เป็น URL หน้าหนังของ 24hd.media หรือไม่"""
    if not u:
        return False
    return "24hd.media" in urllib.parse.urlparse(u).netloc.lower()


def resolve_24hd(page_url: str) -> dict:
    """หา master URL ของ 24hd จากหน้าหนัง/embed โดยตรง (HTTP ธรรมดา ไม่ใช้ Playwright)
    คืน dict รูปแบบเดียวกับ resolve_master: {master, seg_hosts, resolutions}
    และเซ็ต STATE['master'] ให้ (seg_hosts ว่าง — vdohls โหลด segment จาก host เดิม)"""
    info = lister24.resolve_master(page_url)     # {embed, master, description, image}
    master = info.get("master") or ""
    if not master:
        raise RuntimeError("ไม่พบวิดีโอสำหรับเรื่องนี้ (อาจยังไม่ปล่อย/มีแต่ตัวอย่าง)")
    STATE["master"] = master
    STATE["seg_hosts"] = []                       # vdohls ไม่ swap host (ใช้ host เดิมของ segment)
    # เติม description/image ถ้า STATE ยังว่าง (จาก resolve_master ของ 24hd)
    if not STATE.get("description") and info.get("description"):
        STATE["description"] = info["description"]
    if not STATE.get("image") and info.get("image"):
        STATE["image"] = info["image"]
    resolutions: list[str] = []
    try:
        mtext = master_text(master)
        resolutions = sorted(set(re.findall(r"RESOLUTION=(\d+x\d+)", mtext)))
    except Exception:  # noqa: BLE001
        pass
    return {"master": master, "seg_hosts": [], "resolutions": resolutions}





# state ที่แชร์ให้ handler (master URL + รายชื่อ CDN mirror host)
STATE: dict = {"master": None, "seg_hosts": [], "title": None, "browse": False,
               "source": None,
               "description": None, "image": None, "quality": None, "sound": None}

# สถานะการอัปเดตรายการหนัง (ปุ่มใน browse.html)
UPDATE: dict = {"running": False, "phase": "idle", "page": 0, "total_pages": 0,
                "movies": 0, "done": 0, "total": 0, "count": 0, "error": None}
UPDATE_LOCK = threading.Lock()

# กันเปิด Chromium (Playwright) หลายตัวพร้อมกัน — คลิกรัว/ดับเบิลคลิกในหน้า browse
# ทำให้ /resolve ถูกยิงซ้อน แต่ละครั้งเปิด browser 1 ตัว บนเครื่อง 8GB = แรมหมด เครื่องค้าง
RESOLVE_LOCK = threading.Lock()

# สถานะการดาวน์โหลดวิดีโอ (ปุ่ม download ใน player)
# รองรับหลายเรื่องพร้อมกัน — เก็บเป็น dict คีย์ด้วย "source" (URL หน้าเว็บของเรื่องนั้น)
# แต่ละแท็บ = 1 เรื่อง = 1 งานดาวน์โหลด แยกกันอิสระ (คีย์ source คงที่แม้กด F5)
DOWNLOAD_DIR = HERE / "downloads"
DOWNLOADS: dict = {}          # source -> job dict
DOWNLOAD_LOCK = threading.Lock()
MAX_DOWNLOADS = 4             # กันโหลดพร้อมกันเยอะเกินจนเครื่อง/CDN รับไม่ไหว


def _idle_job() -> dict:
    return {"running": False, "phase": "idle", "line": "", "pct": None,
            "file": None, "error": None, "fmt": None, "title": None}

# แคช: ไดเรกทอรีของ segment -> URL ของ media playlist ที่มันอยู่ (proxy ดึงมา rewrite)
# ใช้ตอน segment token หมดอายุ (หนังยาว/เปิดค้าง) → ดึง playlist ใหม่ผ่าน master เพื่อขอ URL segment ที่ sig สด
MEDIA_PL: dict = {}
MEDIA_PL_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
def b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode()).decode()


def is_master_source(src: str) -> bool:
    return "_master" in src or "g.zmdb.net" in src


# --------------------------------------------------------------------------- #
# rewrite ทุก URL ใน m3u8 ให้วิ่งผ่าน proxy ของเรา
#   - master  : variant playlist + EXT-X-MEDIA URI -> /p?u=..  (เป็น m3u8 บน g.zmdb.net)
#   - media   : segment + EXT-X-MAP -> /p?u=..&seg=1           (ต้อง host-swap ไป mirror)
#   - ตัด CONTENT-STEERING ทิ้ง (เรา host-swap เองไม่ให้ hls.js ไปยุ่ง routing)
# --------------------------------------------------------------------------- #
def rewrite_m3u8(text: str, base_url: str, master_pl: bool) -> str:
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#EXT-X-CONTENT-STEERING"):
            continue
        if s.startswith("#") and 'URI="' in s:
            m = re.search(r'URI="([^"]+)"', s)
            abs_u = fz.resolve(base_url, m.group(1))
            seg = "&seg=1" if s.startswith("#EXT-X-MAP") else ""  # EXT-X-MAP = init segment
            out.append(s.replace(m.group(1), f"/p?u={b64e(abs_u)}{seg}"))
            continue
        if not s or s.startswith("#"):
            out.append(ln)
            continue
        # บรรทัด URI เปล่า
        abs_u = fz.resolve(base_url, s)
        out.append(f"/p?u={b64e(abs_u)}" + ("" if master_pl else "&seg=1"))
    return "\n".join(out)


def _try_mirrors(u: str) -> bytes | None:
    """ลองโหลด segment ไล่ทุก CDN mirror; คืน bytes ถ้าได้ ไม่งั้น None"""
    for h in STATE["seg_hosts"]:
        try:
            return fz.http_get(fz.swap_host(u, h), tries=2)
        except Exception:  # noqa: BLE001
            continue
    return None


def _refresh_segment(seg_url: str) -> str | None:
    """ขอ URL ของ segment เดิมที่ token สด — ดึง media playlist ใหม่ผ่าน master (FreshResolver)
    แล้วจับคู่ด้วยชื่อไฟล์ (แก้ปัญหา sig/exp หมดอายุตอนดูหนังยาว ๆ)"""
    master = STATE.get("master")
    if not master:
        return None
    seg_path = urllib.parse.urlparse(seg_url).path
    seg_dir, _, seg_file = seg_path.rpartition("/")
    with MEDIA_PL_LOCK:
        pl_url = MEDIA_PL.get(seg_dir)
    if not pl_url:
        return None
    try:
        fresh_pl = fz.FreshResolver(master).fresh(pl_url)   # media playlist URL ที่ token สด
        text = fz.get_text(fresh_pl)
    except Exception:  # noqa: BLE001
        return None
    refs = re.findall(r'URI="([^"]+)"', text)
    refs += [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.startswith("#")]
    for ref in refs:
        cand = fz.resolve(fresh_pl, ref)
        if urllib.parse.urlparse(cand).path.rpartition("/")[2] == seg_file:
            return cand
    return None


def fetch_upstream(u: str, seg: bool) -> bytes:
    """ดึงจาก upstream พร้อม header ที่ถูกต้อง; ถ้าเป็น segment ให้ไล่ CDN mirror
    (ถ้า token หมดอายุจน 403 ทุก mirror → ขอ URL สดผ่าน master แล้วลองใหม่ 1 รอบ)"""
    # แหล่ง 24hd (vdohls): ไม่มี content steering/mirror — โหลดจาก host เดิม + referer เฉพาะ
    if is_vdohls_url(u):
        return vdohls_get(u)
    if seg and STATE["seg_hosts"]:
        data = _try_mirrors(u)
        if data is not None:
            return data
        fresh = _refresh_segment(u)      # token หมดอายุ? ขอใหม่จาก master
        if fresh:
            data = _try_mirrors(fresh)
            if data is not None:
                return data
        raise RuntimeError(f"segment โหลดไม่ได้จาก mirror ใดเลย (แม้ขอ token ใหม่แล้ว): {u}")
    return fz.http_get(u, tries=3)


# --------------------------------------------------------------------------- #
PLAYER_TEMPLATE = HERE / "player.html"
BROWSE_TEMPLATE = HERE / "browse.html"
MOVIES_JSON = HERE / "77hd_movies.json"
MOVIES_JSON_24 = HERE / "24hd_movies.json"


def _load_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def merged_movies() -> list[dict]:
    """รวมรายการหนังจากทั้ง 77hd และ 24hd เป็น list เดียว ติดป้าย field `site`
    เพื่อให้หน้า browse รู้ว่าแต่ละเรื่องมาจากแหล่งไหน (เล่นด้วยกลไกต่างกัน):
      - 77hd: ต้อง resolve master ด้วย Playwright ก่อน (ไม่มี field master)
      - 24hd: มี master URL (vdohls) มาแล้ว เล่น/โหลดได้ทันที
    """
    out: list[dict] = []
    for m in _load_json_list(MOVIES_JSON):
        m = dict(m)
        m.setdefault("site", "77hd")
        out.append(m)
    for m in _load_json_list(MOVIES_JSON_24):
        m = dict(m)
        m["site"] = "24hd"
        out.append(m)
    return out


def render_player() -> bytes:
    """โหลด player.html แล้วฉีดชื่อ + ข้อมูลหนัง (เรื่องย่อ/โปสเตอร์/คุณภาพ/เสียง)"""
    html_txt = PLAYER_TEMPLATE.read_text(encoding="utf-8")
    raw_title = STATE.get("title") or "Local Player"
    title = html_mod.escape(raw_title)
    # ข้อมูล meta ฉีดเป็น JSON ให้ฝั่ง JS ประกอบ info panel เอง (ปลอดภัยจาก HTML injection)
    meta = {
        "title": raw_title,
        "source": STATE.get("source") or "",
        "description": STATE.get("description") or "",
        "image": STATE.get("image") or "",
        "quality": STATE.get("quality") or "",
        "sound": STATE.get("sound") or "",
    }
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    return (html_txt
            .replace("__TITLE__", title)
            .replace("__META_JSON__", meta_json)
            .encode("utf-8"))


def fetch_full_synopsis(page_url: str) -> str | None:
    """ดึงเรื่องย่อ 'เต็ม' จากหน้า 77-hd (<div class="synopsis-content">)
    เพราะ 77hd_movies.json เก็บมาแค่พรีวิวที่ถูกตัดด้วย '...' — best-effort"""
    if is_master_source(page_url):
        return None
    try:
        raw = dv.http_get(page_url, referer=page_url, timeout=12)
        html_txt = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'class="synopsis-content"[^>]*>(.*?)</div>', html_txt, re.S)
    if not m:
        return None
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    txt = html_mod.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


def resolve_master(source: str, *, headful: bool = False,
                   timeout_s: int = 60) -> dict:
    """หา master URL (ถ้าเป็น URL หน้าเว็บใช้ Playwright) + คำนวณ CDN mirror,
    เซ็ตลง STATE แล้วคืนข้อมูลสรุป (master, seg_hosts, resolutions)"""
    if is_master_source(source):
        master = source
    else:
        master = dv.grab_master(source, headful=headful, timeout_s=timeout_s)
    STATE["master"] = master

    try:
        mtext = fz.get_text(master)
        steer = fz.steering_uri_of(mtext)
        STATE["seg_hosts"] = fz.resolve_seg_hosts(master, steer)
    except Exception as e:  # noqa: BLE001
        print(f"  เตือน: อ่าน steering ไม่ได้ ({e}) — ใช้ host ของ playlist", file=sys.stderr)
        mtext = ""
        STATE["seg_hosts"] = []

    resolutions = sorted(set(re.findall(r"RESOLUTION=(\d+x\d+)", mtext))) if mtext else []
    return {"master": master, "seg_hosts": STATE["seg_hosts"], "resolutions": resolutions}


def start_update(with_desc: bool) -> bool:
    """เริ่มอัปเดตรายการหนัง (crawl 77-hd.com) ใน background thread.
    คืน False ถ้ามี job กำลังรันอยู่แล้ว"""
    with UPDATE_LOCK:
        if UPDATE["running"]:
            return False
        UPDATE.update({"running": True, "phase": "starting", "page": 0,
                       "total_pages": 0, "movies": 0, "done": 0, "total": 0,
                       "count": 0, "error": None, "with_desc": with_desc})

    def on_progress(ev: dict) -> None:
        with UPDATE_LOCK:
            UPDATE.update(ev)

    def worker() -> None:
        try:
            n = lister.run_crawl(MOVIES_JSON, max_pages=0, delay=0.2,
                                 with_desc=with_desc, workers=10,
                                 on_progress=on_progress)
            with UPDATE_LOCK:
                UPDATE.update({"phase": "done", "count": n})
        except Exception as e:  # noqa: BLE001
            with UPDATE_LOCK:
                UPDATE.update({"phase": "error", "error": str(e)})
            print(f"อัปเดตรายการล้มเหลว: {e}", file=sys.stderr)
        finally:
            with UPDATE_LOCK:
                UPDATE["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def _safe_filename(name: str) -> str:
    """ทำชื่อไฟล์ให้ปลอดภัย (ตัดอักขระต้องห้าม แต่คงภาษาไทยไว้)"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] or "video"


def _job_dirname(source: str, master: str) -> str:
    """ชื่อโฟลเดอร์ย่อยต่อเรื่อง — แยกไฟล์ของแต่ละเรื่องออกจากกัน
    กันไฟล์ (และ tmp ระหว่างทางของ fetch_zmdb) ชนกันตอนโหลดหลายเรื่องพร้อมกัน
    ใช้ slug จาก URL หน้าเว็บ (คงที่แม้กด F5) เช่น .../toy-story-5-2026/ -> toy-story-5-2026"""
    slug = ""
    if source:
        path = urllib.parse.urlparse(source).path
        slug = urllib.parse.unquote(path).strip("/").split("/")[-1]
    if not slug and master:                      # ไม่มี source -> ใช้ id ของ hls
        m = re.search(r"/hls/([0-9A-Za-z]+)", master)
        slug = m.group(1) if m else ""
    return _safe_filename(slug) or "video"


def start_download(source: str, master: str, fmt: str, title: str,
                   height: str | None = None) -> tuple[bool, str, str]:
    """เริ่มดาวน์โหลด 1 เรื่องด้วย fetch_zmdb ใน background (รองรับหลายเรื่องพร้อมกัน)
    source = URL หน้าเว็บของเรื่อง (ใช้เป็นคีย์งาน คงที่แม้กด F5)
    master = master URL ของเรื่องนั้น (แต่ละแท็บส่งของตัวเองมา)
    height = ความสูงสูงสุด (เช่น '1080'); ไม่ระบุ = ตาม default ของ fetch_zmdb
    คืน (started, message, key)"""
    fmt = "mkv" if fmt == "mkv" else "mp4"
    h = str(height) if (height and str(height).isdigit()) else None
    key = source or "__default__"
    with DOWNLOAD_LOCK:
        job = DOWNLOADS.get(key)
        if job and job["running"]:
            return False, "กำลังดาวน์โหลดเรื่องนี้อยู่ กรุณารอให้เสร็จก่อน", key
        if not master:
            return False, "ยังไม่มีวิดีโอให้โหลด (เปิดเล่นวิดีโอก่อน)", key
        running = sum(1 for j in DOWNLOADS.values() if j["running"])
        if running >= MAX_DOWNLOADS:
            return (False,
                    f"ดาวน์โหลดพร้อมกันได้สูงสุด {MAX_DOWNLOADS} เรื่อง "
                    "กรุณารอให้บางเรื่องเสร็จก่อน", key)
        base = _safe_filename(title or "video")
        tag = f" [{int(h)}p]" if h else ""
        name = f"{base}{tag}.{fmt}"
        subdir = _job_dirname(source, master)     # แยกโฟลเดอร์ต่อเรื่อง กันไฟล์ชนกัน
        out_dir = DOWNLOAD_DIR / subdir
        rel = f"{subdir}/{name}"                  # path ที่ /file ใช้อ้างถึง (มีโฟลเดอร์ย่อย)
        DOWNLOADS[key] = {"running": True, "phase": "starting", "line": "",
                          "pct": None, "file": None, "error": None,
                          "fmt": fmt, "title": name, "master": master}

    cmd = [sys.executable, "-u", str(HERE / "fetch_zmdb.py"), master,
           "--format", fmt, "-o", name, "--out-dir", str(out_dir)]
    if h:
        cmd += ["--height", h]
    # แหล่ง 24hd (vdohls): ต้องใช้ referer เฉพาะ + ไม่มี content steering
    if is_vdohls_url(master):
        cmd += ["--referer", VDOHLS_REFERER, "--origin", "", "--no-steering"]

    def worker() -> None:
        job = DOWNLOADS[key]          # อ้างถึง dict เดิม (mutate ในล็อก)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
            # ใช้ readline() ตรง ๆ กัน read-ahead buffering ของ iterator ที่หน่วง progress
            for line in iter(proc.stderr.readline, ""):  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                with DOWNLOAD_LOCK:
                    m = re.search(r"\[([^\]]+)\]\s+(\d+)/(\d+)", line)
                    if m:
                        done, total = int(m.group(2)), int(m.group(3))
                        job["pct"] = round(done * 100 / total) if total else None
                        job["line"] = f"{m.group(1)} · {done}/{total}"
                        job["phase"] = "downloading"
                    elif re.search(r"\[([^\]]+)\]\s+\d+\s+ชิ้น", line):
                        lbl = re.search(r"\[([^\]]+)\]", line).group(1)
                        job.update({"phase": "downloading",
                                    "line": f"กำลังโหลด {lbl}...", "pct": None})
                    elif line.startswith("รวมด้วย ffmpeg"):
                        job.update({"phase": "muxing", "line": "กำลังรวมไฟล์ (ffmpeg)...", "pct": None})
                    elif "ดึง playlist" in line:
                        job.update({"phase": "preparing", "line": "กำลังเตรียม playlist..."})
                    else:
                        job["line"] = line[:180]
            rc = proc.wait()
            with DOWNLOAD_LOCK:
                if rc == 0 and (out_dir / name).exists():
                    sz = (out_dir / name).stat().st_size / 1024 / 1024
                    job.update({"phase": "done", "file": rel, "pct": 100,
                                "line": f"เสร็จแล้ว · {sz:.0f} MB"})
                else:
                    job.update({"phase": "error",
                                "error": f"ดาวน์โหลดล้มเหลว (exit {rc})"})
        except Exception as e:  # noqa: BLE001
            with DOWNLOAD_LOCK:
                job.update({"phase": "error", "error": str(e)})
        finally:
            with DOWNLOAD_LOCK:
                job["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True, "เริ่มดาวน์โหลดแล้ว", key


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # เงียบ log ปกติ
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # client ปิดการเชื่อมต่อไปแล้ว (เช่น hls.js ยกเลิก request ที่ไม่ใช้) — ปกติ ไม่ต้องทำอะไร
            pass

    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parts.query)
        try:
            if parts.path == "/":
                if STATE.get("browse"):
                    self._send(BROWSE_TEMPLATE.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(render_player(), "text/html; charset=utf-8")
                return

            if parts.path == "/player":
                self._send(render_player(), "text/html; charset=utf-8")
                return

            if parts.path == "/movies.json":
                movies = merged_movies()
                if not movies:
                    self._send(b"[]", "application/json; charset=utf-8", 404)
                else:
                    self._send(json.dumps(movies, ensure_ascii=False).encode(),
                               "application/json; charset=utf-8")
                return

            if parts.path == "/resolve":
                page = qs.get("url", [""])[0]
                title = qs.get("title", [""])[0]
                if not page:
                    self._send(b'{"ok":false,"error":"missing url"}',
                               "application/json; charset=utf-8", 400)
                    return
                # ---- 24hd: master อยู่ในหน้า embed (HTTP ธรรมดา) ไม่ต้อง Chromium/lock ----
                if is_24hd_page(page):
                    try:
                        STATE["source"] = page
                        STATE["title"] = title or None
                        STATE["description"] = qs.get("desc", [""])[0] or None
                        STATE["image"] = qs.get("image", [""])[0] or None
                        STATE["quality"] = qs.get("quality", [""])[0] or None
                        STATE["sound"] = qs.get("sound", [""])[0] or None
                        info = resolve_24hd(page)
                        meta = {
                            "title": STATE.get("title") or "",
                            "source": STATE.get("source") or "",
                            "description": STATE.get("description") or "",
                            "image": STATE.get("image") or "",
                            "quality": STATE.get("quality") or "",
                            "sound": STATE.get("sound") or "",
                        }
                        body = json.dumps({"ok": True, "meta": meta, **info},
                                          ensure_ascii=False)
                        self._send(body.encode(), "application/json; charset=utf-8")
                    except Exception as e:  # noqa: BLE001
                        body = json.dumps({"ok": False, "error": str(e)},
                                          ensure_ascii=False)
                        self._send(body.encode(), "application/json; charset=utf-8", 502)
                    return
                # กันยิงซ้อน: ถ้ากำลัง resolve เรื่องอื่นอยู่ (browser เปิดค้าง) ให้ปฏิเสธ
                # แทนที่จะเปิด Chromium อีกตัว (กันแรมหมด/เครื่องค้าง)
                if not RESOLVE_LOCK.acquire(blocking=False):
                    body = json.dumps(
                        {"ok": False,
                         "error": "กำลังเปิดอีกเรื่องอยู่ กรุณารอให้เรื่องก่อนหน้าเสร็จก่อน"},
                        ensure_ascii=False)
                    self._send(body.encode(), "application/json; charset=utf-8", 429)
                    return
                try:
                    STATE["source"] = page
                    STATE["title"] = title or None
                    STATE["description"] = qs.get("desc", [""])[0] or None
                    STATE["image"] = qs.get("image", [""])[0] or None
                    STATE["quality"] = qs.get("quality", [""])[0] or None
                    STATE["sound"] = qs.get("sound", [""])[0] or None
                    # เรื่องย่อจาก JSON เป็นพรีวิวที่ถูกตัด ('...') → ดึงฉบับเต็มจากหน้าเพจมาแทน
                    # คงส่วนหัว (คะแนน/แนวหนัง ก่อน 'เรื่องย่อ:') ไว้ให้ player ยังแยกข้อมูลได้
                    full_syn = fetch_full_synopsis(page)
                    if full_syn:
                        prefix = ""
                        old = STATE["description"] or ""
                        if "เรื่องย่อ" in old:
                            prefix = old.split("เรื่องย่อ")[0]
                        STATE["description"] = (
                            f"{prefix}เรื่องย่อ: {full_syn}" if prefix.strip() else full_syn)
                    info = resolve_master(page)
                    meta = {
                        "title": STATE.get("title") or "",
                        "source": STATE.get("source") or "",
                        "description": STATE.get("description") or "",
                        "image": STATE.get("image") or "",
                        "quality": STATE.get("quality") or "",
                        "sound": STATE.get("sound") or "",
                    }
                    body = json.dumps({"ok": True, "meta": meta, **info}, ensure_ascii=False)
                    self._send(body.encode(), "application/json; charset=utf-8")
                except Exception as e:  # noqa: BLE001
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                    self._send(body.encode(), "application/json; charset=utf-8", 502)
                finally:
                    RESOLVE_LOCK.release()
                return

            if parts.path == "/update":
                with_desc = qs.get("desc", ["0"])[0] == "1"
                started = start_update(with_desc)
                with UPDATE_LOCK:
                    snap = dict(UPDATE)
                body = json.dumps({"ok": True, "started": started, **snap},
                                  ensure_ascii=False)
                self._send(body.encode(), "application/json; charset=utf-8")
                return

            if parts.path == "/update-status":
                with UPDATE_LOCK:
                    snap = dict(UPDATE)
                self._send(json.dumps(snap, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return

            if parts.path == "/download":
                fmt = qs.get("fmt", ["mp4"])[0]
                height = qs.get("height", [""])[0]
                # แต่ละแท็บส่ง source (URL หน้าเว็บ) + master ของเรื่องตัวเองมา
                source = qs.get("source", [""])[0] or STATE.get("source") or ""
                master = qs.get("master", [""])[0] or STATE.get("master") or ""
                title = qs.get("title", [""])[0] or STATE.get("title") or "video"
                started, msg, key = start_download(source, master, fmt, title, height)
                with DOWNLOAD_LOCK:
                    snap = dict(DOWNLOADS.get(key, _idle_job()))
                body = json.dumps({"ok": started, "message": msg, "source": key, **snap},
                                  ensure_ascii=False)
                self._send(body.encode(), "application/json; charset=utf-8",
                           200 if started else 409)
                return

            if parts.path == "/download-status":
                source = qs.get("source", [""])[0] or STATE.get("source") or "__default__"
                with DOWNLOAD_LOCK:
                    if qs.get("all", [""])[0] == "1":
                        snap = {"jobs": {k: dict(v) for k, v in DOWNLOADS.items()}}
                    else:
                        snap = dict(DOWNLOADS.get(source, _idle_job()))
                        snap["source"] = source
                self._send(json.dumps(snap, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return

            if parts.path == "/file":
                raw = qs.get("name", [""])[0]
                # อนุญาตโฟลเดอร์ย่อยต่อเรื่อง (เช่น <เรื่อง>/หนัง.mp4) แต่กัน path traversal
                comps = [p for p in Path(raw).parts if p not in ("", ".", "..")]
                fp = DOWNLOAD_DIR.joinpath(*comps).resolve() if comps else DOWNLOAD_DIR
                safe = comps[-1] if comps else ""
                base = DOWNLOAD_DIR.resolve()
                if (not comps or not fp.is_file()
                        or (fp != base and base not in fp.parents)):
                    self._send(b"not found", "text/plain; charset=utf-8", 404)
                    return
                size = fp.stat().st_size
                fn_ascii = (safe.encode("ascii", "ignore").decode() or "video")
                fn_star = urllib.parse.quote(safe)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{fn_ascii}"; '
                                 f"filename*=UTF-8''{fn_star}")
                self.end_headers()
                with fp.open("rb") as fh:      # stream ทีละ chunk (ไฟล์อาจใหญ่หลาย GB)
                    try:
                        while True:
                            chunk = fh.read(1024 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass   # client ยกเลิกดาวน์โหลด — ปกติ
                return

            if parts.path == "/master.m3u8":
                # แต่ละแท็บ pin master ของตัวเองมาได้ (?m=...) กันชนกันเวลาเปิดหลายเรื่องพร้อมกัน
                master = qs.get("m", [""])[0] or STATE["master"]
                text = master_text(master)                   # referer/sig ตามแหล่ง
                body = rewrite_m3u8(text, master, master_pl=True)
                self._send(body.encode(), "application/vnd.apple.mpegurl")
                return

            if parts.path == "/p":
                u = b64d(qs["u"][0])
                seg = qs.get("seg", ["0"])[0] == "1"
                data = fetch_upstream(u, seg)
                if data[:7] == b"#EXTM3U":                   # media playlist -> rewrite
                    # จำว่าไดเรกทอรีนี้มาจาก media playlist ตัวไหน (ไว้ขอ token สดตอน segment หมดอายุ)
                    seg_dir = urllib.parse.urlparse(u).path.rpartition("/")[0]
                    with MEDIA_PL_LOCK:
                        MEDIA_PL[seg_dir] = u
                    body = rewrite_m3u8(data.decode("utf-8", "replace"), u, master_pl=False)
                    self._send(body.encode(), "application/vnd.apple.mpegurl")
                elif u.split("?")[0].endswith(".vtt"):
                    self._send(data, "text/vtt; charset=utf-8")
                else:
                    self._send(data, "application/octet-stream")
                return

            self._send(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # client หายไประหว่างตอบ (hls.js ยกเลิก/สลับ quality) — เงียบ ๆ ไม่ใช่ error จริง
            return
        except Exception as e:  # noqa: BLE001
            print(f"proxy error: {self.path} -> {e}", file=sys.stderr)  # log สั้น ไม่ใช่ traceback
            self._send(f"proxy error: {e}".encode(), "text/plain", 502)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="local proxy + player สำหรับเล่นวิดีโอ 77-hd/zmdb ในเบราว์เซอร์เอง")
    ap.add_argument("source", nargs="?",
                    help="URL หน้าหนัง 77-hd หรือ master URL ตรง ๆ "
                         "(ไม่ใส่ = เปิดโหมด browse รายการหนังจาก 77hd_movies.json)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="ค่าเริ่มต้น 127.0.0.1 (อย่าเปลี่ยนถ้าไม่จำเป็น)")
    ap.add_argument("--open", action="store_true", help="เปิดเบราว์เซอร์ให้อัตโนมัติ")
    ap.add_argument("--headful", action="store_true", help="[กรณีเป็น URL 77-hd] เปิด Chrome แบบเห็นหน้าต่างตอนหา master")
    ap.add_argument("--timeout", type=int, default=60, help="[กรณีเป็น URL 77-hd] วินาทีรอดัก master")
    args = ap.parse_args()

    if args.source:
        # ---- โหมดเล่นเรื่องเดียว: resolve master ทันที ----
        STATE["browse"] = False
        if is_master_source(args.source):
            print("ใช้ master URL ที่ให้มาโดยตรง", file=sys.stderr)
        else:
            print("หา master URL จากหน้า 77-hd (Playwright) ...", file=sys.stderr)
        info = resolve_master(args.source, headful=args.headful, timeout_s=args.timeout)
        print(f"  master: {info['master']}", file=sys.stderr)
        print(f"  CDN mirror: {info['seg_hosts']}", file=sys.stderr)
        if info["resolutions"]:
            print(f"  ความละเอียด: {', '.join(info['resolutions'])}", file=sys.stderr)
    else:
        # ---- โหมด browse: แสดงรายการหนังจาก 77hd_movies.json ----
        STATE["browse"] = True
        if not MOVIES_JSON.exists():
            print(f"เตือน: ไม่พบ {MOVIES_JSON.name} — รัน `python3 list_77hd.py` ก่อน "
                  f"เพื่อสร้างรายการหนัง", file=sys.stderr)
        else:
            try:
                n = len(json.loads(MOVIES_JSON.read_text(encoding="utf-8")))
                print(f"โหมด browse: โหลดรายการหนัง {n} เรื่องจาก {MOVIES_JSON.name}",
                      file=sys.stderr)
            except Exception:  # noqa: BLE001
                print(f"โหมด browse: ใช้ {MOVIES_JSON.name}", file=sys.stderr)

    url = f"http://{args.host}:{args.port}/"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    label = "รายการหนัง (browse)" if STATE["browse"] else "player"
    print(f"\n▶ เปิด {label} ที่: {url}", file=sys.stderr)
    print("  (bind localhost เท่านั้น — อย่า expose ออกเน็ต)  Ctrl+C เพื่อหยุด\n", file=sys.stderr)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nหยุด server", file=sys.stderr)
        srv.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"ผิดพลาด: {e}", file=sys.stderr)
        sys.exit(1)
