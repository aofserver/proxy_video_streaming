#!/usr/bin/env python3
"""
list_77hd.py — ไล่เก็บ URL หนัง/ซีรีย์ทั้งหมดจาก 77-hd.com (ทุกหน้า) แล้ว dump เป็น JSON

หลักการ:
  หน้า listing อยู่ที่ https://77-hd.com/ (หน้า 1) และ https://77-hd.com/page/N/
  แต่ละหน้ามีลิงก์หนังแบบ https://77-hd.com/<slug>/ (ไม่ใช่ /category/, /page/, /tag/ ฯลฯ)
  การ์ดหนังจะมี <a> ซ้อนกับรูป + ชื่อเรื่อง — ดึง href + ข้อความชื่อจาก title/alt/ข้อความในลิงก์
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from download_video_77hd import http_get  # reuse: HTTP GET + desktop UA/headers

BASE = "https://77-hd.com"

# path แรกที่ไม่ใช่หน้าหนัง (กันไม่ให้เก็บลิงก์เมนู/หมวด/เพจ)
NON_MOVIE_PREFIX = (
    "category", "page", "tag", "author", "wp-", "genre",
    "feed", "search", "actor", "director", "country", "year",
)


def page_url(n: int) -> str:
    return f"{BASE}/" if n == 1 else f"{BASE}/page/{n}/"


def detect_last_page(html_txt: str) -> int:
    """หาเลขหน้าสูงสุดจากลิงก์ pagination /page/N/ ในหน้าแรก (คืน 1 ถ้าหาไม่เจอ)"""
    nums = [int(m) for m in re.findall(r'/page/(\d+)/', html_txt)]
    return max(nums) if nums else 1


def clean_title(raw: str) -> str:
    t = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", t).strip()


def is_movie_href(href: str) -> str | None:
    """คืน absolute movie URL ถ้า href เป็นหน้าหนัง; ไม่งั้น None"""
    if not href:
        return None
    u = urllib.parse.urljoin(BASE + "/", href)
    p = urllib.parse.urlparse(u)
    if p.netloc.replace("www.", "") != "77-hd.com":
        return None
    seg = [s for s in p.path.split("/") if s]
    if len(seg) != 1:                       # หน้าหนัง = /slug/ (segment เดียว)
        return None
    if seg[0].lower() in NON_MOVIE_PREFIX:
        return None
    return f"{BASE}/{seg[0]}/"


# <a href="...">...ชื่อ...</a>  (จับ href + เนื้อในลิงก์เพื่อเดาชื่อ/รูป)
A_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                  re.I | re.S)
TITLE_ATTR_RE = re.compile(r'(?:title|alt)=["\']([^"\']+)["\']', re.I)
IMG_SRC_RE = re.compile(
    r'<img\b[^>]*?\b(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', re.I)

# บล็อก meta ของการ์ด: <span tag-sound>เสียง/ซับ</span>[<span tag-quality>คุณภาพ</span>]
# ตามด้วยลิงก์ชื่อเรื่อง <a href="URL"><p class="meta-post-title">
# ใช้จับ "พากย์ไทย / ซับไทย / ไทยโรง / 4K / HD" ผูกกับ URL หนังของการ์ดนั้น
CARD_META_RE = re.compile(
    r'tag-sound["\']>([^<]*)</span>'
    r'(?:\s*<span[^>]*tag-quality[^>]*>([^<]*)</span>)?'
    r'.*?<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>\s*<p[^>]*meta-post-title',
    re.I | re.S)


def extract_img(inner: str) -> str:
    """ดึง URL รูปจริงจากในลิงก์ (ข้าม placeholder data:image)"""
    for m in IMG_SRC_RE.finditer(inner):
        src = m.group(1).strip()
        if src and not src.startswith("data:"):
            return src
    return ""


def parse_page(html_txt: str) -> dict[str, dict]:
    """คืน {movie_url: {"title":.., "image":.., "sound":.., "quality":..}} จาก HTML หน้า listing"""
    found: dict[str, dict] = {}
    for href, inner in A_RE.findall(html_txt):
        murl = is_movie_href(href)
        if not murl:
            continue
        title = clean_title(inner)
        if not title:                       # ลิงก์รูป -> เอา title/alt แทน
            m = TITLE_ATTR_RE.search(inner)
            title = clean_title(m.group(1)) if m else ""
        image = extract_img(inner)
        rec = found.setdefault(murl, {"title": "", "image": "", "sound": "", "quality": ""})
        # เก็บชื่อที่ยาวสุด (การ์ดเดียวกันมีทั้งลิงก์รูปเปล่าและลิงก์ชื่อ)
        if len(title) > len(rec["title"]):
            rec["title"] = title
        if image and not rec["image"]:
            rec["image"] = image
    # ผูก tag-sound / tag-quality เข้ากับ URL หนังของแต่ละการ์ด
    for sound, quality, href in CARD_META_RE.findall(html_txt):
        murl = is_movie_href(href)
        if not murl:
            continue
        rec = found.setdefault(murl, {"title": "", "image": "", "sound": "", "quality": ""})
        s = clean_title(sound)
        q = clean_title(quality)
        if s and not rec.get("sound"):
            rec["sound"] = s
        if q and not rec.get("quality"):
            rec["quality"] = q
    return found


def crawl(max_pages: int, delay: float, stop_empty: int = 2,
          on_progress=None) -> dict[str, dict]:
    movies: dict[str, dict] = {}
    empty_streak = 0
    n = 0
    while True:
        n += 1
        if max_pages > 0 and n > max_pages:
            break
        url = page_url(n)
        try:
            html_txt = http_get(url, referer=BASE + "/").decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            print(f"  หน้า {n}: โหลดไม่ได้ ({e}) — ข้าม", file=sys.stderr)
            empty_streak += 1
            if empty_streak >= stop_empty:
                print("  หยุด: หลายหน้าติดกันโหลดไม่ได้", file=sys.stderr)
                break
            continue
        # auto-detect จำนวนหน้าจากหน้าแรก ถ้าไม่ได้กำหนด max_pages มา
        if n == 1 and max_pages <= 0:
            max_pages = detect_last_page(html_txt)
            print(f"  ตรวจพบทั้งหมด {max_pages} หน้า (auto)", file=sys.stderr)
        page_movies = parse_page(html_txt)
        new = [k for k in page_movies if k not in movies]
        for u, rec in page_movies.items():
            cur = movies.setdefault(u, {"title": "", "image": "", "sound": "", "quality": ""})
            if len(rec["title"]) > len(cur["title"]):
                cur["title"] = rec["title"]
            if rec["image"] and not cur["image"]:
                cur["image"] = rec["image"]
            if rec.get("sound") and not cur.get("sound"):
                cur["sound"] = rec["sound"]
            if rec.get("quality") and not cur.get("quality"):
                cur["quality"] = rec["quality"]
        total_txt = max_pages if max_pages > 0 else "?"
        print(f"  หน้า {n}/{total_txt}: เจอ {len(page_movies)} เรื่อง "
              f"(ใหม่ {len(new)}) — รวม {len(movies)}", file=sys.stderr)
        if on_progress:
            on_progress({"phase": "pages", "page": n,
                         "total_pages": max_pages if max_pages > 0 else 0,
                         "movies": len(movies)})
        if not page_movies:
            empty_streak += 1
            if empty_streak >= stop_empty:
                print(f"  หยุด: หน้า {n} ไม่มีหนังแล้ว (จบหน้าจริง)", file=sys.stderr)
                break
        else:
            empty_streak = 0
        if delay:
            time.sleep(delay)
    return movies


# --------------------------------------------------------------------------- #
# ดึง description จากหน้ารายละเอียด (meta description / og:description)
# --------------------------------------------------------------------------- #
DESC_RE = re.compile(
    r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])'
    r'[^>]*\bcontent=["\']([^"\']*)["\']', re.I)
OGIMG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]*\bcontent=["\']([^"\']+)["\']', re.I)


def fetch_detail(url: str) -> tuple[str, str]:
    """คืน (description, og_image) จากหน้ารายละเอียด"""
    html_txt = http_get(url, referer=BASE + "/").decode("utf-8", "replace")
    m = DESC_RE.search(html_txt)
    desc = clean_title(m.group(1)) if m else ""
    mi = OGIMG_RE.search(html_txt)
    ogimg = mi.group(1).strip() if mi else ""
    return desc, ogimg


def add_descriptions(movies: dict[str, dict], workers: int, delay: float,
                     on_progress=None) -> None:
    """เติม description ให้ทุกเรื่อง (ยิงหน้ารายละเอียดแบบขนาน)"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    urls = list(movies)
    total = len(urls)
    print(f"\nดึง description จากหน้ารายละเอียด {total} เรื่อง "
          f"(workers={workers}) ...", file=sys.stderr)
    done = 0

    def work(u: str):
        try:
            return u, fetch_detail(u)
        except Exception as e:  # noqa: BLE001
            return u, (f"[ดึงไม่ได้: {e}]", "")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, u) for u in urls]
        for f in as_completed(futs):
            u, (desc, ogimg) = f.result()
            movies[u]["description"] = desc
            if ogimg and not movies[u].get("image"):
                movies[u]["image"] = ogimg
            done += 1
            if done % 100 == 0 or done == total:
                print(f"  {done}/{total}", file=sys.stderr)
                if on_progress:
                    on_progress({"phase": "desc", "done": done, "total": total})


