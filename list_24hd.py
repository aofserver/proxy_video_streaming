#!/usr/bin/env python3
"""
list_24hd.py — ไล่เก็บหนัง/ซีรีย์ทั้งหมดจาก 24hd.media เก็บลง SQLite
พร้อม "Master URL" (HLS playlist ของ vdohls — ไม่มี auth/token)

ผลลัพธ์เก็บใน movies.db (ตาราง movies, source='24hd') ผ่าน movies_db.py

หลักการ:
  1) หน้า listing อยู่ตาม "category" (เช่น /category/inter-movie/, /category/netflix/ ...)
     ไล่หน้าย่อยด้วย pattern มาตรฐาน WordPress: <CAT>/page/N/
  2) การ์ดหนังลิงก์ไปหน้ารายละเอียดแบบ https://www.24hd.media/<slug>  (segment เดียว)
  3) หน้ารายละเอียดมีปุ่ม "ตัวเล่นหลัก" → iframe/ลิงก์ embed
        https://player77hdfree.xyz/embed/<vdoId>
  4) หน้า embed ฝัง JSON config:
        "vdoId":"<ID>", "node":{"static":"static.vdohls.com","playlist":"vdohls.com"}
     → Master URL = https://<node.playlist>/<vdoId>/playlist.m3u8
       (ตรงกับตัวอย่าง /box/zootopia-2-y2025 → https://vdohls.com/2-0JPeW1_fceq/playlist.m3u8)
"""
from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from download_video_77hd import http_get  # reuse: HTTP GET + desktop UA/headers
import movies_db

BASE = "https://www.24hd.media"
HOST = "24hd.media"

# หมวดหมู่หลักที่ครอบคลุมหนัง/ซีรีย์ทั้งเว็บ (จากเมนู "ประเภท" ในหน้าแรก)
# ใช้ path ที่ percent-encode แล้ว (ภาษาไทย) ตามที่เว็บใช้จริง
DEFAULT_CATEGORIES = [
    "inter-movie",                 # หนังฝรั่ง (ใหญ่สุด ~12k)
    "asia-movie",                  # หนังเอเชีย
    "netflix",                     # NETFLIX
    "thai-movie",                  # หนังไทย
    "anime",                       # อนิเมะ
    "animation-%e0%b8%81%e0%b8%b2%e0%b8%a3%e0%b9%8c%e0%b8%95%e0%b8%b9%e0%b8%99",   # หนังการ์ตูน
    "%e0%b8%ab%e0%b8%99%e0%b8%b1%e0%b8%87%e0%b8%88%e0%b8%b5%e0%b8%99",             # หนังจีน
    "%e0%b8%ab%e0%b8%99%e0%b8%b1%e0%b8%87%e0%b9%80%e0%b8%81%e0%b8%b2%e0%b8%ab%e0%b8%a5%e0%b8%b5",  # หนังเกาหลี
    "%e0%b8%94%e0%b8%b9%e0%b8%8b%e0%b8%b5%e0%b8%a3%e0%b8%b5%e0%b9%88%e0%b8%a2%e0%b9%8c",           # ดูซีรี่ย์
    "%e0%b8%ab%e0%b8%99%e0%b8%b1%e0%b8%87-soundtrack",                            # หนัง SoundTrack
    "%e0%b8%ab%e0%b8%99%e0%b8%b1%e0%b8%87%e0%b9%80%e0%b8%a3%e0%b8%97-r18",         # หนัง 18+
    "%e0%b8%8b%e0%b8%b5%e0%b8%a3%e0%b8%b5%e0%b9%88%e0%b8%a2%e0%b9%8c%e0%b9%80%e0%b8%81%e0%b8%b2%e0%b8%ab%e0%b8%a5%e0%b8%b5",  # ซีรี่ย์เกาหลี
    "%e0%b8%8b%e0%b8%b5%e0%b8%a3%e0%b8%b5%e0%b9%88%e0%b8%a2%e0%b9%8c%e0%b8%88%e0%b8%b5%e0%b8%99",  # ซีรี่ย์จีน
]

# path ส่วนแรกที่ "ไม่ใช่" หน้าหนัง (กันไม่ให้เก็บลิงก์เมนู/หมวด/ระบบ)
NON_MOVIE_PREFIX = {
    "category", "page", "tag", "author", "wp-json", "wp-admin", "wp-content",
    "wp-includes", "feed", "search", "box", "actor", "director", "country",
    "year", "genre", "comments", "cdn-cgi",
}

