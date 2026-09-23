#!/usr/bin/env python3
"""
download_video_77hd.py — รับลิงก์หน้าหนังของ 77-hd.com แล้วหา master URL ให้อัตโนมัติ
                          (แทน Video DownloadHelper) จากนั้นต่อให้ fetch_zmdb.py โหลด

ทำไมต้องมีสคริปต์นี้:
  77-hd.com ฝัง player ผ่าน iframe ของ zmdb.net โดย master URL (ที่มี token sig/exp)
  ไม่ได้อยู่ในหน้า HTML ตรง ๆ ต้องไล่ผ่านหลายชั้น:

    77-hd.com/<slug>/
       └─ iframe → zmdb.net/embed?id=<embedId>&type=<movie|tv>
            ├─ GET /api/embed/bootstrap?id=<embedId>   → linkToken + ข้อมูลเรื่อง
            └─ GET /api/embed/links (Bearer linkToken) → embedUrl (streamXXX.com/play/<videoId>)
                 └─ streamXXX.com อยู่หลัง Cloudflare + auth  ← ต้องใช้เบราว์เซอร์จริง
                      → เปิดด้วย Playwright (headless Chrome) แล้วดัก request `_master`

  3 ชั้นแรกยิงด้วย urllib (stdlib) ได้เลย ชั้นสุดท้ายต้องมี browser context จริง
  จึงใช้ Playwright เฉพาะขั้นคาย master URL

ติดตั้ง Playwright ครั้งเดียว:
  .venv/bin/pip install playwright
  .venv/bin/playwright install chromium

ตัวอย่าง:
  # แค่หา master URL มาดู
  ./download_video_77hd.py "https://77-hd.com/zootopia-2-2025/" --print-master

  # ดูว่ามีเสียง/เซิร์ฟเวอร์อะไรบ้าง
  ./download_video_77hd.py "https://77-hd.com/zootopia-2-2025/" --list

  # หา master แล้วโหลดเป็น mkv (ต่อ args ให้ fetch_zmdb.py)
  ./download_video_77hd.py "https://77-hd.com/zootopia-2-2025/" \
      --format mkv --audio-langs th,en --sub-langs th,en --default-audio th

  # โหลดลงไดรฟ์ภายนอกแทนโฟลเดอร์ downloads/ ในโปรเจกต์
  ./download_video_77hd.py "https://77-hd.com/zootopia-2-2025/" \
      --format mkv --output-dir "/Volumes/Aofserver Ext"


"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FETCH = HERE / "fetch_zmdb.py"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# helper: HTTP GET ด้วย urllib (stdlib)
# --------------------------------------------------------------------------- #
def http_get(url: str, referer: str | None = None,
             bearer: str | None = None, timeout: int = 30) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "en-US,en;q=0.9,th;q=0.8"}
    if referer:
        headers["Referer"] = referer
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str, **kw) -> dict:
    return json.loads(http_get(url, **kw).decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# ชั้น 1: หน้า 77-hd.com → embedId + type จาก iframe ของ zmdb
# --------------------------------------------------------------------------- #
def resolve_embed(page_url: str) -> tuple[str, str]:
    """คืน (embedId, type) จาก iframe zmdb ในหน้าเว็บ"""
    page = http_get(page_url, referer=page_url).decode("utf-8", "replace")
    # src อาจมี &#038; หรือ &amp; คั่น
    m = re.search(r'zmdb\.net/embed\?id=(\d+)[^"\']*?type=([a-z]+)', page, re.I)
    if not m:
        m = re.search(r'zmdb\.net/embed\?id=(\d+)', page, re.I)
        if not m:
            raise RuntimeError("หา iframe zmdb ในหน้านี้ไม่เจอ "
                               "(หน้าอาจเปลี่ยนโครงสร้าง หรือไม่ใช่หน้าหนัง)")
        return m.group(1), "movie"
    return m.group(1), m.group(2)


# --------------------------------------------------------------------------- #
# ชั้น 2-3: zmdb API → linkToken → playerEmbedLinks (embedUrl)
# --------------------------------------------------------------------------- #
def get_bootstrap(embed_id: str, mtype: str) -> dict:
    ref = f"https://zmdb.net/embed?id={embed_id}&type={mtype}"
    url = f"https://zmdb.net/api/embed/bootstrap?id={embed_id}&type={mtype}"
    return get_json(url, referer=ref)


def get_links(embed_id: str, mtype: str, link_token: str) -> list[dict]:
    ref = f"https://zmdb.net/embed?id={embed_id}&type={mtype}"
    url = f"https://zmdb.net/api/embed/links?id={embed_id}&type={mtype}"
    data = get_json(url, referer=ref, bearer=link_token)
    if not data.get("success"):
        raise RuntimeError(f"/api/embed/links ไม่สำเร็จ: {data.get('error')}")
    return data.get("playerEmbedLinks", [])


# สคริปต์ซ่อนร่องรอย automation — zmdb embed จะ redirect ไป decoy (baidu)
# ถ้าตรวจเจอ headless/webdriver หรือถูกเปิดนอก iframe ที่อนุญาต
STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome={runtime:{}};
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
Object.defineProperty(navigator,'languages',{get:()=>['th-TH','th','en-US','en']});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
"""