def movies_to_list(movies: dict[str, dict]) -> list[dict]:
    return [
        {
            "title": rec.get("title", ""),
            "url": u,
            "image": rec.get("image", ""),
            "sound": rec.get("sound", ""),
            "quality": rec.get("quality", ""),
            "description": ("" if rec.get("description", "").startswith("[ดึงไม่ได้")
                            else rec.get("description", "")),
        }
        for u, rec in sorted(movies.items())
    ]


def run_crawl(out_path, *, max_pages: int = 0, delay: float = 0.3,
              with_desc: bool = True, workers: int = 10, on_progress=None) -> int:
    """ไล่เก็บหนังทั้งหมด (+description ถ้า with_desc) แล้วเขียนไฟล์ JSON; คืนจำนวนเรื่อง
    ใช้ได้ทั้งจาก CLI และเรียกจาก tools_77hd.py (ปุ่มอัปเดต)"""
    movies = crawl(max_pages, delay, on_progress=on_progress)
    if with_desc:
        add_descriptions(movies, workers, delay, on_progress=on_progress)
    out_list = movies_to_list(movies)
    Path(out_path).write_text(
        json.dumps(out_list, ensure_ascii=False, indent=2), encoding="utf-8")
    if on_progress:
        on_progress({"phase": "done", "count": len(out_list)})
    return len(out_list)


