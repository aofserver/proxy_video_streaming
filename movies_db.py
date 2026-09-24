#!/usr/bin/env python3
"""
movies_db.py — ที่เก็บรายการหนังทั้งหมด (77hd + 24hd) ใน SQLite ไฟล์เดียว

แทนไฟล์ JSON สองไฟล์เดิม (77hd_movies.json / 24hd_movies.json)
ใช้ sqlite3 จาก stdlib ล้วน

⚠️ ห้าม import โมดูลอื่นในโปรเจกต์นี้จากไฟล์นี้ — tools_77hd.py import list_77hd +
   list_24hd อยู่แล้ว ถ้าไฟล์นี้ import กลับจะเกิด cycle (และ list_*.py ไม่ควรต้อง
   ลาก Playwright/FastAPI เข้ามาเพียงเพื่อเขียน DB)

ทดสอบตัวเอง: python3 movies_db.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "movies.db"

FIELDS = ("title", "url", "image", "description", "sound", "quality", "embed", "master")

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies(
  id          INTEGER PRIMARY KEY,   -- rowid — คงที่ข้ามรอบ crawl เพราะเราอัปเดต ไม่ได้ลบ
  source      TEXT NOT NULL,         -- '77hd' | '24hd'
  title       TEXT,
  url         TEXT NOT NULL UNIQUE,  -- natural key ของ crawler ทั้งคู่
  image       TEXT,
  description TEXT,
  sound       TEXT,                  -- 77hd (NULL สำหรับ 24hd)
  quality     TEXT,                  -- 77hd
  embed       TEXT,                  -- 24hd
  master      TEXT                   -- 24hd
);
CREATE INDEX IF NOT EXISTS idx_movies_source ON movies(source);
"""

# ค่าว่าง ('') จาก crawler หมายถึง "รอบนี้ดึงไม่ได้" ไม่ใช่ "ไม่มีค่า" —
# โหมด --no-desc / --no-master / fetch ล้ม ล้วนส่งค่าว่างมา ถ้าเขียนทับตรง ๆ
# ข้อมูลเดิมจะหายเงียบ ๆ จึงคงค่าเดิมไว้เมื่อค่าใหม่ว่าง
_UPSERT = """
INSERT INTO movies(source,title,url,image,description,sound,quality,embed,master)
VALUES(?,?,?,?,?,?,?,?,?)
ON CONFLICT(url) DO UPDATE SET
  source      = excluded.source,
  title       = COALESCE(NULLIF(excluded.title,''),       movies.title),
  image       = COALESCE(NULLIF(excluded.image,''),       movies.image),
  description = COALESCE(NULLIF(excluded.description,''), movies.description),
  sound       = COALESCE(NULLIF(excluded.sound,''),       movies.sound),
  quality     = COALESCE(NULLIF(excluded.quality,''),     movies.quality),
  embed       = COALESCE(NULLIF(excluded.embed,''),       movies.embed),
  master      = COALESCE(NULLIF(excluded.master,''),      movies.master)
"""

_ready: set[str] = set()   # path ที่สร้าง schema แล้วในโปรเซสนี้


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)   # รอ lock แทนที่จะ error ทันที
    con.row_factory = sqlite3.Row
    # อ่านไม่บล็อกการเขียน — /movies.json อ่านบน request thread พร้อมกับที่
    # start_update() เขียนจาก background thread
    con.execute("PRAGMA journal_mode=WAL")
    key = str(DB_PATH)
    if key not in _ready:
        con.executescript(SCHEMA)
        _ready.add(key)
    return con


def _row(source: str, url: str, rec: dict) -> tuple:
    """dict ของ crawler → tuple ตามลำดับคอลัมน์ (ค่าว่างเก็บเป็น NULL)"""
    v = lambda k: (rec.get(k) or "").strip() or None   # noqa: E731
    return (source, v("title"), url, v("image"), v("description"),
            v("sound"), v("quality"), v("embed"), v("master"))


def upsert(source: str, movies: dict[str, dict]) -> int:
    """เขียน/อัปเดตรายการของแหล่งหนึ่ง (คีย์ด้วย url) — ค่าว่างไม่ทับค่าเดิม
    คืนจำนวนแถวที่เขียน"""
    rows = [_row(source, u, rec) for u, rec in movies.items() if u]
    con = connect()
    try:
        with con:                       # commit ให้เอง / rollback ถ้า error
            con.executemany(_UPSERT, rows)
    finally:
        con.close()
    return len(rows)


def load_all() -> list[dict]:
    """ทุกแถว เรียงตาม id"""
    con = connect()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM movies ORDER BY id")]
    finally:
        con.close()


def get(movie_id: int) -> dict | None:
    """แถวเดียวตาม id (None ถ้าไม่พบ) — ใช้ตอนหน้า player เปิดด้วย ?id="""
    con = connect()
    try:
        r = con.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def count(source: str | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM movies", ()
    if source:
        sql += " WHERE source=?"
        params = (source,)
    con = connect()
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    """ยืนยันว่าการ upsert ไม่ทำข้อมูลเดิมหาย (จำลองโหมด --no-desc) และ id คงที่"""
    import tempfile

    global DB_PATH
    with tempfile.TemporaryDirectory() as d:
        DB_PATH = Path(d) / "t.db"

        # รอบแรก: ข้อมูลครบ
        upsert("77hd", {"https://x/1": {
            "title": "T", "image": "i.jpg", "description": "ย่อ",
            "sound": "พากย์ไทย", "quality": "HD", "master": "https://m/p.m3u8"}})
        before = load_all()[0]
        assert before["description"] == "ย่อ" and before["master"] == "https://m/p.m3u8"

        # รอบสอง: ดึงไม่ครบ (มีแค่ title) — ค่าเดิมต้องไม่หาย
        upsert("77hd", {"https://x/1": {"title": "T"}})
        after = load_all()[0]
        assert after["id"] == before["id"], "id ต้องคงที่ข้ามรอบ crawl"
        assert after["description"] == "ย่อ", "description โดนค่าว่างลบ"
        assert after["master"] == "https://m/p.m3u8", "master โดนค่าว่างลบ"
        assert after["image"] == "i.jpg", "image โดนค่าว่างลบ"
        assert after["sound"] == "พากย์ไทย"

        # ค่าใหม่ที่ไม่ว่างต้องทับได้
        upsert("77hd", {"https://x/1": {"title": "T2", "quality": "4K"}})
        assert load_all()[0]["title"] == "T2" and load_all()[0]["quality"] == "4K"

        # แหล่งที่สอง + ฟิลด์ที่ไม่มีต้องเป็น NULL
        upsert("24hd", {"https://y/2": {"title": "U", "master": "https://n/p.m3u8"}})
        assert count() == 2 and count("24hd") == 1 and count("77hd") == 1
        r24 = [m for m in load_all() if m["source"] == "24hd"][0]
        assert r24["sound"] is None and r24["quality"] is None
        assert r24["url"] == "https://y/2" and r24["master"] == "https://n/p.m3u8"

        # url ว่างถูกข้าม
        assert upsert("77hd", {"": {"title": "ข้าม"}}) == 0 and count() == 2

        # get() ตาม id — ตัวที่หน้า player ใช้
        want = [m for m in load_all() if m["url"] == "https://x/1"][0]
        assert get(want["id"])["url"] == "https://x/1"
        assert get(want["id"])["description"] == "ย่อ"
        assert get(999999) is None

    print("movies_db self-check: PASS")


if __name__ == "__main__":
    _self_check()
