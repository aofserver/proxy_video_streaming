#!/usr/bin/env python3
"""
fetch_zmdb.py — ดาวน์โหลด HLS (zmdb / cdn-osx content-steering) เป็น mp4 หรือ mkv

ทำไมต้องมีสคริปต์นี้แทน yt-dlp ตรงๆ:
  สตรีมนี้ใช้ HLS Content Steering — playlist ออกจาก g.zmdb.net แต่ segment
  จริงต้องโหลดจาก CDN mirror ที่ระบุใน playback-routing.json ไม่งั้นได้ 403.
  โฮสต์ mirror เปลี่ยนตาม gw_enc (o1, o3, ...) จึงต้องอ่านแบบ dynamic.

โหมด output:
  --format mp4  (ค่าเริ่มต้น) : วิดีโอ + เสียง 1 แทร็ก -> .mp4
  --format mkv               : วิดีโอ + เสียง "ทุกภาษา" + ซับ "ทุกภาษา" -> .mkv
                               (สลับเสียง/เปิดปิดซับได้ใน player เช่น VLC/IINA/mpv)

รับ URL ได้ 2 แบบ:
  * master URL   เช่น .../t.<hash>/_master?...     (มีหลายความชัด + เสียง + ซับ)
  * media index  เช่น .../_v/_index?...            (วิดีโอแทร็กเดียว ไม่มีเสียง/ซับ)

ตัวอย่าง:
  ./fetch_zmdb.py MASTER_URL --format mkv -o "หนัง.mkv"
  ./fetch_zmdb.py MASTER_URL --format mp4 --height 1080 --audio-lang th -o "หนัง.mp4"
  ./fetch_zmdb.py "VIDEO_INDEX_URL" --format mp4 -o "clip.mp4"

ขอบเขต: ใช้กับเนื้อหาที่คุณมีสิทธิ์ดาวน์โหลดเท่านั้น
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "downloads"
BIN_DIR = HERE / "bin"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "Origin": "https://zmdb.net",
    "Referer": "https://zmdb.net/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
}


def set_headers(referer: str | None = None, origin: str | None = None) -> None:
    """เปลี่ยน Origin/Referer ที่ใช้ยิงทุก request (แหล่งต่างเว็บต้องการค่าต่างกัน)
    เช่น vdohls (24hd) ต้องการ Referer=https://player77hdfree.xyz/ และไม่ต้องมี Origin
    ส่ง origin="" เพื่อลบ Origin header ทิ้ง"""
    if referer is not None:
        HEADERS["Referer"] = referer
    if origin is not None:
        if origin == "":
            HEADERS.pop("Origin", None)
        else:
            HEADERS["Origin"] = origin


# --------------------------------------------------------------------------- #
# หา ffmpeg / ffprobe (bin/ ในโฟลเดอร์นี้ก่อน แล้วค่อย PATH)
# --------------------------------------------------------------------------- #
def _tool(name: str) -> str:
    local = BIN_DIR / name
    if local.exists():
        return str(local)
    import shutil
    found = shutil.which(name)
    return found or name


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, tries: int = 6, timeout: int = 60) -> bytes:
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2 ** i, 20))  # 429 -> ถอย exponential
    raise RuntimeError(f"GET ล้มเหลว {url}: {last}")


def http_get_with(url: str, headers: dict, tries: int = 4, timeout: int = 60) -> bytes:
    """เหมือน http_get แต่ใช้ headers ที่ส่งมา (ไม่แตะ HEADERS global — thread-safe)
    ใช้กับแหล่งที่ต้องการ Referer/Origin ต่างจาก zmdb เช่น vdohls (24hd)"""
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2 ** i, 15))
    raise RuntimeError(f"GET ล้มเหลว {url}: {last}")


def get_text(url: str) -> str:
    return http_get(url).decode("utf-8", "replace")


def swap_host(url: str, host: str | None) -> str:
    if not host:                 # None/"" = คง host เดิมไว้ (แหล่งที่ segment อยู่ host ของตัวเอง)
        return url
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(p._replace(netloc=host))


def resolve(base_url: str, ref: str) -> str:
    return urllib.parse.urljoin(base_url, ref)


def qs_of(url: str) -> str:
    """query string ของ url (ใช้ต่อ gw_enc ให้ playback-routing.json)"""
    return urllib.parse.urlparse(url).query


# --------------------------------------------------------------------------- #
# Content Steering — อ่าน CDN mirror แบบ dynamic
# --------------------------------------------------------------------------- #
def resolve_seg_hosts(any_url: str, steering_uri: str | None) -> list[str]:
    """คืนรายชื่อโฮสต์สำหรับโหลด segment ตามลำดับความสำคัญ (จาก routing json)

    ถ้าอ่านไม่ได้ จะ fallback เป็น host ของ playlist เอง
    """
    p = urllib.parse.urlparse(any_url)
    playlist_host = p.netloc

    routing_url = None
    if steering_uri:
        routing_url = resolve(any_url, steering_uri)
    else:
        # เดา path มาตรฐาน โดยคง gw_enc เดิม
        routing_url = f"{p.scheme}://{playlist_host}/hls/playback-routing.json"
        q = qs_of(any_url)
        if "gw_enc=" in q:
            enc = re.search(r"gw_enc=[^&]+", q)
            if enc:
                routing_url += "?" + enc.group(0)

    hosts: list[str] = []
    try:
        data = json.loads(get_text(routing_url))
        clones = {c["ID"]: c for c in data.get("PATHWAY-CLONES", [])}
        for pid in data.get("PATHWAY-PRIORITY", []):
            if pid == ".":
                hosts.append(playlist_host)
            elif pid in clones:
                h = clones[pid].get("URI-REPLACEMENT", {}).get("HOST")
                if h:
                    hosts.append(h)
    except Exception as e:  # noqa: BLE001
        print(f"  เตือน: อ่าน routing ไม่ได้ ({e}) ใช้ host ของ playlist แทน",
              file=sys.stderr)

    # ให้ playlist host เป็นตัวเลือกสุดท้ายเสมอ (segment มักไม่ได้ที่ host นี้ แต่กันพลาด)
    if playlist_host not in hosts:
        hosts.append(playlist_host)
    return hosts


# --------------------------------------------------------------------------- #
# แกะ master playlist
# --------------------------------------------------------------------------- #
def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_videos(master_url: str, master: str) -> list[tuple[int, str]]:
    """คืน [(height, url), ...] เรียงจากเล็กไปใหญ่"""
    lines = master.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", ln)
            h = int(m.group(1)) if m else 0
            out.append((h, resolve(master_url, lines[i + 1].strip())))
    out.sort(key=lambda c: c[0])
    return out


def parse_media(master_url: str, master: str, mtype: str) -> list[dict]:
    """คืนรายการ audio หรือ subtitle: [{lang, name, default, url}, ...]"""
    out = []
    for ln in master.splitlines():
        if ln.startswith("#EXT-X-MEDIA") and f"TYPE={mtype}" in ln:
            u = re.search(r'URI="([^"]+)"', ln)
            if not u:
                continue
            lm = re.search(r'LANGUAGE="([^"]+)"', ln)
            nm = re.search(r'NAME="([^"]+)"', ln)
            out.append({
                "lang": lm.group(1) if lm else "und",
                "name": nm.group(1) if nm else "",
                "default": "DEFAULT=YES" in ln,
                "url": resolve(master_url, u.group(1)),
            })
    return out


def steering_uri_of(master: str) -> str | None:
    m = re.search(r'#EXT-X-CONTENT-STEERING:[^\n]*SERVER-URI="([^"]+)"', master)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Token สด: sub-playlist URL มี sig/exp ที่หมดอายุใน ~15 นาที
# หนังยาวโหลด video เกินเวลานั้น -> ต้องดึง master ใหม่ก่อนเริ่มแต่ละ track
# เพื่อให้ได้ sig สดของ track นั้น ๆ  (จับคู่ด้วย path เช่น /_v/ , /_a_1/ , /_s/_tha)
# --------------------------------------------------------------------------- #
def _track_key(url: str) -> str:
    """คีย์ระบุ track จาก path โดยไม่สน query (sig) เช่น /hls/<id>/_a_1/_index"""
    return urllib.parse.urlparse(url).path


class FreshResolver:
    """ดึง master URL ใหม่เพื่อได้ sub-playlist URL ที่มี token สด"""

    def __init__(self, master_url: str):
        self.master_url = master_url

    def fresh(self, stale_url: str) -> str:
        """คืน URL ของ track เดียวกับ stale_url แต่ token สดจาก master ล่าสุด"""
        key = _track_key(stale_url)
        master = get_text(self.master_url)
        # หา URI ทุกตัวใน master แล้วจับที่ path ตรงกัน
        cands = re.findall(r'URI="([^"]+)"', master)
        # บวก stream-inf (บรรทัดถัดไป) ด้วย
        lines = master.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                cands.append(lines[i + 1].strip())
        for ref in cands:
            u = resolve(self.master_url, ref)
            if _track_key(u) == key:
                return u
        return stale_url  # หาไม่เจอ ใช้ตัวเดิม


# --------------------------------------------------------------------------- #
# ดาวน์โหลด media playlist หนึ่งตัว -> ไฟล์เดียว
# --------------------------------------------------------------------------- #
def download_playlist(media_url: str, out_file: Path, label: str,
                      seg_hosts: list[str], workers: int = 4,
                      resolver: "FreshResolver | None" = None) -> None:
    if resolver is not None:
        media_url = resolver.fresh(media_url)  # token สดก่อนเริ่ม track นี้
    text = get_text(media_url)
    seg_refs = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")]
    map_m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)

    parts: list[str] = []
    if map_m:
        parts.append(map_m.group(1))      # init segment (hdr.bin / hdr__v.bin)
    parts.extend(seg_refs)                # s_*.bin / seg_*.bin

    abs_urls = [resolve(media_url, ref) for ref in parts]
    total = len(abs_urls)
    print(f"  [{label}] {total} ชิ้น -> {out_file.name}", file=sys.stderr)

    def fetch(idx_url):
        idx, url = idx_url
        for host in seg_hosts:
            try:
                return idx, http_get(swap_host(url, host), tries=3)
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"[{label}] segment {idx} โหลดไม่ได้จาก mirror ใดเลย")

    # เขียนลงดิสก์ทีละชิ้นตามลำดับ (ไม่ buffer ทั้ง track ใน RAM)
    # ชิ้นที่มาไม่เรียงเก็บใน pending ชั่วคราว แล้ว flush เมื่อถึงคิว
    pending: dict[int, bytes] = {}
    next_write = 0
    done = 0
    with out_file.open("wb") as f:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for idx, data in ex.map(fetch, list(enumerate(abs_urls))):
                pending[idx] = data
                while next_write in pending:
                    f.write(pending.pop(next_write))
                    next_write += 1
                done += 1
                if done % 20 == 0 or done == total:
                    print(f"  [{label}] {done}/{total}", file=sys.stderr)
        # เผื่อมีชิ้นตกค้าง (ไม่ควรเกิด แต่กันไว้)
        for i in range(next_write, total):
            if i in pending:
                f.write(pending.pop(i))


def download_vtt(sub_url: str, out_file: Path, label: str,
                 seg_hosts: list[str],
                 resolver: "FreshResolver | None" = None) -> bool:
    """ซับใน HLS เป็น playlist ที่ชี้ไฟล์ .vtt — ต่อกันเป็นไฟล์เดียว"""
    if resolver is not None:
        sub_url = resolver.fresh(sub_url)
    try:
        text = get_text(sub_url)
    except Exception:
        return False
    refs = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    if not refs:
        return False
    chunks = []
    for ref in refs:
        abs_url = resolve(sub_url, ref)
        got = None
        for host in seg_hosts:
            try:
                got = http_get(swap_host(abs_url, host), tries=3)
                break
            except Exception:  # noqa: BLE001
                continue
        if got is None:
            return False
        chunks.append(got.decode("utf-8", "replace"))
    # รวม WEBVTT (เก็บ header อันเดียว)
    merged = "WEBVTT\n\n" + "\n".join(
        re.sub(r"^WEBVTT.*?\n", "", c, count=1, flags=re.S).strip()
        for c in chunks
    ) + "\n"
    out_file.write_text(merged, encoding="utf-8")
    print(f"  [{label}] ซับ {len(refs)} ชิ้น -> {out_file.name}", file=sys.stderr)
    return True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="master URL หรือ media-index URL")
    ap.add_argument("--format", choices=["mp4", "mkv"], default="mp4",
                    help="mp4=แทร็กเดียว, mkv=ทุกเสียง+ทุกซับ")
    ap.add_argument("--height", type=int, default=1080, help="ความสูงสูงสุด")
    ap.add_argument("--audio-lang", default=None,
                    help="[mp4] เลือกภาษาเสียง 1 ภาษา เช่น th (ค่าเริ่มต้น=default)")
    ap.add_argument("--audio-langs", default=None,
                    help="[mkv] เลือกเฉพาะภาษาเสียงที่ต้องการ คั่นด้วย , เช่น th,en "
                         "(ไม่ระบุ=ทุกภาษา)")
    ap.add_argument("--sub-langs", default=None,
                    help="[mkv] เลือกเฉพาะภาษาซับที่ต้องการ คั่นด้วย , เช่น th,en "
                         "(ไม่ระบุ=ทุกภาษา; ใส่ none เพื่อไม่เอาซับเลย)")
    ap.add_argument("--default-audio", default=None,
                    help="[mkv] ภาษาเสียงที่จะเล่นอัตโนมัติ เช่น th "
                         "(ไม่ระบุ=ตามต้นฉบับ)")
    ap.add_argument("--subs-off", action="store_true",
                    help="[mkv] ไม่ตั้งซับเป็น default (เปิดมาไม่โชว์ซับ แต่ยังเลือกได้)")
    ap.add_argument("-o", "--output", default=None, help="ชื่อไฟล์ปลายทาง")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--workers", type=int, default=4, help="จำนวน thread โหลด segment")
    ap.add_argument("--referer", default=None,
                    help="Referer header (เว็บอื่นต้องการค่าต่างกัน เช่น vdohls/24hd)")
    ap.add_argument("--origin", default=None,
                    help="Origin header (ใส่สตริงว่าง '' เพื่อลบทิ้ง)")
    ap.add_argument("--no-steering", action="store_true",
                    help="ไม่มี content steering (เช่น vdohls/24hd) — โหลด segment "
                         "จาก host ของ playlist ตรง ๆ ไม่ยิง playback-routing.json")
    args = ap.parse_args()

    if args.referer is not None or args.origin is not None:
        set_headers(referer=args.referer, origin=args.origin)

    def _seg_hosts(any_url: str, steering_uri: str | None) -> list[str]:
        # ข้าม steering (vdohls ไม่มี routing json) — โหลด segment จาก host เดิมของมัน
        # (None = ไม่ swap host; segment ของ vdohls อยู่คนละ host กับ playlist เช่น vh004.xyz)
        if args.no_steering:
            return [None]
        return resolve_seg_hosts(any_url, steering_uri)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tmp"
    tmp.mkdir(exist_ok=True)

    print("ดึง playlist หลัก...", file=sys.stderr)
    top = get_text(args.url)

    # --- กรณี URL เป็น media index ตรงๆ (ไม่ใช่ master) ---
    if not is_master(top):
        print("URL นี้เป็น media playlist แทร็กเดียว (ไม่มีเสียง/ซับให้เลือก)",
              file=sys.stderr)
        seg_hosts = _seg_hosts(args.url, None)
        print(f"CDN mirror: {seg_hosts}", file=sys.stderr)
        v_tmp = tmp / "v.mp4"
        download_playlist(args.url, v_tmp, "video", seg_hosts, args.workers)
        name = args.output or "output.mp4"
        final = out_dir / name
        r = subprocess.run([FFMPEG, "-y", "-i", str(v_tmp), "-c", "copy",
                            str(final)], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-1500:], file=sys.stderr)
            return 1
        v_tmp.unlink(missing_ok=True)
        _report(final)
        return 0

    # --- กรณี master ---
    videos = parse_videos(args.url, top)
    audios = parse_media(args.url, top, "AUDIO")
    subs = parse_media(args.url, top, "SUBTITLES")
    steer = steering_uri_of(top)
    seg_hosts = _seg_hosts(args.url, steer)
    resolver = FreshResolver(args.url)  # ดึง token สดก่อนแต่ละ track

    print(f"CDN mirror: {seg_hosts}", file=sys.stderr)
    print(f"video renditions: {[h for h, _ in videos]}", file=sys.stderr)
    print(f"audio: {[(a['lang'], a['name']) for a in audios]}", file=sys.stderr)
    print(f"subs : {[(s['lang'], s['name']) for s in subs]}", file=sys.stderr)

    # กรองภาษา (โหมด mkv) ตาม --audio-langs / --sub-langs
    def _wanted(csv: str | None) -> set[str] | None:
        if csv is None:
            return None  # ไม่ระบุ = เอาทุกภาษา
        return {x.strip().lower() for x in csv.split(",") if x.strip()}

    want_a = _wanted(args.audio_langs)
    want_s = _wanted(args.sub_langs)

    if want_a is not None:
        filtered = [a for a in audios if a["lang"].lower() in want_a]
        if not filtered:
            print(f"เตือน: ไม่พบเสียงภาษา {sorted(want_a)} — ใช้ทุกภาษาแทน",
                  file=sys.stderr)
        else:
            audios = filtered
    if want_s is not None:
        if want_s == {"none"}:
            subs = []
        else:
            subs = [s for s in subs if s["lang"].lower() in want_s]

    if args.format == "mkv":
        print(f"-> จะรวมเสียง: {[a['lang'] for a in audios]}", file=sys.stderr)
        print(f"-> จะรวมซับ : {[s['lang'] for s in subs]}", file=sys.stderr)

    # เลือกวิดีโอ (<= height ที่ดีที่สุด)
    ok = [v for v in videos if v[0] <= args.height] or videos
    v_url = ok[-1][1]
    v_tmp = tmp / "v.mp4"
    download_playlist(v_url, v_tmp, "video", seg_hosts, args.workers, resolver)

    ff_inputs = ["-i", str(v_tmp)]
    ff_maps = ["-map", "0:v:0"]
    ff_meta: list[str] = []
    idx = 1  # input index ถัดจากวิดีโอ

    if args.format == "mp4":
        # เสียงเดียว
        chosen = None
        for a in audios:
            if args.audio_lang and a["lang"] == args.audio_lang:
                chosen = a
                break
        if chosen is None:
            chosen = next((a for a in audios if a["default"]), audios[0] if audios else None)
        if chosen:
            a_tmp = tmp / "a.mp4"
            download_playlist(chosen["url"], a_tmp, f"audio-{chosen['lang']}",
                              seg_hosts, args.workers, resolver)
            ff_inputs += ["-i", str(a_tmp)]
            ff_maps += ["-map", f"{idx}:a:0"]
            idx += 1
        final = out_dir / (args.output or "output.mp4")
        cmd = [FFMPEG, "-y", *ff_inputs, *ff_maps, "-c", "copy", str(final)]

    else:  # mkv — เสียง + ซับ (ตามที่กรองไว้)
        a_files = []
        want_def_a = (args.default_audio or "").strip().lower() or None
        has_default_a = any(a["default"] for a in audios)
        # ถ้าระบุ --default-audio และมีภาษานั้นจริง ให้ตัวนั้นเป็น default
        has_want_def = want_def_a is not None and any(
            a["lang"].lower() == want_def_a for a in audios)
        for n, a in enumerate(audios):
            a_tmp = tmp / f"a_{n}.mp4"
            download_playlist(a["url"], a_tmp, f"audio-{a['lang']}",
                              seg_hosts, args.workers, resolver)
            ff_inputs += ["-i", str(a_tmp)]
            ff_maps += ["-map", f"{idx}:a:0"]
            ff_meta += [f"-metadata:s:a:{n}", f"language={a['lang']}",
                        f"-metadata:s:a:{n}", f"title={a['name'] or a['lang']}"]
            # เลือก default: --default-audio > ต้นฉบับ > ตัวแรก
            if has_want_def:
                is_def = a["lang"].lower() == want_def_a
            else:
                is_def = a["default"] or (not has_default_a and n == 0)
            ff_meta += [f"-disposition:a:{n}", "default" if is_def else "0"]
            a_files.append(a_tmp)
            idx += 1

        s_files = []
        sub_count = 0
        for s in subs:
            s_tmp = tmp / f"s_{s['lang']}.vtt"
            if download_vtt(s["url"], s_tmp, f"sub-{s['lang']}", seg_hosts, resolver):
                ff_inputs += ["-i", str(s_tmp)]
                ff_maps += ["-map", f"{idx}:s:0"]
                ff_meta += [f"-metadata:s:s:{sub_count}", f"language={s['lang']}",
                            f"-metadata:s:s:{sub_count}", f"title={s['name'] or s['lang']}"]
                # ปิดซับ default ถ้า --subs-off; ไม่งั้นตามต้นฉบับ
                if not args.subs_off and s["default"]:
                    ff_meta += [f"-disposition:s:{sub_count}", "default"]
                else:
                    ff_meta += [f"-disposition:s:{sub_count}", "0"]
                s_files.append(s_tmp)
                sub_count += 1
                idx += 1

        name = args.output or "output.mkv"
        if not name.endswith(".mkv"):
            name += ".mkv"
        final = out_dir / name
        # mkv: วิดีโอ/เสียง copy, ซับ vtt -> srt (webvtt ใน mkv บาง player ไม่ชอบ)
        cmd = [FFMPEG, "-y", *ff_inputs, *ff_maps,
               "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
               *ff_meta, str(final)]

    print("รวมด้วย ffmpeg...", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2500:], file=sys.stderr)
        return 1

    # เก็บกวาด tmp
    for p in tmp.iterdir():
        p.unlink(missing_ok=True)
    tmp.rmdir()
    _report(final)
    return 0


def _report(final: Path) -> None:
    size = final.stat().st_size / 1024 / 1024
    print(f"\nเสร็จ: {final}  ({size:.1f} MB)")
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name:stream_tags=language",
             "-of", "default=noprint_wrappers=1", str(final)],
            capture_output=True, text=True).stdout
        print("แทร็กในไฟล์:\n" + out, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