# --------------------------------------------------------------------------- #
# ชั้น 4: เปิด "หน้า 77-hd จริง" ด้วย Playwright แล้วดัก request ที่เป็น master
#
# สำคัญ: ต้องโหลดหน้า 77-hd โดยตรง ไม่ใช่ stream037/zmdb-embed แยกเดี่ยว ๆ
#   - stream037/play เปิดตรง ๆ → 403 "cannot be embedded from unauthorized sources"
#   - zmdb/embed เปิดเป็น top-level → ตรวจว่าไม่ได้อยู่ใน iframe ที่อนุญาต → redirect baidu
#   ต้องให้ลำดับ iframe ถูกต้อง (77-hd → zmdb/embed → stream037) player ถึงจะเล่นจริง
#   แล้ว master (g.zmdb.net/.../_master?gw_enc=..) จะถูกโหลดโดย hls.js เอง
# --------------------------------------------------------------------------- #
def grab_master(page_url: str, headful: bool = False,
                timeout_s: int = 60) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "ยังไม่ได้ติดตั้ง Playwright — รัน:\n"
            "  .venv/bin/pip install playwright\n"
            "  .venv/bin/playwright install chromium")

    masters: list[str] = []

    def note(u: str) -> None:
        if "_master" in u and u not in masters:
            masters.append(u)

    def _close_ad_tab(pg) -> None:
        """ปิด popup/แท็บโฆษณาทันที — สำคัญมาก: กันแท็บโฆษณาสะสมจนกินแรมเครื่องค้าง
        (การคลิกเล่นในหน้าสตรีมมิ่งจุดชนวน popunder ได้ตลอด แต่ละแท็บ = renderer เต็ม ~150MB)"""
        try:
            pg.close()
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
            args=["--autoplay-policy=no-user-gesture-required",
                  "--disable-blink-features=AutomationControlled",
                  # จำกัด heap ของ V8 ต่อ renderer ไม่ให้บวมจนกินแรมหมดเครื่อง
                  "--js-flags=--max-old-space-size=256"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 768},
                locale="th-TH",
            )
            ctx.add_init_script(STEALTH)

            # --- กันเครื่องค้าง (1): บล็อกทรัพยากรหนักที่ไม่จำเป็นต่อการดัก master ---
            # เราต้องการแค่ให้ hls.js ยิง request `_master` เท่านั้น ไม่ต้องโหลด
            # รูป/วิดีโอโฆษณา/ฟอนต์ (กินแรม+เน็ตมหาศาลบนเครื่อง 8GB). request URL ยัง
            # ถูกจับผ่าน event ก่อน abort จึงไม่พลาด master
            block_types = {"image", "media", "font"}

            def _route(route):
                try:
                    if route.request.resource_type in block_types:
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    pass

            ctx.route("**/*", _route)

            page = ctx.new_page()

            # --- กันเครื่องค้าง (2): ปิดทุกแท็บที่ไม่ใช่หน้าหลักทันทีที่ถูกเปิด ---
            ctx.on("page", lambda pg: None if pg is page else _close_ad_tab(pg))
            page.on("popup", _close_ad_tab)
            page.on("request", lambda r: note(r.url))
            page.on("response", lambda r: note(r.url))

            try:
                page.goto(page_url, wait_until="domcontentloaded",
                          timeout=timeout_s * 1000)
            except Exception as e:
                print(f"  เตือน: โหลดหน้าเว็บมีปัญหา ({e})", file=sys.stderr)
            page.wait_for_timeout(500)   # ให้ iframe embed แนบตัวสักครู่ (จากเดิม 3000)

            # สั่งเล่นในทุก frame (player อยู่ใน iframe ซ้อน) จน master โผล่
            # popup ถูกปิดอัตโนมัติแล้ว การคลิกจึงปลอดภัย (ไม่สะสมแท็บ)
            # ไม่คลิกพิกัดกลางจอแบบสุ่มอีกต่อไป — เป็นตัวจุดชนวน popunder โดยตรง
            deadline = timeout_s * 1000
            step = 400                   # โพลถี่ขึ้น (จากเดิม 1000) จับ master/สถานะได้ไวขึ้น
            waited = 500
            play_js = ("document.querySelectorAll('video')"
                       ".forEach(v=>{try{v.muted=true;v.play()}catch(e){}});"
                       "document.querySelectorAll('button,[class*=play]')"
                       ".forEach(x=>{try{x.click()}catch(e){}})")
            # ต้นทาง zmdb จะโชว์ข้อความพวกนี้เมื่อไม่มีสตรีมจริง — ตรวจเจอแล้วเลิกทันที
            # (ไม่ต้องรอจนหมด timeout) และรายงานเหตุผลที่ถูกต้อง
            NOT_FOUND_MARKERS = ("ไม่พบวิดีโอ", "อาจถูกลบ", "video not found",
                                 "ลิงก์ไม่ถูกต้อง")
            not_found = False
            while waited < deadline and not masters:
                for fr in page.frames:
                    try:
                        fr.evaluate(play_js)
                    except Exception:
                        pass
                # เช็ค "ไม่พบวิดีโอ" ทุกโพล (ข้อความ static เรนเดอร์เร็ว จะได้เลิกไว)
                if not masters:
                    for fr in page.frames:
                        if "zmdb.net/embed" not in fr.url:
                            continue
                        try:
                            txt = fr.evaluate(
                                "document.body?document.body.innerText:''") or ""
                        except Exception:
                            txt = ""
                        if any(mk in txt for mk in NOT_FOUND_MARKERS):
                            not_found = True
                            break
                    if not_found:
                        break
                if masters:
                    break
                page.wait_for_timeout(step)
                waited += step
        finally:
            # --- กันเครื่องค้าง (3): ปิด browser เสมอ แม้เกิด exception ระหว่างทาง ---
            try:
                browser.close()
            except Exception:
                pass

    if not masters:
        if not_found:
            raise RuntimeError(
                "ต้นทาง (zmdb) แจ้งว่า “ไม่พบวิดีโอ” — เรื่องนี้ยังไม่มีสตรีมจริงบนเซิร์ฟเวอร์ "
                "(หน้าเว็บอาจมีแค่ตัวอย่าง หรือหนังถูกลบ/ยังไม่ปล่อย) — ไม่ใช่ปัญหาของสคริปต์")
        raise RuntimeError(
            "ดัก master URL ไม่ได้ภายในเวลาที่กำหนด — เป็นไปได้ว่า:\n"
            "  • หนังเรื่องนี้ยังไม่มีสตรีมจริง (หน้าเว็บมีแต่ตัวอย่าง/trailer) — พบบ่อยกับหนังใหม่ที่ยังไม่ออก\n"
            "  • ติด Cloudflare/anti-bot ของต้นทาง — ลอง --headful เพื่อดูหน้าจอจริง\n"
            "  • เน็ตช้า/เซิร์ฟเวอร์ช้า — ลองเพิ่ม --timeout")
    # เผื่อมีหลายตัว เอาตัวที่มี gw_enc/sig ก่อน
    masters.sort(key=lambda u: (0 if ("gw_enc" in u or "sig" in u) else 1))
    return masters[0]