def main() -> int:
    ap = argparse.ArgumentParser(description="เก็บ URL หนังทั้งหมดจาก 77-hd.com เป็น JSON")
    ap.add_argument("-o", "--out", default="77hd_movies.json", help="ไฟล์ JSON ปลายทาง")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="จำนวนหน้าสูงสุด (0 = auto-detect จาก pagination, ค่าเริ่มต้น)")
    ap.add_argument("--delay", type=float, default=0.4, help="หน่วงเวลาต่อหน้า (วินาที)")
    ap.add_argument("--no-desc", action="store_true",
                    help="ไม่ต้องดึง description (เร็วขึ้นมาก ไม่ยิงหน้ารายละเอียด)")
    ap.add_argument("--workers", type=int, default=8,
                    help="จำนวน thread ดึง description พร้อมกัน (ค่าเริ่มต้น 8)")
    args = ap.parse_args()

    tgt = "auto-detect" if args.max_pages <= 0 else f"สูงสุด {args.max_pages} หน้า"
    print(f"เริ่มไล่เก็บหนังจาก {BASE} ({tgt}) ...", file=sys.stderr)
    n = run_crawl(args.out, max_pages=args.max_pages, delay=args.delay,
                  with_desc=not args.no_desc, workers=args.workers)
    print(f"\n✔ เสร็จ: {n} เรื่อง → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