# ปุ่มเล่น/iframe → embed URL (host อะไรก็ได้ ตราบใดที่ path เป็น /embed/<id>)
EMBED_RE = re.compile(r'https?://[a-z0-9.\-]+/embed/([A-Za-z0-9_\-]+)', re.I)
# JSON config ในหน้า embed
VDOID_RE = re.compile(r'"vdoId"\s*:\s*"([^"]+)"')
PLAYLIST_NODE_RE = re.compile(r'"playlist"\s*:\s*"([^"]+)"')

A_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
TITLE_ATTR_RE = re.compile(r'(?:title|alt)=["\']([^"\']+)["\']', re.I)
IMG_SRC_RE = re.compile(
    r'<img\b[^>]*?\b(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', re.I)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean_text(raw: str) -> str:
    t = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", t).strip()


# ป้าย meta ที่การ์ดหนัง 24hd แปะไว้หน้าชื่อจริง:
#   "<rating> <คุณภาพ> <ภาษา/โหมด> <ชื่อจริง>"
# เช่น "8 HD พากย์ไทย ...", "5.4 HD ซับไทย ...", "6.9 ZOOM เสียงโรง ..."
# ตัด rating ออก *เฉพาะ* เมื่อตามด้วยคำคุณภาพ (กันตัดเลขที่เป็นส่วนของชื่อจริง)
_META_PREFIX_RE = re.compile(
    r"^\s*"
    r"(?:\d+(?:\.\d+)?\s+)?"                                     # rating (optional)
    r"(?:(?:HD|FHD|4K|CAM|SD|ZOOM)\s*)"                          # คุณภาพ (บังคับมี)
    r"(?:(?:ซับไทย|พากย์ไทย|บรรยายไทย|SoundTrack|ซาวด์แทร็ก|"
    r"เสียงโรง|มาสเตอร์|Master)\s*)?",                            # ภาษา/โหมด (optional)
    re.I,
)


def strip_meta_prefix(title: str) -> str:
    """ลบป้าย rating/คุณภาพ/ภาษา ที่ติดหน้าชื่อเรื่อง (คืนชื่อจริงล้วน)"""
    if not title:
        return title
    cleaned = _META_PREFIX_RE.sub("", title, count=1).strip()
    return cleaned or title            # กันเผลอลบจนว่าง


def cat_page_url(cat: str, n: int) -> str:
    base = f"{BASE}/category/{cat}/"
    return base if n == 1 else f"{base}page/{n}/"


def detect_last_page(html_txt: str, cat: str) -> int:
    """หาเลขหน้าสูงสุดจากลิงก์ pagination <CAT>/page/N/ (คืน 1 ถ้าไม่เจอ)"""
    pat = re.compile(re.escape(f"/category/{cat}/") + r"page/(\d+)/")
    nums = [int(m) for m in pat.findall(html_txt)]
    return max(nums) if nums else 1


def is_movie_href(href: str) -> str | None:
    """คืน absolute movie URL (แบบ /<slug>) ถ้า href เป็นหน้าหนัง; ไม่งั้น None"""
    if not href:
        return None
    u = urllib.parse.urljoin(BASE + "/", href)
    p = urllib.parse.urlparse(u)
    if p.netloc.replace("www.", "") != HOST:
        return None
    seg = [s for s in p.path.split("/") if s]
    # หน้าหนัง = /box/<slug> (alias) หรือ /<slug> (segment เดียว)
    if len(seg) == 2 and seg[0].lower() == "box":
        slug = seg[1]
    elif len(seg) == 1:
        slug = seg[0]
    else:
        return None
    if slug.lower() in NON_MOVIE_PREFIX:
        return None
    return f"{BASE}/{slug}"


def extract_img(inner: str) -> str:
    for m in IMG_SRC_RE.finditer(inner):
        src = m.group(1).strip()
        if src and not src.startswith("data:"):
            return src
    return ""


def parse_listing(html_txt: str) -> dict[str, dict]:
    """คืน {movie_url: {title, image}} จาก HTML หน้า listing (category)"""
    found: dict[str, dict] = {}
    for href, inner in A_RE.findall(html_txt):
        murl = is_movie_href(href)
        if not murl:
            continue
        title = clean_text(inner)
        if not title:                       # ลิงก์รูปเปล่า → เอา title/alt แทน
            m = TITLE_ATTR_RE.search(inner)
            title = clean_text(m.group(1)) if m else ""
        image = extract_img(inner)
        title = strip_meta_prefix(title)
        rec = found.setdefault(murl, {"title": "", "image": ""})
        if len(title) > len(rec["title"]):
            rec["title"] = title
        if image and not rec["image"]:
            rec["image"] = image
    return found