# --------------------------------------------------------------------------- #
# probe: โชว์ความละเอียดของ master (informational)
# --------------------------------------------------------------------------- #
def probe_master(master_url: str) -> None:
    try:
        txt = http_get(master_url, referer="https://zmdb.net/").decode("utf-8", "replace")
    except Exception as e:
        print(f"  (probe ไม่ได้: {e})", file=sys.stderr)
        return
    res = sorted(set(re.findall(r"RESOLUTION=(\d+x\d+)", txt)))
    auds = len(re.findall(r"#EXT-X-MEDIA:TYPE=AUDIO", txt))
    subs = len(re.findall(r"#EXT-X-MEDIA:TYPE=SUBTITLES", txt))
    print(f"  ความละเอียด: {', '.join(res) or '(อ่านไม่ได้)'}  "
          f"| เสียง {auds} แทร็ก | ซับ {subs} แทร็ก", file=sys.stderr)


# --------------------------------------------------------------------------- #
def print_links(links: list[dict]) -> None:
    print("ลิงก์ที่มี:")
    for i, l in enumerate(links):
        mark = "" if l.get("embedUrl") else "  [ไม่มี embedUrl]"
        vip = " [VIP]" if l.get("isVipOnly") else ""
        print(f"  [{i}] {l.get('language','?'):<12} "
              f"{l.get('qualityLabel','?'):<5} "
              f"{l.get('serverLabel','?')}{vip}{mark}")
        if l.get("embedUrl"):
            print(f"       {l['embedUrl']}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="หา master URL ของหนังจาก 77-hd.com อัตโนมัติ แล้วโหลดด้วย fetch_zmdb.py")
    ap.add_argument("page_url", help="URL หน้าหนังของ 77-hd.com")

    # โหมดทำงาน
    ap.add_argument("--list", action="store_true",
                    help="แสดงลิงก์ที่มี (ภาษา/คุณภาพ/เซิร์ฟเวอร์) แล้วจบ")
    ap.add_argument("--print-master", action="store_true",
                    help="พิมพ์ master URL ที่หาได้แล้วจบ (ไม่โหลด)")
    ap.add_argument("--headful", action="store_true",
                    help="เปิดเบราว์เซอร์แบบเห็นหน้าต่าง (ดีบั๊ก Cloudflare/โฆษณา)")
    ap.add_argument("--timeout", type=int, default=60,
                    help="วินาทีที่รอดัก master URL (ค่าเริ่มต้น 60)")

    # ส่งต่อให้ fetch_zmdb.py
    ap.add_argument("--format", choices=["mp4", "mkv"], default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--audio-lang", default=None)
    ap.add_argument("--audio-langs", default=None)
    ap.add_argument("--sub-langs", default=None)
    ap.add_argument("--default-audio", default=None)
    ap.add_argument("--subs-off", action="store_true")
    ap.add_argument("-o", "--output", default=None,
                    help="ชื่อไฟล์ปลายทาง (ไม่ใช่โฟลเดอร์)")
    ap.add_argument("--out-dir", "--output-dir", dest="out_dir", default=None,
                    help="โฟลเดอร์/ไดรฟ์ปลายทาง เช่น '/Volumes/Aofserver Ext' "
                         "(ค่าเริ่มต้น: โฟลเดอร์ downloads/ ในโปรเจกต์)")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    # --- ชั้น 1-3: metadata (embed id + bootstrap + links) ---
    # ใช้เฉพาะ --list (โชว์ลิงก์) หรือดาวน์โหลด (ตั้งชื่อไฟล์) เท่านั้น
    # --print-master ไม่ต้องใช้ → ข้ามทั้งหมดเพื่อความเร็ว (ประหยัด 3 HTTP round trips)
    title, year, ql = "video", None, ""
    links: list[dict] = []
    if not args.print_master:
        print("อ่านหน้าเว็บ 77-hd ...", file=sys.stderr)
        embed_id, mtype = resolve_embed(args.page_url)
        print(f"  zmdb embed id = {embed_id} (type={mtype})", file=sys.stderr)
        try:
            boot = get_bootstrap(embed_id, mtype)
            content = boot.get("content", {})
            title = content.get("title") or content.get("titleTh") or "video"
            year = content.get("year")
            link_token = boot.get("linkToken")
            links = get_links(embed_id, mtype, link_token) if link_token else []
            if links:
                ql = (links[0].get("qualityLabel") or "").replace(" ", "")
            print(f"  เรื่อง: {title}" + (f" ({year})" if year else ""), file=sys.stderr)
        except Exception as e:
            links = []
            if args.list:
                raise
            print(f"  (ดึง metadata ไม่ได้: {e} — ข้ามไปหา master ต่อ)", file=sys.stderr)

    if args.list:
        if not links:
            raise RuntimeError("ไม่มี playerEmbedLinks ให้แสดง")
        print_links(links)
        return 0

    # --- ชั้น 4: โหลดหน้า 77-hd จริงด้วย Playwright → ดัก master URL ---
    # master มีทุก audio/subtitle track อยู่แล้ว เลือกภาษาตอนโหลดด้วย flags ของ fetch_zmdb
    print("เปิดหน้าเว็บด้วย browser จริงเพื่อดัก master URL ...", file=sys.stderr)
    master = grab_master(args.page_url, headful=args.headful, timeout_s=args.timeout)
    print(f"  master URL: {master}", file=sys.stderr)
    probe_master(master)

    if args.print_master:
        print(master)   # stdout: เอาไปใช้ต่อสะดวก
        return 0

    # --- ต่อให้ fetch_zmdb.py ---
    ext = args.format or "mp4"
    default_name = f"{title}" + (f" {year}" if year else "")
    if ql:
        default_name += f" [{ql}]"
    default_name += f".{ext}"

    cmd = [sys.executable, str(FETCH), master]
    if args.format:        cmd += ["--format", args.format]
    if args.height:        cmd += ["--height", str(args.height)]
    if args.audio_lang:    cmd += ["--audio-lang", args.audio_lang]
    if args.audio_langs:   cmd += ["--audio-langs", args.audio_langs]
    if args.sub_langs:     cmd += ["--sub-langs", args.sub_langs]
    if args.default_audio: cmd += ["--default-audio", args.default_audio]
    if args.subs_off:      cmd += ["--subs-off"]
    cmd += ["-o", args.output or default_name]
    if args.out_dir:       cmd += ["--out-dir", args.out_dir]
    if args.workers:       cmd += ["--workers", str(args.workers)]

    print("เริ่มดาวน์โหลดด้วย fetch_zmdb.py ...", file=sys.stderr)
    print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), file=sys.stderr)
    return subprocess.call(cmd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nยกเลิก", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        print(f"ผิดพลาด: {e}", file=sys.stderr)
        sys.exit(1)