# --------------------------------------------------------------------------- #
# ชั้น master URL: หน้าหนัง → embed → vdohls playlist.m3u8
# --------------------------------------------------------------------------- #
DESC_RE = re.compile(
    r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])'
    r'[^>]*\bcontent=["\']([^"\']*)["\']', re.I)
OGIMG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]*\bcontent=["\']([^"\']+)["\']', re.I)


def _box_variant(movie_url: str) -> str | None:
    """คืน URL แบบ /box/<slug> ถ้า movie_url เป็น /<slug> (บาง slug redirect หน้าแรก
    แต่ /box/<slug> เข้าหน้าหนังได้) — คืน None ถ้าเป็น /box/ อยู่แล้ว"""
    p = urllib.parse.urlparse(movie_url)
    seg = [s for s in p.path.split("/") if s]
    if not seg or seg[0].lower() == "box":
        return None
    return f"{BASE}/box/{seg[-1]}"


def _resolve_once(movie_url: str, out: dict, timeout: int) -> bool:
    """แกะหน้าหนัง 1 URL → เติม embed/master/description/image ลง out
    คืน True ถ้าเจอ embed (มีตัวเล่น)"""
    page = http_get(movie_url, referer=BASE + "/", timeout=timeout).decode(
        "utf-8", "replace")
    m = DESC_RE.search(page)
    if m and not out.get("description"):
        out["description"] = clean_text(m.group(1))
    mi = OGIMG_RE.search(page)
    if mi and not out.get("image"):
        out["image"] = mi.group(1).strip()

    em = EMBED_RE.search(page)
    if not em:
        return False
    embed_url = em.group(0)
    out["embed"] = embed_url
    embed_html = http_get(embed_url, referer=BASE + "/", timeout=timeout).decode(
        "utf-8", "replace")
    vid = VDOID_RE.search(embed_html)
    node = PLAYLIST_NODE_RE.search(embed_html)
    if vid:
        playlist_host = node.group(1) if node else "vdohls.com"
        out["master"] = f"https://{playlist_host}/{vid.group(1)}/playlist.m3u8"
    else:
        mm = re.search(r'https?://[a-z0-9.\-]+/[^"\']+/playlist\.m3u8', embed_html, re.I)
        if mm:
            out["master"] = mm.group(0)
    return True


def resolve_master(movie_url: str, timeout: int = 30) -> dict:
    """คืน {embed, master, description, image} ของหนังหนึ่งเรื่อง

    - master: https://<node.playlist>/<vdoId>/playlist.m3u8  (ว่างถ้าหาไม่เจอ)
    - ถ้า URL แบบ /<slug> ไม่เจอตัวเล่น (บาง slug redirect หน้าแรก) จะลอง /box/<slug> ให้
    """
    out = {"embed": "", "master": "", "description": "", "image": ""}
    try:
        found = _resolve_once(movie_url, out, timeout)
    except Exception:  # noqa: BLE001
        found = False
    if not found:
        box = _box_variant(movie_url)
        if box:
            try:
                _resolve_once(box, out, timeout)
            except Exception:  # noqa: BLE001
                pass
    return out


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def crawl_listing(categories: list[str], max_pages: int, delay: float,
                  stop_empty: int = 2, on_progress=None) -> dict[str, dict]:
    """ไล่ทุก category → คืน {movie_url: {title, image}} (dedupe ข้ามหมวด)"""
    movies: dict[str, dict] = {}
    for cat in categories:
        empty_streak = 0
        last_page = max_pages
        n = 0
        print(f"\n[category] {urllib.parse.unquote(cat)}", file=sys.stderr)
        while True:
            n += 1
            if last_page > 0 and n > last_page:
                break
            url = cat_page_url(cat, n)
            try:
                html_txt = http_get(url, referer=BASE + "/").decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                print(f"  หน้า {n}: โหลดไม่ได้ ({e}) — ข้าม", file=sys.stderr)
                empty_streak += 1
                if empty_streak >= stop_empty:
                    break
                continue
            if n == 1 and max_pages <= 0:
                last_page = detect_last_page(html_txt, cat)
                print(f"  ตรวจพบ {last_page} หน้า (auto)", file=sys.stderr)
            page_movies = parse_listing(html_txt)
            new = 0
            for u, rec in page_movies.items():
                cur = movies.setdefault(u, {"title": "", "image": ""})
                if u not in movies or not cur["title"]:
                    new += 1
                if len(rec["title"]) > len(cur["title"]):
                    cur["title"] = rec["title"]
                if rec["image"] and not cur["image"]:
                    cur["image"] = rec["image"]
            total_txt = last_page if last_page > 0 else "?"
            print(f"  หน้า {n}/{total_txt}: เจอ {len(page_movies)} — รวมทั้งหมด {len(movies)}",
                  file=sys.stderr)
            if on_progress:
                on_progress({"phase": "pages", "category": cat, "page": n,
                             "movies": len(movies)})
            if not page_movies:
                empty_streak += 1
                if empty_streak >= stop_empty:
                    break
            else:
                empty_streak = 0
            if delay:
                time.sleep(delay)
    return movies


def resolve_all(movies: dict[str, dict], workers: int,
                on_progress=None) -> None:
    """เติม embed/master/description ให้ทุกเรื่อง (ยิงหน้าหนัง+embed แบบขนาน)"""
    urls = list(movies)
    total = len(urls)
    print(f"\nหา Master URL จากหน้าหนัง {total} เรื่อง (workers={workers}) ...",
          file=sys.stderr)
    done = 0
    ok = 0

    def work(u: str):
        try:
            return u, resolve_master(u)
        except Exception:  # noqa: BLE001
            # ดึงไม่ได้ -> เว้นว่าง (upsert จะคงค่าเดิมไว้ ไม่ลบ master/description ทิ้ง)
            return u, {"embed": "", "master": "", "description": "", "image": ""}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, u) for u in urls]
        for f in as_completed(futs):
            u, info = f.result()
            movies[u]["embed"] = info.get("embed", "")
            movies[u]["master"] = info.get("master", "")
            movies[u]["description"] = info.get("description", "")
            if info.get("image") and not movies[u].get("image"):
                movies[u]["image"] = info["image"]
            done += 1
            if movies[u]["master"]:
                ok += 1
            if done % 50 == 0 or done == total:
                print(f"  {done}/{total} (มี master {ok})", file=sys.stderr)
                if on_progress:
                    on_progress({"phase": "master", "done": done, "total": total,
                                 "with_master": ok})


def run_crawl(*, categories: list[str] | None = None,
              max_pages: int = 0, delay: float = 0.3, with_master: bool = True,
              workers: int = 10, on_progress=None) -> int:
    """ไล่เก็บหนังทั้งหมด (+master ถ้า with_master) แล้ว upsert ลง movies.db; คืนจำนวนเรื่อง"""
    cats = categories or DEFAULT_CATEGORIES
    movies = crawl_listing(cats, max_pages, delay, on_progress=on_progress)
    if with_master:
        resolve_all(movies, workers, on_progress=on_progress)
    n = movies_db.upsert("24hd", movies)
    if on_progress:
        on_progress({"phase": "done", "count": n})
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"เก็บรายชื่อหนังจาก 24hd.media ลง {movies_db.DB_PATH.name} "
                    f"พร้อม Master URL (vdohls)")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="จำนวนหน้าต่อ category (0 = auto-detect, ค่าเริ่มต้น)")
    ap.add_argument("--delay", type=float, default=0.3, help="หน่วงเวลาต่อหน้า (วินาที)")
    ap.add_argument("--no-master", action="store_true",
                    help="ไม่ต้องหา Master URL (เร็วขึ้น ไม่ยิงหน้าหนัง/embed)")
    ap.add_argument("--workers", type=int, default=10,
                    help="จำนวน thread หา master พร้อมกัน (ค่าเริ่มต้น 10)")
    ap.add_argument("--categories", nargs="*", default=None,
                    help="ระบุ category slug เอง (ไม่ใส่ = ใช้ชุดมาตรฐานทั้งหมด)")
    args = ap.parse_args()

    tgt = "auto-detect" if args.max_pages <= 0 else f"สูงสุด {args.max_pages} หน้า/หมวด"
    print(f"เริ่มไล่เก็บหนังจาก {BASE} ({tgt}) ...", file=sys.stderr)
    n = run_crawl(categories=args.categories, max_pages=args.max_pages,
                  delay=args.delay, with_master=not args.no_master,
                  workers=args.workers)
    print(f"\n✔ เสร็จ: {n} เรื่อง → {movies_db.DB_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
