import os
import re
import json
import base64
import uuid
import requests
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, ImageMessage, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, ButtonsTemplate, PostbackAction,
    LeaveEvent, JoinEvent,
)
from google import genai
from google.genai import types

app = Flask(__name__)

LINE_TOKEN        = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET       = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
ADMIN_USER_ID     = os.environ.get("LINE_ADMIN_USER_ID", "")
PROMPTPAY_API_KEY = os.environ.get("PROMPTPAY_API_KEY", "")
# กลุ่มที่ให้ "เช็คสลิป + เตือน + ส่งรายงาน" เท่านั้น (คั่นด้วยคอมมา) — เว้นว่าง = ทุกกลุ่ม
SLIP_GROUPS       = [g.strip() for g in os.environ.get("SLIP_GROUPS", "").split(",") if g.strip()]
# กลุ่มที่ "เปิดรับจองโต๊ะ" (คั่นด้วยคอมมา) — จองวันนี้ การ์ด+ปุ่มขึ้นในกลุ่มนั้น / เว้นว่าง = ปิด
RESV_GROUPS       = [g.strip() for g in os.environ.get("RESV_GROUPS", "").split(",") if g.strip()]
# กลุ่มที่ "ปิดการจองโต๊ะ" — ไม่ยุ่งกับการจองเลย (ทั้งจองวันนี้และล่วงหน้า) เช่นกลุ่ม the riches
RESV_EXCLUDE_GROUPS = [g.strip() for g in os.environ.get("RESV_EXCLUDE_GROUPS", "").split(",") if g.strip()]
# กลุ่มที่ "ให้บอทเมินทั้งหมด" — ไม่ทำอะไรเลย (ไม่เช็คสลิป/ไม่จอง/ไม่ตอบคำสั่ง/ไม่ส่งรายงาน-เตือน) เช่นกลุ่มที่เลิกใช้แล้ว
IGNORE_GROUPS     = [g.strip() for g in os.environ.get("IGNORE_GROUPS", "").split(",") if g.strip()]
# กลุ่ม "บัญชีเจ้าหนี้การค้า" (เช่น ดวงใจการสุรา) — ในกลุ่มนี้ บอทไม่ทำระบบสลิป/จองปกติ แต่ทำบัญชีหนี้แทน
# รูปบิล=เพิ่มหนี้, สลิปโอน=ลดหนี้, สรุปยอดค้างอัตโนมัติ; ตั้ง id กลุ่มด้วยคำสั่ง groupid
PAYABLE_GROUPS    = [g.strip() for g in os.environ.get("PAYABLE_GROUPS", "").split(",") if g.strip()]
PAYABLE_VENDOR    = os.environ.get("PAYABLE_VENDOR", "ดวงใจการสุรา")   # ชื่อเจ้าหนี้ (โชว์ในรายงาน)
PAYABLE_SUMMARY_HOUR = int(os.environ.get("PAYABLE_SUMMARY_HOUR", "1"))  # ส่งสรุปหนี้รายวันหลังกี่โมง (ดีฟอลต์ ตี1)
# รับวันที่ที่ AI อ่านจากบิล/สลิป(รูป) เฉพาะที่ไม่เก่าเกินกี่วัน (กันอ่านปีเพี้ยน/มั่ว) — ยอดค้างจริงเก่าได้ จึงตั้งกว้าง; เกินช่วงนี้หรืออนาคต→ใช้วันที่ส่ง
PAYABLE_DATE_MAX_DAYS = int(os.environ.get("PAYABLE_DATE_MAX_DAYS", "90"))
# บัญชีหนี้แบบ "บัญชีเดียว 2 กลุ่ม" (mirror) — รูปแบบ "primary:mirror,primary2:mirror2"
#   primary = กลุ่มส่งบิล/สลิป (เงียบ ไม่ตอบในกลุ่ม) + เป็นที่เก็บข้อมูลจริง
#   mirror  = กลุ่มที่บอทเด้งผล/ดูยอด/สรุป (ใช้ข้อมูลชุดเดียวกับ primary)
PAYABLE_MIRROR      = {}        # primary -> mirror
_PAYABLE_PRIMARY_OF = {}        # mirror  -> primary
for _pair in os.environ.get("PAYABLE_MIRROR", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _p, _m = (x.strip() for x in _pair.split(":", 1))
        if _p and _m and _p != _m:
            PAYABLE_MIRROR[_p] = _m
            _PAYABLE_PRIMARY_OF[_m] = _p
# กลุ่ม "บาร์น้ำ+จองโต๊ะล่วงหน้า" — จองล่วงหน้าจากกลุ่มไหนก็ตามจะส่งการ์ด+ปุ่มมาที่นี่ (ดู id ด้วยคำสั่ง groupid)
BAR_GROUP_ID      = os.environ.get("BAR_GROUP_ID", "")
# redirect รายงาน: ส่งรายงานของ "กลุ่มต้นทาง" ไปเข้า "กลุ่มปลายทาง" แทน (เนื้อห้ารายงานยังเป็นของต้นทาง)
# รูปแบบ "ต้นทาง:ปลายทาง,ต้นทาง2:ปลายทาง2" — ครอบทุกรายงาน (สลิป/จอง/หนี้). ตั้งบน Render กัน repo public เห็น group id
REPORT_REDIRECT   = {}
for _pair in os.environ.get("REPORT_REDIRECT", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _src, _dst = (x.strip() for x in _pair.split(":", 1))
        if _src and _dst:
            REPORT_REDIRECT[_src] = _dst
# คีย์เวิร์ดบัญชีรับเงินของร้าน (ชื่อ ไทย/อังกฤษ + เลขบัญชี/เลขท้าย) คั่นด้วยคอมมา
# ใช้เทียบ "ปลายทาง" บนสลิป ถ้าไม่ตรงสักคำ = เตือนว่าอาจโอนผิดบัญชี (ตั้งบน Render กัน repo public เห็นเลขบัญชี)
PAYEE_KEYWORDS    = [k.strip().lower() for k in os.environ.get("PAYEE_KEYWORDS", "").split(",") if k.strip()]
# แยกบัญชีรับเงิน (ไว้แยกยอดรวมในรายงาน) — รูปแบบ "ป้ายชื่อ:คีย์เวิร์ด,คีย์เวิร์ด;ป้าย2:คีย์A,คีย์B"
# ตั้งบน Render กัน repo public เห็นเลขบัญชี เช่น "ไส้ย่างซอย4:4818,ไส้ย่าง,บริษัท;พิมนภัทร์&สุวัฒน์:6120,พิมนภัทร,สุวัฒน"
PAYEE_ACCOUNTS    = []
for _acc in os.environ.get("PAYEE_ACCOUNTS", "").split(";"):
    if ":" in _acc:
        _label, _kws = _acc.split(":", 1)
        _kwl = [k.strip().lower() for k in _kws.split(",") if k.strip()]
        if _label.strip() and _kwl:
            PAYEE_ACCOUNTS.append((_label.strip(), _kwl))
# อ่านเฉพาะ "รายรับ" (โอนเข้าบัญชีร้านที่ระบุใน PAYEE_ACCOUNTS เท่านั้น) — นอกนั้นไม่บันทึก
# INCOME_ONLY=1 = ทุกกลุ่ม / INCOME_ONLY_GROUPS = เฉพาะกลุ่มที่ระบุ (คั่นคอมมา) เช่นกลุ่ม staff
INCOME_ONLY        = os.environ.get("INCOME_ONLY", "0") == "1"
INCOME_ONLY_GROUPS = [g.strip() for g in os.environ.get("INCOME_ONLY_GROUPS", "").split(",") if g.strip()]
# เตือนในกลุ่มเมื่อบอท "อ่านรูปไม่ออกว่าเป็นสลิป" — ดีฟอลต์ปิด (กันเด้งรกกับรูปทั่วไป/รูปอาหาร); ตั้ง 1 เพื่อเปิด
SLIP_WARN_UNREAD   = os.environ.get("SLIP_WARN_UNREAD", "0") == "1"
# จองที่ยังไม่คอนเฟิร์ม จะแจ้งเตือนซ้ำ (ทุก 5 นาทีช่วง 18:00-22:00, ทุก 15 นาทีเวลาอื่น) จนกว่าจะเกินชั่วโมงนี้นับจากแจ้ง
RESV_NAG_MAX_HOURS = float(os.environ.get("RESV_NAG_MAX_HOURS", "6"))
# รายงานสรุปจองล่วงหน้า (เข้ากลุ่มบาร์น้ำหลังเที่ยงคืน) — แสดงจองล่วงหน้าที่แจ้งมาภายในกี่วันล่าสุด (ใช้กับจองที่ไม่มีวันที่จริง)
RESV_REPORT_DAYS   = int(os.environ.get("RESV_REPORT_DAYS", "7"))
# ลบข้อมูลเก่าอัตโนมัติ (วันละครั้ง) กัน DB บวม
RESV_KEEP_DAYS     = int(os.environ.get("RESV_KEEP_DAYS", "15"))  # จองที่ผ่านวันมาแล้ว เก็บ 15 วัน
MISS_KEEP_DAYS     = int(os.environ.get("MISS_KEEP_DAYS", "14"))  # ตัวนับรูปตกหล่น เก็บ 14 วัน
SLIP_KEEP_DAYS     = int(os.environ.get("SLIP_KEEP_DAYS", "60"))  # สลิป เก็บ 60 วัน
# เวลาเปิดร้าน (ชั่วโมง) สำหรับตรวจเวลาจอง — เปิด 11:00 ถึง 00:00 (เที่ยงคืน)
OPEN_HOUR, CLOSE_HOUR = 11, 24
TZ                = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))

line_bot_api = LineBotApi(LINE_TOKEN)
handler      = WebhookHandler(LINE_SECRET)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def _gemini_generate(contents, attempts=4):
    """เรียก Gemini (SDK google-genai) พร้อม retry + backoff — กัน 503 overload/สะดุดชั่วคราว
    contents เป็น str (ข้อความ) หรือ list [str, รูป]; รันใน background thread จึงรอ backoff ได้"""
    last = None
    for i in range(attempts):
        try:
            return gemini_client.models.generate_content(model=GEMINI_MODEL, contents=contents)
        except Exception as e:
            last = e
            if i < attempts - 1:
                print(f"[gemini] retry {i+1}/{attempts}: {str(e)[:100]}", flush=True)
                time.sleep(2 * (2 ** i))   # 2, 4, 8 วินาที
    raise last

# ─── Persistent storage (PostgreSQL ถ้ามี DATABASE_URL ไม่งั้น SQLite) ────────
# ย้ายมาใช้ Postgres (managed) เพราะ Render persistent disk mount ไม่เสถียร (mount ช้า/ไม่ขึ้น → ข้อมูลหาย)
# Postgres เป็น DB แยก ไม่ผูกกับ instance/disk → deploy/restart/suspend กี่รอบ ข้อมูลอยู่ครบ 100%
# โค้ดชุดเดียวรองรับทั้งสอง: ตั้ง env DATABASE_URL = ใช้ Postgres / ไม่ตั้ง = SQLite (local/ชั่วคราว)
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
USE_PG         = bool(DATABASE_URL)
_init_lock     = threading.Lock()
_init_done     = False

if USE_PG:
    import psycopg2
    import psycopg2.extras
    STORAGE_DESC = "PostgreSQL (managed)"
    _PK   = "SERIAL PRIMARY KEY"
    _REAL = "DOUBLE PRECISION"
else:
    import sqlite3
    DB_PATH = os.path.join("/var/data" if os.path.isdir("/var/data") else ".", "slips.db")
    STORAGE_DESC = f"SQLite ({DB_PATH}) [ephemeral ถ้าไม่มี disk!]"
    _PK   = "INTEGER PRIMARY KEY AUTOINCREMENT"
    _REAL = "REAL"

def _raw_connect():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)  # fail เร็วถ้า DB ล่ม ไม่ค้าง
    c = sqlite3.connect(DB_PATH, timeout=15)   # timeout กัน 'database is locked' ตอนรูปเยอะ
    c.row_factory = sqlite3.Row
    return c

class _Conn:
    """ห่อ connection ให้ใช้เหมือน sqlite3 เดิม (conn.execute(sql,params).fetchone()/fetchall(),
    row['col'], commit, context manager) — รองรับทั้ง Postgres และ SQLite ด้วยโค้ดชุดเดียว
    placeholder ในโค้ดใช้ '?' ทั้งหมด แล้วแปลงเป็น '%s' ให้อัตโนมัติเมื่อใช้ Postgres"""
    def __init__(self, raw):
        self._raw = raw
    def execute(self, sql, params=()):
        if USE_PG:
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params)
        else:
            cur = self._raw.cursor()
            cur.execute(sql, params)
        return cur
    def insert_returning_id(self, sql, params):
        # คืน id แถวที่เพิ่ง insert (Postgres ใช้ RETURNING id, SQLite ใช้ lastrowid)
        cur = self._raw.cursor()
        if USE_PG:
            cur.execute(sql.replace("?", "%s") + " RETURNING id", params)
            return cur.fetchone()[0]
        cur.execute(sql, params)
        return cur.lastrowid
    def commit(self):
        self._raw.commit()
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            self._raw.commit() if exc_type is None else self._raw.rollback()
        finally:
            self._raw.close()

def _connect():
    return _Conn(_raw_connect())

def _ensure_init():
    """สร้างตารางครั้งแรกที่ใช้ DB (idempotent) — ไม่มี background thread/Event ให้ค้างข้าม worker restart"""
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if not _init_done:
            init_db()
            _init_done = True

def _db():
    # เชื่อม DB ตรงๆ ทุกครั้ง (Postgres เชื่อมเร็ว) — DB ล่ม = error เร็วๆ ถูก catch ที่ handler
    # ไม่มี state รอ/Event ที่ค้างได้อีก; ถ้า DB กลับมา ครั้งถัดไปก็ใช้ได้เลย (self-heal)
    _ensure_init()
    return _connect()

def init_db():
    with _connect() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS slips (
                id            {_PK},
                group_id      TEXT,
                slip_date     TEXT,
                sender        TEXT,
                amount        {_REAL},
                bank          TEXT,
                ref_number    TEXT,
                slip_datetime TEXT,
                verdict       TEXT,
                recorded_at   TEXT,
                message_id    TEXT
            )
        """)
        if not USE_PG:
            # migration เฉพาะ SQLite DB เก่าที่ยังไม่มีคอลัมน์ (Postgres สร้างใหม่มีครบแล้ว)
            try:
                conn.execute("ALTER TABLE slips ADD COLUMN slip_datetime TEXT")
            except Exception:
                pass
        # migration: account_label = บัญชีปลายทางที่จับได้ (ไว้แยกยอดในรายงาน) — ทั้ง PG/SQLite
        if USE_PG:
            conn.execute("ALTER TABLE slips ADD COLUMN IF NOT EXISTS account_label TEXT")
        else:
            try:
                conn.execute("ALTER TABLE slips ADD COLUMN account_label TEXT")
            except Exception:
                pass
        # migration: message_id = id ข้อความ LINE ของรูป (กันประมวลผล/นับรูปเดียวกันซ้ำ) — ทั้ง PG/SQLite
        if USE_PG:
            conn.execute("ALTER TABLE slips ADD COLUMN IF NOT EXISTS message_id TEXT")
        else:
            try:
                conn.execute("ALTER TABLE slips ADD COLUMN message_id TEXT")
            except Exception:
                pass
        conn.execute("CREATE TABLE IF NOT EXISTS groups (group_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        # บัญชีเจ้าหนี้การค้า (ดวงใจการสุรา) — บิลซื้อ (เพิ่มหนี้) / เงินจ่าย (ลดหนี้)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS payable_bills (
                id          {_PK},
                group_id    TEXT,
                doc_date    TEXT,
                amount      {_REAL},
                note        TEXT,
                recorded_at TEXT
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS payable_payments (
                id            {_PK},
                group_id      TEXT,
                doc_date      TEXT,
                amount        {_REAL},
                sender        TEXT,
                ref_number    TEXT,
                slip_datetime TEXT,
                recorded_at   TEXT
            )
        """)
        # นับรูปที่รับเข้ามาแต่ "ไม่ได้บันทึกเป็นสลิป" (อ่านไม่ออก/ไม่ใช่สลิป/error) ไว้กระทบยอดในรายงาน
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS image_misses (
                id          {_PK},
                group_id    TEXT,
                stat_date   TEXT,
                reason      TEXT,
                recorded_at TEXT
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS reservations (
                id              {_PK},
                origin_group_id TEXT,
                requested_by    TEXT,
                customer        TEXT,
                people          TEXT,
                resv_datetime   TEXT,
                table_no        TEXT,
                note            TEXT,
                raw_text        TEXT,
                status          TEXT,
                created_at      TEXT,
                confirmed_by    TEXT,
                confirmed_at    TEXT,
                notify_group_id TEXT,
                reminded_at     TEXT
            )
        """)
        # migration: เพิ่มคอลัมน์ใหม่ให้ตาราง reservations ที่สร้างไว้ก่อนแล้ว
        # notify_group_id=กลุ่มที่ส่งการ์ด, reminded_at=เวลาเตือนซ้ำล่าสุด, resv_date=วันที่จริงของการจอง (YYYY-MM-DD)
        for _col in ("notify_group_id TEXT", "reminded_at TEXT", "resv_date TEXT"):
            if USE_PG:
                conn.execute(f"ALTER TABLE reservations ADD COLUMN IF NOT EXISTS {_col}")
            else:
                try:
                    conn.execute(f"ALTER TABLE reservations ADD COLUMN {_col}")
                except Exception:
                    pass
        conn.commit()

# พยายามสร้างตารางตอน start (best-effort) — ถ้า DB ยังไม่พร้อมก็ไม่เป็นไร _db() จะ init เองตอนใช้ครั้งแรก
try:
    _ensure_init()
    print(f"[STORAGE] ✅ ใช้ {STORAGE_DESC}", flush=True)
except Exception as e:
    print(f"🔴 [STORAGE] init ตอน start ยังไม่ได้ (จะลองใหม่ตอนมี request): {e}", flush=True)
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — AI Visual Analysis (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

def extract_slip_info(image_bytes: bytes, retry: bool = False) -> dict:
    retry_note = (
        "🔁 นี่คือการอ่าน 'รอบสอง' — รอบแรกตอบว่าไม่ใช่สลิป แต่รูปนี้ถูกส่งในกลุ่มรับสลิป "
        "จึงมีโอกาสสูงที่จะเป็นสลิป หรือมี 'สลิปโอน' อยู่ในรูป (อาจถ่ายคู่กับใบบิลร้าน/ถ่ายจากหน้าจอ/เอียง/เบลอ/แสงน้อย) "
        "โปรดเพ่งดูให้ละเอียดที่สุด ถ้าพอเห็นจำนวนเงิน + ร่องรอยการโอน/จ่ายสำเร็จ ให้ is_slip=true ไว้ก่อน\n\n"
    ) if retry else ""
    prompt = (
        retry_note +
        "ดูรูปนี้ว่าเป็นสลิป/หลักฐานการชำระเงินให้ร้านหรือไม่ "
        "(โอนผ่านธนาคาร/แอปธนาคาร หรือจ่ายผ่านเป๋าตัง/G-Wallet/รัฐช่วยจ่าย เช่น ไทยช่วยไทย คนละครึ่ง เราชนะ) "
        "แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_slip":true,"sender":null,"amount":0.00,"datetime":null,"bank":null,'
        '"account":null,"receiver":null,"receiver_account":null,"ref_number":null,"cropped":false,"fraud_score":0,"fraud_reasons":[]}\n\n'
        "is_slip: true ถ้าเป็นหลักฐานการชำระเงิน/โอนเงิน/เติมเงินสำเร็จ "
        "(สลิปโอนทุกธนาคาร, เติมเงินพร้อมเพย์/วอลเล็ต, เป๋าตัง/รัฐช่วยจ่าย). "
        "ตีความกว้างไว้ก่อน: เห็นจำนวนเงิน + คำว่าสำเร็จ/วันเวลา/เลขอ้างอิง แม้ถ่ายจากจอ/เอียง/เบลอ → true. "
        "false เฉพาะรูปที่ไม่ใช่การเงินชัดๆ (คน/อาหาร/เมนู/วิว/สติกเกอร์)\n"
        "ถ้ารูปมีทั้งใบบิลร้านและสลิปโอนคู่กัน → true และอ่านยอดจาก 'สลิปโอน' (ไม่ใช่ยอดบนบิลร้าน)\n"
        "สำคัญ: ถ้าเห็นจำนวนเงินบนรูป ให้อ่าน amount มาเสมอ แม้ไม่แน่ใจว่าเป็นสลิป (อย่าใส่ 0 ทั้งที่เห็นยอด)\n"
        "amount: จำนวนเงินที่ 'ร้านได้รับจริง' (อ่านจากสลิปโอน ไม่ใช่จากบิลร้าน)\n"
        "  - สลิปโอนธนาคารทั่วไป = ยอดที่โอน\n"
        "  - สลิปจ่ายผ่านรัฐช่วยจ่าย/เป๋าตัง (เช่น 'ไทยช่วยไทย' 'คนละครึ่ง' '60/40') ที่มีหลายยอด: "
        "ให้ใช้ยอดบรรทัด 'ค่าสินค้า/บริการ' (ยอดเต็มที่ร้านได้รับ) เท่านั้น "
        "ห้ามใช้ 'จำนวนเงินที่ชำระ' (ส่วนที่ลูกค้าจ่ายเอง มักติดลบ) หรือ 'จดบันทึก' (ส่วนที่รัฐช่วย)\n"

        "sender: ชื่อบัญชี 'ผู้โอน/ต้นทาง' (ฝั่ง 'จาก') ตามที่เห็นในสลิป\n"
        "receiver: ชื่อบัญชี 'ปลายทาง/ผู้รับเงิน' (ฝั่ง 'ไปยัง') ตามที่เห็นในสลิป (ถ้ามีข้อมูลเพิ่มในวงเล็บก็ใส่ด้วย)\n"
        "receiver_account: เลขบัญชีปลายทาง/ผู้รับ (ตามที่เห็น แม้ถูกปิดบางส่วน เช่น xxx-x-x4818-x ก็ใส่)\n"
        "bank: ธนาคาร 'ปลายทาง/ผู้รับเงิน' (ฝั่ง 'ไปยัง') เท่านั้น — ⚠️ ห้ามใส่ธนาคารของผู้โอน/ต้นทาง\n"
        "cropped: true ถ้าสลิปถูกตัด/ครอป หรือมีบางส่วนถูกบัง/นิ้วบัง จนข้อมูลสำคัญ (ปลายทาง/ยอดเงิน/เลขอ้างอิง) มองไม่เห็นครบ\n\n"
        "datetime: ดึงวันเวลาจากสลิป แปลงเป็น ค.ศ. รูปแบบ ISO YYYY-MM-DDTHH:MM:SS เสมอ "
        "(สลิปไทยใช้ พ.ศ. เช่น 2569 = ค.ศ. 2026 ให้ลบ 543)\n"
        "⚠️ ห้ามนำเรื่องวันที่ (ว่าเป็นอนาคตหรืออดีต) มาเป็นเหตุผล fraud โดยเด็ดขาด ไม่ว่ากรณีใดๆ — "
        "คุณไม่รู้วันที่ปัจจุบันที่แท้จริง ระบบจะตรวจวันที่เองด้วยโค้ดภายหลัง "
        "ให้ดึงวันที่ออกมาเฉยๆ และห้ามใส่เรื่องวันที่ใน fraud_reasons\n\n"
        "ตรวจ 'ร่องรอยการตัดต่อ' อย่างละเอียด เพ่งที่ตัวเลขจำนวนเงิน ชื่อผู้รับ และวันเวลาเป็นพิเศษ ดูจุดเหล่านี้:\n"
        "  - ตัวเลข/ตัวอักษรที่คมชัด เบลอ หรือพิกเซลไม่เข้ากับส่วนอื่นรอบๆ (ร่องรอยแปะทับ)\n"
        "  - ฟอนต์ ขนาด ความหนา หรือช่องไฟของตัวเลข ไม่สม่ำเสมอกับตัวเลขอื่นในสลิปเดียวกัน\n"
        "  - สีพื้นหลัง/เงา/noise รอบตัวเลขไม่กลมกลืน เหมือนถูก copy-paste หรือมีกล่องสีทับ\n"
        "  - ตัวอักษรซ้อนเหลื่อม เส้นบรรทัดเบี้ยว ขอบตัวเลขมีรอยถู/เกลี่ย\n"
        "❗ จุดที่ถูกปลอมบ่อยสุดคือ 'จำนวนเงิน' — เทียบ ขนาด/ฟอนต์/ความคม/ความหนา ของตัวเลขจำนวนเงิน "
        "กับตัวเลขอื่นในสลิป (เช่น เลขบัญชี วันเวลา) ถ้าจำนวนเงินขนาดหรือฟอนต์ต่างจากตัวเลขอื่นอย่างเห็นได้ชัด "
        "= ถูกแก้ไขแน่นอน ให้ fraud_score >= 80\n"
        "fraud_score: 0-100 — สำคัญมาก: 'false positive' (หาว่าสลิปจริงเป็นปลอม) แย่กว่าการปล่อยผ่าน "
        "เพราะทำให้ร้านปฏิเสธเงินลูกค้าจริง ดังนั้นถ้าไม่เห็นร่องรอยตัดต่อที่ 'ตัวเลขจำนวนเงิน' ชัดเจนจริงๆ ให้คะแนนต่ำเสมอ:\n"
        "  • 0-39 = ดูปกติ ไม่เห็นการตัดต่อ (สลิปจริงเกือบทั้งหมดต้องอยู่ช่วงนี้ = ค่าตั้งต้น)\n"
        "  • 40-69 = น่าสงสัยเล็กน้อยแต่ไม่ชัด\n"
        "  • 70-100 = เห็น 'ตัวเลขจำนวนเงิน' ถูกแก้ดิจิทัลชัดเจนเท่านั้น (ฟอนต์/ขนาด/แนววางของตัวเลขเงิน "
        "ต่างจากตัวเลขอื่นในสลิปอย่างเห็นได้ชัด หรือมีกล่อง/รอยแปะทับบนจำนวนเงิน)\n"
        "⛔ ห้ามให้คะแนนสูง 'เด็ดขาด' จากสิ่งเหล่านี้ (เป็นเรื่องปกติของสลิปจริง ไม่ใช่การปลอม):\n"
        "  - โลโก้/ไอคอน/แบรนด์/รูปแบบแอปธนาคาร (K+/KBIZ/SCB/ฯลฯ) — คุณตัดสิน 'โลโก้ปลอม' ไม่ได้ "
        "โลโก้ที่ดูแปลก/เบลอ/สีเพี้ยน เกิดจากความละเอียดรูปหรือการบีบอัด ห้ามใช้เป็นเหตุผล fraud โดยเด็ดขาด\n"
        "  - ลายน้ำ/พื้นหลัง/รูปตึก, ถ่ายจอเอียง/แสงสะท้อน/เบลอทั้งรูป, OCR ชื่อเพี้ยน, "
        "รูปแบบแอปธุรกิจต่างจากแอปบุคคล, สี/ฟอนต์โดยรวมของสลิป\n"
        "ถ้าไม่แน่ใจ หรือร่องรอยไม่ได้อยู่ที่ 'ตัวเลขจำนวนเงิน' โดยตรง → ให้ fraud_score < 40 เสมอ\n"
        "fraud_reasons: ระบุเฉพาะร่องรอยที่ 'ตัวเลขจำนวนเงิน' ถูกแก้ พร้อมตำแหน่ง (ห้ามพูดเรื่องโลโก้/แบรนด์) ถ้าไม่เจอให้เป็น []"
    )
    img_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    # _gemini_generate มี retry+backoff ในตัวแล้ว (กัน 503 overload) — เรียกครั้งเดียวพอ
    response = _gemini_generate([prompt, img_part])
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def extract_payable_doc(image_bytes: bytes) -> dict:
    """อ่านรูปในกลุ่มบัญชีเจ้าหนี้ → แยกว่าเป็น 'บิลซื้อ' หรือ 'สลิปโอนจ่าย' พร้อมยอดเงิน
    คืน JSON: doc_type=bill|payment|other, amount, ref_number(สำหรับสลิป), sender"""
    prompt = (
        f"รูปนี้อยู่ในกลุ่มซื้อ-ขายระหว่างร้านอาหารกับร้านขายเครื่องดื่ม/สุรา ({PAYABLE_VENDOR}) "
        "ช่วยดูว่าเป็นเอกสารแบบไหน แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"doc_type":"bill","amount":0.00,"ref_number":null,"sender":null,"doc_date":null}\n\n'
        "doc_type:\n"
        "  - 'payment' = สลิปโอนเงินผ่านแอปธนาคาร (มี 'โอนเงินสำเร็จ'/ผู้โอน→ผู้รับ/เลขที่รายการ/จำนวนเงิน) "
        "⚠️ ถือเป็น payment 'เสมอ' แม้มีโน้ต/ข้อความเขียนกำกับ เช่น '(6/6) ค้าง 14,153' (โน้ตพวกนี้ไม่ทำให้เป็นบิล)\n"
        "  - 'bill' = เอกสารที่มี 'รายการสินค้าหลายแถว + ราคา' (บิล/ใบส่งของ/ใบกำกับ/ใบเงินสดของร้านขายส่ง) เท่านั้น\n"
        "  - 'other' = อย่างอื่น (รูปคน/อาหาร/ข้อความ ฯลฯ)\n\n"
        "amount (สำคัญมาก — อ่านให้แม่น เพราะใช้คิดยอดหนี้):\n"
        "  • ถ้าเป็น 'bill': ใช้ 'ยอดรวมค่าสินค้าของบิลใบนี้' = ผลรวมราคาของทุกแถวสินค้า (จำนวน × ราคาต่อหน่วย)\n"
        "    ปกติตรงกับช่อง 'รวมเงิน' หรือ 'รวมทั้งสิ้น' — ให้ตรวจว่าตรงกับผลรวมแถวสินค้าด้วย\n"
        "    ⚠️ ห้ามนำ 'ยอดยกมา/ยอดค้างเก่า/ยอดค้างสะสม/ยอดรวมทั้งบัญชี' ที่พิมพ์บนบิลมาคิด — เอาเฉพาะค่าสินค้าของบิลใบนี้\n"
        "    ⚠️ บิลถ่ายเอียงตัวเลขอาจเลื่อนบรรทัด: ห้ามหยิบเลขจากช่อง 'ส่วนลด' มาเป็นยอด\n"
        "    ห้ามตอบ 0 ถ้ามีรายการสินค้าราคา>0 (เช่นช่อง 'ยอดเงินรับสุทธิ/รับเงินสด' เป็น 0.00 เพราะยังไม่จ่าย ก็ห้ามใช้)\n"
        "    ถ้ามีส่วนลดจริง (>0) ให้ใช้ยอดหลังหักส่วนลด\n"
        "  • ถ้าเป็น 'payment': ใช้ยอดเงินที่โอนจริง\n"
        "ref_number: เลขอ้างอิงรายการ (เฉพาะสลิปโอน ถ้าไม่มีใส่ null)\n"
        "sender: ชื่อผู้โอน/ชื่อบิล ถ้าอ่านได้ (ไม่มีใส่ null)\n"
        "doc_date: 'วันที่ล่าสุด/วันที่ออกเอกสารใบนี้' (บิล=วันที่ออกบิล, สลิป=วันที่โอน) แปลงเป็น ค.ศ. YYYY-MM-DD "
        "(พ.ศ. เช่น 2569 = ค.ศ. 2026 ให้ลบ 543). "
        "⚠️ ถ้าบนบิลมีหลายวันที่ (เช่นมีรายการ 'ยอดยกมา/ค้างเก่า' ที่เป็นวันก่อนๆ) ให้เลือก 'วันที่ใหม่สุด' เท่านั้น "
        "ห้ามใช้วันที่ของยอดยกมา/ค้างเก่า; ถ้าอ่านวันที่ไม่ได้ใส่ null\n"
        "ถ้า doc_type='other' ให้ amount=0"
    )
    img_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    response = _gemini_generate([prompt, img_part])
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — PromptPay Slip Verify API
# ══════════════════════════════════════════════════════════════════════════════

def verify_with_promptpay(image_bytes: bytes) -> dict:
    if not PROMPTPAY_API_KEY:
        return {"verified": None, "error": "ไม่ได้ตั้งค่า PROMPTPAY_API_KEY"}
    try:
        resp = requests.post(
            "https://api.slipok.com/api/line/apikey/verify",
            headers={"x-authorization": PROMPTPAY_API_KEY},
            files={"files": ("slip.jpg", image_bytes, "image/jpeg")},
            timeout=10,
        )
        data = resp.json()
        if data.get("success"):
            d = data.get("data", {})
            return {"verified": True, "api_amount": d.get("amount"),
                    "api_ref": d.get("transRef"), "error": None}
        return {"verified": False, "error": data.get("message", "API ตอบว่าสลิปไม่ผ่าน")}
    except Exception as e:
        return {"verified": None, "error": f"เรียก API ไม่ได้: {str(e)[:60]}"}


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — Duplicate Reference Number Check
# ══════════════════════════════════════════════════════════════════════════════

_dup_lock = threading.Lock()   # กัน race ตอนเช็คซ้ำ+บันทึก เมื่อหลาย thread ทำพร้อมกัน
_postback_lock = threading.Lock()   # กันกดปุ่มยืนยันซ้ำ (atomic check-then-mark)


def _postback_once(token: str) -> bool:
    """คืน True ถ้าเป็นการกดปุ่มครั้งแรก (ควรทำงาน), False ถ้ากดซ้ำ (เคยทำแล้ว)
    ใช้ meta เก็บถาวร + lock ให้ atomic → กันกดรัว/หลายคนกดพร้อมกันแล้วทำงานซ้ำ (ทนรีสตาร์ท/หลาย worker)"""
    if not token:
        return True
    with _postback_lock:
        if _get_meta(f"pb_done:{token}"):
            return False
        _set_meta(f"pb_done:{token}", datetime.now(TZ).date().isoformat())   # เก็บวันที่ไว้ให้ cleanup ลบทีหลัง
        return True

def find_duplicate(group_id: str, info: dict):
    """หาความซ้ำ/ความผิดปกติเทียบกับใบก่อนหน้าในกรุ๊ป — คืน (type, prev_amount)
    - ref_mismatch: เลขอ้างอิงเดียวกันแต่ยอดเงินไม่ตรง = ถูกตัดต่อ! (ฟันธง)
    - ref: เลขอ้างอิงตรง + ยอดตรง = สลิปซ้ำจริง
    - amount_time: ยอดเงิน + วันเวลาบนสลิปตรงกัน (กันกรณี ref อ่านเพี้ยน)"""
    ref    = info.get("ref_number")
    amount = float(info.get("amount") or 0)
    dt     = info.get("datetime")
    with _db() as conn:
        if ref:
            row = conn.execute(
                "SELECT amount FROM slips WHERE group_id=? AND ref_number=? ORDER BY id LIMIT 1",
                (group_id, ref)).fetchone()
            if row:
                prev = float(row["amount"] or 0)
                if amount and prev and abs(prev - amount) > 0.01:
                    return ("ref_mismatch", prev)
                return ("ref", None)
        if amount and dt:
            if conn.execute(
                "SELECT 1 FROM slips WHERE group_id=? AND amount=? AND slip_datetime=? LIMIT 1",
                (group_id, amount, dt)).fetchone():
                return ("amount_time", None)
    return (None, None)


# ══════════════════════════════════════════════════════════════════════════════
# Verdict Builder
# ══════════════════════════════════════════════════════════════════════════════

def _payee_matches(info: dict) -> bool:
    """ปลายทางบนสลิปตรงกับบัญชีร้านไหม (เทียบกับ PAYEE_KEYWORDS) — ถ้าไม่ตั้งค่าไว้ถือว่าผ่าน"""
    if not PAYEE_KEYWORDS:
        return True
    hay = f"{info.get('receiver') or ''} {info.get('receiver_account') or ''} {info.get('bank') or ''}".lower()
    return any(k in hay for k in PAYEE_KEYWORDS)


def _slip_account_label(info: dict):
    """จับว่าสลิปโอนเข้าบัญชีไหน (ตาม PAYEE_ACCOUNTS) — คืน label หรือ None ถ้าไม่ตรง/ไม่ได้ตั้งค่า"""
    if not PAYEE_ACCOUNTS:
        return None
    hay = f"{info.get('receiver') or ''} {info.get('receiver_account') or ''} {info.get('bank') or ''}".lower()
    for label, kws in PAYEE_ACCOUNTS:
        if any(k in hay for k in kws):
            return label
    return None


def _is_income(info: dict) -> bool:
    """สลิปนี้เป็น 'รายรับ' (โอนเข้าบัญชีร้าน) ไหม — ตรง PAYEE_KEYWORDS หรือ PAYEE_ACCOUNTS อย่างใดอย่างหนึ่ง"""
    hay = f"{info.get('receiver') or ''} {info.get('receiver_account') or ''} {info.get('bank') or ''}".lower()
    if any(k in hay for k in PAYEE_KEYWORDS):
        return True
    return _slip_account_label(info) is not None


def build_verdict(info: dict, promptpay: dict, dup_type=None, prev_amount=None) -> dict:
    issues = []
    fraud_score = info.get("fraud_score", 0)

    if fraud_score >= 70:
        issues.append(f"🔴 AI ตรวจพบร่องรอยปลอม (คะแนน {fraud_score}/100)")
        for r in info.get("fraud_reasons", []):
            issues.append(f"   • {r}")
    elif fraud_score >= 40:
        issues.append(f"🟡 AI พบจุดน่าสงสัย (คะแนน {fraud_score}/100)")
        for r in info.get("fraud_reasons", []):
            issues.append(f"   • {r}")

    if promptpay.get("verified") is False:
        issues.append(f"🔴 PromptPay API: ไม่ผ่านการตรวจสอบ ({promptpay.get('error','')})")
    elif promptpay.get("verified") is True:
        api_amt  = promptpay.get("api_amount")
        slip_amt = info.get("amount")
        if api_amt and slip_amt and abs(float(api_amt) - float(slip_amt)) > 0.01:
            issues.append(f"🔴 ยอดเงินไม่ตรง: สลิปแสดง {slip_amt} แต่ API พบ {api_amt} บาท")

    if dup_type == "ref_mismatch":
        cur_amt  = f"{float(info.get('amount') or 0):,.2f}"
        prev_amt = f"{float(prev_amount or 0):,.2f}"
        issues.append(f"🔴 เลขอ้างอิงเดียวกับสลิปใบก่อน แต่ยอดเงินไม่ตรง! "
                      f"(ใบก่อน {prev_amt} / ใบนี้ {cur_amt} บาท) → สลิปถูกตัดต่อ!")
    elif dup_type == "ref":
        issues.append(f"🔴 เลขอ้างอิง {info.get('ref_number')} เคยถูกส่งมาแล้ว! (สลิปซ้ำ)")
    elif dup_type == "amount_time":
        issues.append(f"🔴 ยอด {info.get('amount')} บาท + วันเวลาเดียวกับสลิปใบก่อนหน้า → น่าจะเป็นสลิปซ้ำ (โปรดตรวจสอบ)")

    # ข้อมูลไม่ครบ / ถูกตัด-บัง (เคสซ่อนปลายทาง ฯลฯ)
    receiver_missing = not (info.get("receiver") or info.get("receiver_account"))
    if info.get("cropped"):
        issues.append("🟡 สลิปอาจถูกตัด/ครอป หรือมีบางส่วนถูกบัง — โปรดตรวจสอบข้อมูลให้ครบ")
    if not info.get("amount"):
        issues.append("🟡 อ่านจำนวนเงินบนสลิปไม่ได้ — โปรดตรวจสอบ")

    # ปลายทาง: ถ้ามองไม่เห็น = อาจถูกบัง / ถ้าเห็นแต่ไม่ตรงบัญชีร้าน = อาจโอนผิดบัญชี
    if PAYEE_KEYWORDS and receiver_missing:
        issues.append("🟡 มองไม่เห็นบัญชีปลายทางในสลิป (อาจถูกตัด/บัง) — โปรดตรวจว่าโอนเข้าบัญชีร้านจริง")
    elif not _payee_matches(info):
        issues.append(f"🟡 บัญชีปลายทางไม่ตรงกับบัญชีร้าน ({info.get('receiver') or '?'} {info.get('receiver_account') or ''}) — โปรดตรวจสอบก่อนรับเงิน")

    critical = sum(1 for i in issues if i.startswith("🔴"))
    warning  = sum(1 for i in issues if i.startswith("🟡"))
    status   = "FAIL" if critical > 0 else ("WARN" if warning > 0 else "PASS")

    amount_str = f"{float(info.get('amount', 0)):,.2f}" if info.get("amount") else "?"
    sender = info.get("sender") or "?"
    dt     = info.get("datetime") or "?"
    bank   = info.get("bank") or ""
    receiver = info.get("receiver") or ""
    ref    = info.get("ref_number") or "-"

    # ปลายทาง = ชื่อผู้รับ + ธนาคารผู้รับ (อย่างใดอย่างหนึ่งหรือทั้งคู่ ถ้าไม่มีเลย = ?)
    dest = " / ".join(p for p in (receiver, bank) if p) or "?"

    base_info = (
        f"👤 ผู้โอน: {sender}\n"
        f"💰 จำนวน: {amount_str} บาท\n"
        f"📅 วันเวลา: {dt}\n"
        f"🏦 ปลายทาง: {dest}\n"
        f"🔖 อ้างอิง: {ref}"
    )

    if status == "PASS":
        api_note = " ✅ ผ่าน API" if promptpay.get("verified") else ""
        group_msg = f"✅ สลิปผ่านการตรวจสอบ{api_note}\n─────────────────\n{base_info}"
        admin_msg = None
    elif status == "WARN":
        issue_text = "\n".join(issues)
        group_msg  = (f"⚠️ สลิปผ่าน แต่มีจุดน่าสังเกต\n─────────────────\n{base_info}\n"
                      f"─────────────────\n{issue_text}\nกรุณาแจ้ง Admin ตรวจสอบเพิ่มเติม")
        # ไม่ DM admin สำหรับ WARN (กลุ่มเห็น alert ผ่าน reply อยู่แล้ว + ประหยัดโควต้า push)
        admin_msg  = None
    else:
        issue_text = "\n".join(issues)
        group_msg  = (f"🚨 ตรวจพบสลิปต้องสงสัย!\n─────────────────\n{base_info}\n"
                      f"─────────────────\n{issue_text}\n⛔ กรุณาอย่ายืนยันการรับเงินก่อนตรวจสอบ")
        admin_msg  = (f"🚨 [ALERT] สลิปปลอมจาก {sender}!\n"
                      f"จำนวน: {amount_str} บาท | อ้างอิง: {ref}\n─────────────────\n{issue_text}\n"
                      "กรุณาดำเนินการทันที")

    return {"status": status, "issues": issues, "group_msg": group_msg, "admin_msg": admin_msg}


# ══════════════════════════════════════════════════════════════════════════════
# Storage & Report
# ══════════════════════════════════════════════════════════════════════════════

def save_slip(group_id: str, info: dict, verdict_status: str, message_id: str = None):
    today       = datetime.now(TZ).date().isoformat()
    recorded_at = datetime.now(TZ).strftime("%H:%M:%S")
    with _db() as conn:
        conn.execute(
            "INSERT INTO slips (group_id, slip_date, sender, amount, bank, ref_number, slip_datetime, verdict, recorded_at, account_label, message_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, today, info.get("sender"), float(info.get("amount") or 0),
             info.get("bank"), info.get("ref_number"), info.get("datetime"), verdict_status, recorded_at,
             _slip_account_label(info), message_id)
        )
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()


def _slip_message_seen(group_id: str, message_id: str) -> bool:
    """รูปนี้ (message_id เดียวกัน) เคยถูกบันทึกเป็นสลิปในกรุ๊ปนี้แล้วหรือยัง — กัน LINE ส่ง webhook ซ้ำ/รีสตาร์ทแล้วนับซ้ำ"""
    if not message_id:
        return False
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM slips WHERE group_id=? AND message_id=? LIMIT 1",
            (group_id, message_id)).fetchone() is not None


def record_image_miss(group_id: str, reason: str):
    """บันทึกรูปที่รับเข้ามาแต่ไม่ได้กลายเป็นสลิป (reason='notslip' อ่านไม่ออก/ไม่ใช่สลิป, 'error' ประมวลผลพัง)
    ใช้กระทบยอด 'นับมือ' กับ 'บอทนับ' ในรายงาน — ห้าม throw ซ้อน (best-effort)"""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO image_misses (group_id, stat_date, reason, recorded_at) VALUES (?,?,?,?)",
                (group_id, datetime.now(TZ).date().isoformat(), reason,
                 datetime.now(TZ).strftime("%H:%M:%S"))
            )
            conn.commit()
    except Exception as e:
        print(f"[miss] record failed group={group_id}: {e}", flush=True)


def build_daily_report(group_id: str, report_date: str = None) -> str:
    report_date = report_date or datetime.now(TZ).date().isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM slips WHERE group_id=? AND slip_date=? ORDER BY id",
            (group_id, report_date)
        ).fetchall()
        miss_rows = conn.execute(
            "SELECT reason, COUNT(*) c FROM image_misses WHERE group_id=? AND stat_date=? GROUP BY reason",
            (group_id, report_date)
        ).fetchall()
    slips = [dict(r) for r in rows]
    miss_counts = {r["reason"]: r["c"] for r in miss_rows}
    misses    = sum(miss_counts.values())            # รูปที่รับมาแต่ไม่ได้บันทึกเป็นสลิป
    notincome = miss_counts.get("notincome", 0)      # ข้ามเพราะไม่ใช่รายรับ (ตั้งใจข้าม ไม่ใช่ตกหล่น)
    lost      = miss_counts.get("notslip", 0) + miss_counts.get("error", 0)  # ตกหล่นจริง
    received  = len(slips) + misses                  # รับรูปทั้งหมด (ไว้กระทบยอดนับมือ)

    # กระทบยอด: รับรูปกี่ใบ / อ่านเป็นสลิป(รายรับ)กี่ใบ / ตกหล่น / ข้ามไม่ใช่รายรับ
    def _recon_lines():
        if misses == 0:
            return []
        out = ["", "─────────────────",
               f"ℹ️ รับรูป {received} | บันทึกเป็นรายรับ {len(slips)} | ตกหล่น {lost} | ข้ามไม่ใช่รายรับ {notincome}"]
        if lost:
            detail = []
            if miss_counts.get("notslip"):
                detail.append(f"อ่านไม่ออก/ไม่ใช่สลิป {miss_counts['notslip']}")
            if miss_counts.get("error"):
                detail.append(f"ประมวลผลพลาด {miss_counts['error']}")
            out.append("   ตกหล่น: " + ", ".join(detail) + " — ตามดูใบที่บอทไม่ตอบ")
        return out

    if not slips:
        if misses:
            return ("\n".join([f"📊 รายงานสรุปประจำวัน {report_date}",
                               "─────────────────",
                               "ไม่มีสลิปที่อ่านได้"] + _recon_lines()))
        return f"📊 ไม่มีสลิปวันที่ {report_date}"

    total  = sum(float(s.get("amount") or 0) for s in slips)
    passed = sum(1 for s in slips if s.get("verdict") == "PASS")
    warned = sum(1 for s in slips if s.get("verdict") == "WARN")
    failed = sum(1 for s in slips if s.get("verdict") == "FAIL")
    pass_amt = sum(float(s.get("amount") or 0) for s in slips if s.get("verdict") == "PASS")
    warn_amt = sum(float(s.get("amount") or 0) for s in slips if s.get("verdict") == "WARN")
    fail_amt = sum(float(s.get("amount") or 0) for s in slips if s.get("verdict") == "FAIL")
    icons  = {"PASS": "✅", "WARN": "⚠️", "FAIL": "🚨"}

    lines = [
        f"📊 รายงานสรุปประจำวัน {report_date}",
        "─────────────────",
        f"รวมทั้งหมด {len(slips)} รายการ | {total:,.2f} บาท",
        f"✅ ผ่าน {passed} รายการ | {pass_amt:,.2f} บาท",
        f"⚠️ น่าสงสัย {warned} รายการ | {warn_amt:,.2f} บาท",
        f"🚨 ปลอม {failed} รายการ | {fail_amt:,.2f} บาท", "",
    ]
    # แยกยอดตามบัญชีรับเงิน (ไม่รวมสลิปปลอม) — เฉพาะเมื่อตั้ง PAYEE_ACCOUNTS
    if PAYEE_ACCOUNTS:
        acct = {}
        for s in slips:
            if s.get("verdict") == "FAIL":
                continue
            lbl = s.get("account_label") or "ไม่ระบุบัญชี"
            acct[lbl] = acct.get(lbl, 0.0) + float(s.get("amount") or 0)
        if acct:
            lines.append("💳 แยกตามบัญชี (ไม่รวมปลอม):")
            for lbl, amt in acct.items():
                lines.append(f"• {lbl}: {amt:,.2f} บาท")
            lines.append("")
    for i, s in enumerate(slips, 1):
        amt  = f"{float(s.get('amount', 0)):,.2f}" if s.get("amount") else "?"
        icon = icons.get(s.get("verdict", ""), "❓")
        lines.append(f"{i}. {icon} {s.get('sender','?')} | {amt} บาท | {s.get('recorded_at','')}")
    lines += _recon_lines()
    return "\n".join(lines)


def _resv_line(r: dict) -> str:
    icon = "✅" if r.get("status") == "CONFIRMED" else "⏳"
    cust = r.get("customer") or "?"
    ppl  = f" {r['people']}" if r.get("people") else ""
    when = f" | {r['resv_datetime']}" if r.get("resv_datetime") else ""
    zone = f" | {r['table_no']}" if r.get("table_no") else ""
    by   = f" (โดย {r['confirmed_by']})" if r.get("status") == "CONFIRMED" and r.get("confirmed_by") else ""
    return f"{icon} #{r['id']} {cust}{ppl}{when}{zone}{by}"


def build_resv_summary(notify_group=None, title="📋 สรุปการจองวันนี้", upcoming=False) -> str:
    """สรุปการจอง
    upcoming=False (ดีฟอลต์): เฉพาะจอง 'ของวันนี้' (resv_date = วันนี้) — จองล่วงหน้าจะโผล่เฉพาะวันที่ถึง
    upcoming=True: จองที่กำลังจะถึง (resv_date >= วันนี้) แยกหัวข้อ วันนี้/ล่วงหน้า — ใช้กับ digest ล่วงหน้า
    notify_group=None → ทุกการจอง / ระบุ group → เฉพาะการจองที่ส่งการ์ดเข้ากลุ่มนั้น"""
    today  = datetime.now(TZ).date().isoformat()
    if upcoming:
        # "วันงานจริง" ต้อง >= วันนี้ — ใช้ resv_date ถ้ามี ไม่งั้น fallback วันที่แจ้ง (กันจองที่เลยวันไปแล้วค้างในรายการล่วงหน้า)
        date_cond = "(COALESCE(resv_date, substr(created_at,1,10)) >= ?)"
        params = [today]
    else:
        # วันนี้เท่านั้น: resv_date=วันนี้ (จองล่วงหน้าโผล่เฉพาะวันถึง) + จองที่ไม่มีวันชัดแต่แจ้งวันนี้
        date_cond = "((resv_date = ?) OR (resv_date IS NULL AND substr(created_at,1,10) = ?))"
        params = [today, today]
    q = f"SELECT * FROM reservations WHERE {date_cond}"
    if notify_group:
        q += " AND notify_group_id=?"
        params.append(notify_group)
    q += " ORDER BY resv_date IS NULL, resv_date, created_at"
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if not rows:
        return f"{title}\n─────────────────\nไม่มีการจอง"
    confirmed = sum(1 for r in rows if r.get("status") == "CONFIRMED")
    lines = [title, "─────────────────",
             f"ทั้งหมด {len(rows)} | ✅ คอนเฟิร์ม {confirmed} | ⏳ รอ {len(rows) - confirmed}"]
    if upcoming:
        today_rows = [r for r in rows if r.get("resv_date") == today]
        later_rows = [r for r in rows if r.get("resv_date") != today]
        if today_rows:
            lines += ["", f"📍 วันนี้ ({len(today_rows)})"] + [_resv_line(r) for r in today_rows]
        if later_rows:
            lines += ["", f"📅 ล่วงหน้า ({len(later_rows)})"] + [_resv_line(r) for r in later_rows]
    else:
        lines += [""] + [_resv_line(r) for r in rows]
    return "\n".join(lines)


def build_advance_resv_report() -> str:
    """digest จองล่วงหน้าที่กำลังจะถึง (เข้ากลุ่มบาร์น้ำ) — ใช้ตอนรายงาน 00:30"""
    return build_resv_summary(BAR_GROUP_ID, "📅 จองล่วงหน้าที่กำลังจะถึง", upcoming=True)


def _sane_doc_date(doc_date):
    """รับวันที่ที่ AI อ่านจากเอกสารเฉพาะที่ 'สมเหตุผล' — ไม่เป็นอนาคต และไม่เก่าเกิน PAYABLE_DATE_MAX_DAYS วัน
    กัน AI อ่านวันที่บนบิลเพี้ยน (หยิบเลขที่บิล/วันครบกำหนดมาเป็นวันที่) → คืน None = ให้ใช้วันที่ส่ง(วันนี้)แทน"""
    if not _valid_ymd(doc_date):
        return None
    try:
        d = datetime.strptime(str(doc_date), "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now(TZ).date()
    if d > today or (today - d).days > PAYABLE_DATE_MAX_DAYS:
        return None
    return doc_date


# ══════════════════════════════════════════════════════════════════════════════
# ระบบที่ 3 — บัญชีเจ้าหนี้การค้า (ดวงใจการสุรา): บิลซื้อ(+) / เงินจ่าย(−) / ยอดค้าง
# หมายเหตุ: ห้าม cleanup ตาราง payable_* (เป็นบัญชีเดินสะสม ลบแถวเก่า = ยอดเพี้ยน)
# ══════════════════════════════════════════════════════════════════════════════

def _payable_account_key(group_id: str) -> str:
    """คีย์บัญชี (ที่เก็บข้อมูลจริง) ของกลุ่มนี้ — ถ้าเป็นกลุ่ม mirror ให้ใช้คีย์ของ primary (บัญชีเดียวกัน)"""
    return _PAYABLE_PRIMARY_OF.get(group_id, group_id)


def _payable_output_group(group_id: str) -> str:
    """กลุ่มที่ควร 'เด้งผล' บิล/สลิป — ถ้ากลุ่มนี้เป็น primary ที่มี mirror → ส่งไป mirror (primary เงียบ); ไม่งั้นตอบกลุ่มเดิม"""
    return PAYABLE_MIRROR.get(group_id, group_id)


def _payable_send(event, source_group: str, dest_group: str, text: str):
    """ส่งผลบันทึกบิล/สลิป — ถ้าปลายทางต่างจากกลุ่มต้นทาง ให้ push (กลุ่มต้นทางเงียบ) ไม่งั้น reply ตามปกติ"""
    try:
        if dest_group and dest_group != source_group:
            line_bot_api.push_message(dest_group, TextSendMessage(text=text))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
    except Exception as e:
        print(f"[payable] ส่งผลไม่สำเร็จ dest={dest_group}: {e}", flush=True)


def _payable_opening(group_id: str) -> float:
    """ยอดค้างยกมา (ตั้งครั้งเดียวตอนเริ่ม) ของกลุ่มนี้"""
    v = _get_meta(f"payable_opening:{group_id}")
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0

def _payable_sum(table: str, group_id: str, before_date=None, on_date=None) -> float:
    """รวมยอดบิล/เงินจ่ายของกลุ่ม — เลือกกรองทั้งหมด / ก่อนวันที่ / เฉพาะวันที่"""
    q = f"SELECT COALESCE(SUM(amount),0) s FROM {table} WHERE group_id=?"
    params = [group_id]
    if before_date is not None:
        q += " AND doc_date < ?"; params.append(before_date)
    if on_date is not None:
        q += " AND doc_date = ?"; params.append(on_date)
    with _db() as conn:
        return float(conn.execute(q, tuple(params)).fetchone()["s"] or 0)

def _payable_outstanding(group_id: str) -> float:
    """ยอดค้างจ่ายสะสมปัจจุบัน = ยกมา + บิลรวมทั้งหมด − จ่ายรวมทั้งหมด"""
    return (_payable_opening(group_id)
            + _payable_sum("payable_bills", group_id)
            - _payable_sum("payable_payments", group_id))

def _parse_thai_date(token: str):
    """แปลง 'd/m', 'd/m/yy', 'd/m/yyyy' → ISO 'YYYY-MM-DD' (คืน None ถ้าผิดรูป)
    - ไม่ใส่ปี → ใช้ปีปัจจุบัน (ถ้าวันที่กลายเป็นอนาคต ถอยไปปีก่อน — กันเคสข้ามปี)
    - ปี 2 หลัก หรือ ≥2400 → ถือเป็น พ.ศ. แปลงเป็น ค.ศ. ให้อัตโนมัติ"""
    parts = [p.strip() for p in token.strip().split("/")]
    if len(parts) < 2 or len(parts) > 3:
        return None
    try:
        day, month = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    today = datetime.now(TZ).date()
    has_year = len(parts) == 3 and parts[2] != ""
    if has_year:
        try:
            year = int(parts[2])
        except ValueError:
            return None
        if year < 100:        # เลข 2 หลัก → พ.ศ. ย่อ 25xx
            year = 2500 + year - 543
        elif year >= 2400:    # พ.ศ. เต็ม
            year = year - 543
        # ไม่งั้นถือเป็น ค.ศ. เต็มอยู่แล้ว
    else:
        year = today.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not has_year and d > today:    # ไม่ระบุปีแต่ดันเป็นอนาคต → ปีก่อน
        try:
            d = date(year - 1, month, day)
        except ValueError:
            return None
    return d.isoformat()

def _payable_bill_exists(group_id: str, doc_date: str, amount: float) -> bool:
    """มีบิลวันที่+ยอดเดียวกันอยู่แล้วไหม — กันวางบล็อก 'ค้าง' ซ้ำแล้วนับเบิ้ล"""
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM payable_bills WHERE group_id=? AND doc_date=? AND ABS(amount-?)<0.01 LIMIT 1",
            (group_id, doc_date, float(amount))).fetchone() is not None


def save_payable_bill(group_id: str, amount: float, note: str = None, doc_date: str = None) -> int:
    doc_date = doc_date or datetime.now(TZ).date().isoformat()
    now   = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        rid = conn.insert_returning_id(
            "INSERT INTO payable_bills (group_id, doc_date, amount, note, recorded_at) VALUES (?,?,?,?,?)",
            (group_id, doc_date, float(amount), note, now))
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()
    return rid

def save_payable_payment(group_id: str, amount: float, sender=None, ref_number=None, slip_dt=None, doc_date: str = None) -> int:
    doc_date = doc_date or datetime.now(TZ).date().isoformat()
    now   = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        rid = conn.insert_returning_id(
            "INSERT INTO payable_payments (group_id, doc_date, amount, sender, ref_number, slip_datetime, recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (group_id, doc_date, float(amount), sender, ref_number, slip_dt, now))
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()
    return rid

def _payment_ref_exists(group_id: str, ref_number: str) -> bool:
    if not ref_number:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM payable_payments WHERE group_id=? AND ref_number=? LIMIT 1",
            (group_id, ref_number)).fetchone()
    return row is not None

def build_payable_summary(group_id: str, date_str=None) -> str:
    """สรุปยอดค้างจ่าย 'รายวัน': ยกเข้า + บิลวันนั้น − จ่ายวันนั้น = ค้างสิ้นวัน"""
    d = date_str or datetime.now(TZ).date().isoformat()
    opening      = _payable_opening(group_id)
    bills_before = _payable_sum("payable_bills", group_id, before_date=d)
    pays_before  = _payable_sum("payable_payments", group_id, before_date=d)
    carry_in     = opening + bills_before - pays_before
    bills_today  = _payable_sum("payable_bills", group_id, on_date=d)
    pays_today   = _payable_sum("payable_payments", group_id, on_date=d)
    carry_out    = carry_in + bills_today - pays_today
    with _db() as conn:
        n_bill = conn.execute("SELECT COUNT(*) c FROM payable_bills WHERE group_id=? AND doc_date=?", (group_id, d)).fetchone()["c"]
        n_pay  = conn.execute("SELECT COUNT(*) c FROM payable_payments WHERE group_id=? AND doc_date=?", (group_id, d)).fetchone()["c"]
        n_unread = conn.execute("SELECT COUNT(*) c FROM image_misses WHERE group_id=? AND stat_date=? AND reason='payable_unread'", (group_id, d)).fetchone()["c"]
    msg = (
        f"📊 สรุปหนี้ {PAYABLE_VENDOR}\n"
        f"📅 ประจำวันที่ {d}\n"
        "─────────────────\n"
        f"ยอดยกเข้า: {carry_in:,.2f} บาท\n"
        f"📥 บิลซื้อวันนี้: +{bills_today:,.2f} ({n_bill} รายการ)\n"
        f"💸 จ่ายวันนี้: −{pays_today:,.2f} ({n_pay} รายการ)\n"
        "─────────────────\n"
        f"💰 ค้างจ่ายสะสม: {carry_out:,.2f} บาท"
    )
    if n_unread:
        msg += f"\n⚠️ มีรูปที่อ่านไม่ออก/ยังไม่ได้บันทึก {n_unread} ใบ — โปรดส่งใหม่หรือพิมพ์ยอดเอง"
    return msg


def build_payable_ledger(acct: str, with_total: bool = True) -> str:
    """สรุปหนี้แบบ 'รายวัน' — แยก 📥 บิลซื้อ และ 💸 จ่าย เป็นรายวันคนละส่วน
    with_total=True → ใส่ยอดรวม (รวมซื้อ/รวมจ่าย/ค้างจ่ายสะสม) ท้ายแต่ละส่วน (กลุ่ม mirror)
    with_total=False → ไม่ใส่ยอดรวมเลย แสดงเฉพาะรายวัน (กลุ่ม primary)"""
    with _db() as conn:
        bill_rows = conn.execute(
            "SELECT doc_date, COALESCE(SUM(amount),0) s, COUNT(*) c FROM payable_bills "
            "WHERE group_id=? GROUP BY doc_date ORDER BY doc_date", (acct,)).fetchall()
        pay_rows = conn.execute(
            "SELECT doc_date, COALESCE(SUM(amount),0) s, COUNT(*) c FROM payable_payments "
            "WHERE group_id=? GROUP BY doc_date ORDER BY doc_date", (acct,)).fetchall()
    opening = _payable_opening(acct)
    bar = "━━━━━━━━━━━━━"

    def fdate(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%y")
        except (ValueError, TypeError):
            return d or "-"

    if not bill_rows and not pay_rows and not opening:
        return f"📋 สรุปหนี้ {PAYABLE_VENDOR} — รายวัน\n{bar}\nยังไม่มีรายการ"

    lines = [f"📋 สรุปหนี้ {PAYABLE_VENDOR} — รายวัน"]
    if opening:
        lines += [bar, f"📌 ยอดยกมา   {opening:,.2f}"]

    # ── บิลซื้อ (เพิ่มหนี้) รายวัน ──
    lines += [bar, "📥 บิลซื้อ (รายวัน)"]
    if bill_rows:
        bsum = 0.0
        for r in bill_rows:
            amt = float(r["s"] or 0); bsum += amt
            cnt = f"  ×{r['c']}" if (r["c"] or 0) > 1 else ""
            lines.append(f"  {fdate(r['doc_date'])}   +{amt:,.2f}{cnt}")
        if with_total:
            lines.append(f"  รวมซื้อ   {bsum:,.2f}")
    else:
        lines.append("  — ไม่มี")

    # ── จ่าย (ลดหนี้) รายวัน ──
    lines += [bar, "💸 ชำระจ่าย (รายวัน)"]
    if pay_rows:
        psum = 0.0
        for r in pay_rows:
            amt = float(r["s"] or 0); psum += amt
            cnt = f"  ×{r['c']}" if (r["c"] or 0) > 1 else ""
            lines.append(f"  {fdate(r['doc_date'])}   −{amt:,.2f}{cnt}")
        if with_total:
            lines.append(f"  รวมจ่าย   {psum:,.2f}")
    else:
        lines.append("  — ไม่มี")

    if with_total:
        lines += [bar, f"💰 ค้างจ่ายสะสม   {_payable_outstanding(acct):,.2f} บาท"]
    return "\n".join(lines)


def _payable_report_recipients(acct: str):
    """คืน [(group, with_total), ...] ที่ต้องส่งสรุปหนี้รายวันของบัญชี acct ตอนตี1
    - บัญชี mirror: ส่ง 2 กลุ่ม → primary (ไม่ใส่ยอดรวม) + mirror (ใส่ยอดรวม)
    - บัญชีปกติ: ส่งกลุ่มเดียว (ใส่ยอดรวม)"""
    if acct in PAYABLE_MIRROR:
        return [(acct, False), (PAYABLE_MIRROR[acct], True)]
    return [(acct, True)]


_PAYABLE_SRC = {"block": "บล็อกค้าง", "พิมพ์": "พิมพ์เอง", "รูป": "รูปบิล"}

def build_payable_bill_list(acct: str, limit: int = 40) -> str:
    """รายการบิลซื้อ — โชว์ #id | วันที่ | ยอด | มาจากไหน | เวลาที่ลง (ไว้เช็กที่มาของแต่ละบิล)"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, doc_date, amount, note, recorded_at FROM payable_bills "
            "WHERE group_id=? ORDER BY doc_date, id LIMIT ?", (acct, limit)).fetchall()
    if not rows:
        return "📋 ยังไม่มีบิลซื้อในบัญชีนี้"
    lines = [f"📋 รายการบิลซื้อ ({len(rows)} ใบ)", "━━━━━━━━━━━━━"]
    for r in rows:
        try:
            d = datetime.strptime(r["doc_date"], "%Y-%m-%d").strftime("%d/%m/%y")
        except (ValueError, TypeError):
            d = r["doc_date"] or "-"
        src = _PAYABLE_SRC.get(r["note"], r["note"] or "ไม่ระบุ")
        lines.append(f"#{r['id']}  {d}  {float(r['amount'] or 0):,.2f}  [{src}]  ลง {r['recorded_at'] or '-'}")
    return "\n".join(lines)


def _process_payable_image(event, group_id: str):
    """รูปในกลุ่มเจ้าหนี้ → AI แยกบิล(เพิ่มหนี้)/สลิปจ่าย(ลดหนี้) แล้วบันทึก + ตอบยอดค้างสะสม
    บัญชีเดียว 2 กลุ่ม (mirror): เก็บข้อมูลที่ acct (primary), เด้งผลที่ out (mirror) — กลุ่ม primary เงียบ"""
    acct = _payable_account_key(group_id)   # ที่เก็บข้อมูลจริง (บัญชีเดียวกันสำหรับ primary/mirror)
    out  = _payable_output_group(group_id)  # ที่เด้งผล (primary→mirror, ไม่งั้นกลุ่มเดิม)
    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        info        = extract_payable_doc(image_bytes)
    except Exception as e:
        print(f"[payable] อ่านรูปไม่สำเร็จ group={group_id}: {e}", flush=True)
        _payable_send(event, group_id, out, "⚠️ อ่านรูปไม่สำเร็จ กรุณาส่งใหม่อีกครั้ง")
        return

    doc_type = (info.get("doc_type") or "other").lower()
    amount   = float(info.get("amount") or 0)
    doc_date = _sane_doc_date(info.get("doc_date"))   # ใช้วันที่บนเอกสารเฉพาะที่สมเหตุผล (กัน AI อ่านเพี้ยน) ไม่งั้น = วันนี้

    if doc_type == "bill":
        if amount <= 0:
            record_image_miss(acct, "payable_unread")
            _payable_send(event, group_id, out,
                "🟡 อ่านยอดเงินบนบิลไม่ได้ — โปรดถ่ายให้ชัดแล้วส่งใหม่ หรือพิมพ์ 'บิล <ยอด>' เอง")
            return
        eff_date = doc_date or datetime.now(TZ).date().isoformat()
        if _payable_bill_exists(acct, eff_date, amount):   # กันส่งบิลซ้ำ (วันที่+ยอดเดียวกันมีแล้ว)
            _payable_send(event, group_id, out,
                f"🔁 บิลนี้ (วันที่ {eff_date} ยอด {amount:,.2f}) เคยบันทึกแล้ว — ไม่นับซ้ำ")
            return
        rid = save_payable_bill(acct, amount, note="รูป", doc_date=doc_date)
        _payable_send(event, group_id, out,
            f"📥 บันทึกบิลซื้อ #{rid}\nยอด {amount:,.2f} บาท\n─────────────────\n"
            f"💰 ค้างจ่าย {PAYABLE_VENDOR} สะสม: {_payable_outstanding(acct):,.2f} บาท")
        return

    if doc_type == "payment":
        if amount <= 0:
            record_image_miss(acct, "payable_unread")
            _payable_send(event, group_id, out,
                "🟡 อ่านยอดเงินบนสลิปไม่ได้ — โปรดถ่ายให้ชัดแล้วส่งใหม่")
            return
        ref = info.get("ref_number")
        if _payment_ref_exists(acct, ref):
            _payable_send(event, group_id, out,
                f"🔁 สลิปนี้ (อ้างอิง {ref}) เคยบันทึกแล้ว — ไม่นับซ้ำ")
            return
        rid = save_payable_payment(acct, amount, sender=info.get("sender"),
                                   ref_number=ref, slip_dt=info.get("datetime"), doc_date=doc_date)
        _payable_send(event, group_id, out,
            f"💸 บันทึกจ่าย {PAYABLE_VENDOR} #{rid}\nยอด {amount:,.2f} บาท\n─────────────────\n"
            f"💰 ค้างจ่ายสะสม: {_payable_outstanding(acct):,.2f} บาท")
        return

    # other → อ่านไม่ออกว่าเป็นบิล/สลิป — ห้ามทิ้งเงียบ (กันบัญชีคลาดเคลื่อน) ตอบเตือน + นับไว้ให้เห็นในสรุป
    record_image_miss(acct, "payable_unread")
    print(f"[payable] รูปไม่ใช่บิล/สลิป group={group_id}", flush=True)
    _payable_send(event, group_id, out,
        "🟡 อ่านรูปนี้ไม่ออกว่าเป็นบิลซื้อหรือสลิปจ่าย — ยังไม่ได้บันทึก\n"
        "ถ้าเป็นบิล/สลิปจริง โปรดถ่ายให้ชัดแล้วส่งใหม่ หรือพิมพ์ 'บิล <ยอด>' เอง")


def handle_payable_text(event, text: str, group_id: str) -> bool:
    """คำสั่งบัญชีเจ้าหนี้ในกลุ่ม PAYABLE_GROUPS — คืน True ถ้าจัดการแล้ว
    บัญชีเดียว 2 กลุ่ม (mirror): ทุกคำสั่งทำงานบน acct (บัญชีร่วม) แต่ตอบในกลุ่มที่พิมพ์"""
    low = text.lower().strip()
    acct = _payable_account_key(group_id)   # บัญชีร่วม (primary/mirror ใช้ข้อมูลชุดเดียวกัน)
    out  = _payable_output_group(group_id)  # ผลการ 'บันทึก' เด้งที่ไหน (primary→mirror เงียบในกลุ่มต้นทาง)

    # บล็อก 'ค้าง' = บันทึกบิลค้างแต่ละวัน → รวมทุกบรรทัด = ยอดหนี้ค้างตั้งต้น (ยอดยกมา)
    #   ค้าง
    #   6/6=24153 (ค้าง 14153)   ← มีวงเล็บ = จ่ายบางส่วน ใช้เลขในวงเล็บ
    #   8/6=18504                ← ไม่มีวงเล็บ ใช้เลขปกติ
    # วางบล็อกใหม่ = แทนที่ยอดยกมาเดิม (ไม่บวกเพิ่ม)
    lines = text.splitlines()
    if len(lines) > 1 and lines[0].strip().lower() in ("ค้าง", "ยอดค้าง", "รายการค้าง", "บิลค้าง"):
        total, n, errors = 0.0, 0, []
        for ln in lines[1:]:
            ln = ln.strip()
            if not ln or "=" not in ln:
                continue   # ข้ามเส้นคั่น/บรรทัดว่าง (ไม่มี '=')
            dpart, apart = ln.split("=", 1)
            if _parse_thai_date(dpart.strip()) is None:
                errors.append(ln); continue
            # มีวงเล็บ → ใช้ยอดในวงเล็บ (ค้างจริงหลังจ่ายบางส่วน); ไม่มี → ใช้ยอดปกติ (เลขก่อนวงเล็บ)
            paren = re.search(r"\(([^)]*)\)", apart)
            target = paren.group(1) if paren else apart.split("(")[0]
            num = re.search(r"\d[\d,]*(?:\.\d+)?", target)
            if not num:
                errors.append(ln); continue
            try:
                val = float(num.group(0).replace(",", ""))
            except ValueError:
                errors.append(ln); continue
            if val < 0:
                errors.append(ln); continue
            total += val; n += 1
        if errors:
            print(f"[payable-import] ข้าม {len(errors)} บรรทัด: {errors[:5]}", flush=True)
        if n == 0:
            _payable_send(event, group_id, out, "❌ อ่านบล็อกค้างไม่ได้ — รูปแบบควรเป็น  วัน/เดือน=ยอด (ค้าง xxxx)")
            return True
        _set_meta(f"payable_opening:{acct}", str(total))   # แทนที่ยอดยกมา (วางบล็อกใหม่ทับของเก่า ไม่เบิ้ล)
        _payable_send(event, group_id, out,
            f"📌 ตั้งยอดค้างยกมา = {total:,.2f} บาท (รวม {n} รายการ)\n"
            f"ℹ️ บล็อกค้าง = รวมทุกบรรทัด (มีวงเล็บใช้เลขในวงเล็บ) → เป็นยอดตั้งต้น แทนที่ของเดิม\n"
            "─────────────────\n"
            f"💰 ค้างจ่าย {PAYABLE_VENDOR} สะสม: {_payable_outstanding(acct):,.2f} บาท")
        return True

    # ตั้งยอดยกมา (ค้างเก่า) — ครั้งเดียวตอนเริ่ม
    if low.startswith("ตั้งยอดยกมา") or low.startswith("ยอดยกมา") or low.startswith("ตั้งยอดค้าง") or low.startswith("ค้างเก่า"):
        for kw in ("ตั้งยอดยกมา", "ตั้งยอดค้าง", "ยอดยกมา", "ค้างเก่า"):
            if low.startswith(kw):
                arg = text[len(kw):].strip().replace(",", "").replace("บาท", "").strip()
                break
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"📌 ยอดยกมาปัจจุบัน: {_payable_opening(acct):,.2f} บาท\n"
                     "ตั้งใหม่พิมพ์: ตั้งยอดยกมา 12000"))
            return True
        try:
            val = float(arg)
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="❌ ใส่ตัวเลขไม่ถูก เช่น: ตั้งยอดยกมา 12000"))
            return True
        _set_meta(f"payable_opening:{acct}", str(val))
        _payable_send(event, group_id, out,
            f"✅ ตั้งยอดค้างยกมา = {val:,.2f} บาท\n─────────────────\n"
            f"💰 ค้างจ่าย {PAYABLE_VENDOR} สะสม: {_payable_outstanding(acct):,.2f} บาท")
        return True

    # บันทึกบิลด้วยข้อความ (เผื่อไม่มีรูป) เช่น "บิล 3500" หรือย้อนวันที่ "บิล 6/6 24153"
    if low.startswith("บิล"):
        rest = text[3:].strip()
        if not rest:
            return False
        toks = rest.split()
        doc_date = None
        if toks and "/" in toks[0]:                    # โทเคนแรกเป็นวันที่ → ลงย้อนหลัง
            doc_date = _parse_thai_date(toks[0])
            if doc_date is None:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="❌ วันที่ไม่ถูก เช่น: บิล 6/6 24153 (วัน/เดือน ยอด)"))
                return True
            amount_str = " ".join(toks[1:])
        else:
            amount_str = rest
        amount_str = amount_str.replace(",", "").replace("บาท", "").strip()
        try:
            val = float(amount_str)
        except ValueError:
            return False
        rid = save_payable_bill(acct, val, note="พิมพ์", doc_date=doc_date)
        date_note = f"\n📅 ลงวันที่ {doc_date}" if doc_date else ""
        _payable_send(event, group_id, out,
            f"📥 บันทึกบิลซื้อ #{rid}\nยอด {val:,.2f} บาท{date_note}\n─────────────────\n"
            f"💰 ค้างจ่ายสะสม: {_payable_outstanding(acct):,.2f} บาท")
        return True

    # สรุปหนี้ (วันนี้ / ตามวันที่)
    if low in ("สรุปหนี้", "ยอดค้าง", "หนี้", f"หนี้{PAYABLE_VENDOR}".lower(), "สรุปยอดค้าง"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_payable_summary(acct)))
        return True
    if low.startswith("สรุปหนี้ "):
        arg = text.split(" ", 1)[1].strip()
        try:
            datetime.strptime(arg, "%Y-%m-%d")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_payable_summary(acct, arg)))
            return True
        except ValueError:
            pass

    # ดูรายการบิลซื้อ + ที่มา (เช็กว่าบิลก่อนวันที่ X มาจากไหน: บล็อกค้าง/พิมพ์/รูป + เวลาที่ลง)
    if low in ("รายการบิล", "ดูบิล", "บิลทั้งหมด", "เช็กบิล"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_payable_bill_list(acct)))
        return True

    # ลบทุกบิล/จ่ายของวันที่ที่ระบุ เช่น "ลบวันที่ 6/6" — ใช้เคลียร์รายการที่ลงผิดวัน
    if low.startswith("ลบวันที่"):
        arg = text[len("ลบวันที่"):].strip()
        d = _parse_thai_date(arg)
        if not d:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="❌ วันที่ไม่ถูก เช่น: ลบวันที่ 6/6"))
            return True
        with _db() as conn:
            nb = conn.execute("DELETE FROM payable_bills WHERE group_id=? AND doc_date=?", (acct, d)).rowcount
            np = conn.execute("DELETE FROM payable_payments WHERE group_id=? AND doc_date=?", (acct, d)).rowcount
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"🗑️ ลบรายการวันที่ {d}: บิล {nb} + จ่าย {np} รายการ\n─────────────────\n"
                 f"💰 ค้างจ่ายสะสม: {_payable_outstanding(acct):,.2f} บาท"))
        return True

    # ทดสอบ: force ส่ง 'สรุปหนี้รายวัน' เข้ากลุ่มผู้รับเดี๋ยวนี้ (เหมือนตี1) — เช็คการส่ง/mirror/ยอดรวม
    if low in ("ทดสอบรายงานหนี้", "ทดสอบสรุปหนี้", "ทดสอบรายงาน", "force payable"):
        sent, errs = [], []
        for grp, with_total in _payable_report_recipients(acct):
            try:
                line_bot_api.push_message(grp, TextSendMessage(text=build_payable_ledger(acct, with_total)))
                sent.append(f"{grp} ({'มียอดรวม' if with_total else 'ไม่มียอดรวม'})")
            except Exception as e:
                errs.append(f"{grp}: {e}")
        msg = f"✅ ส่งสรุปหนี้รายวันแล้ว {len(sent)} กลุ่ม"
        if errs:
            msg += "\n❌ ส่งไม่สำเร็จ:\n" + "\n".join(errs)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return True

    # ลบรายการล่าสุด (แก้บันทึกผิด)
    if low in ("ลบบิลล่าสุด", "ลบบิล"):
        _delete_last_payable(event, acct, "payable_bills", "บิลซื้อ")
        return True
    if low in ("ลบจ่ายล่าสุด", "ลบจ่าย", "ลบสลิปล่าสุด"):
        _delete_last_payable(event, acct, "payable_payments", "เงินจ่าย")
        return True

    # ล้างบัญชีหนี้ทั้งหมด (รีเซ็ตเริ่มนับใหม่) — มีปุ่มยืนยัน
    if low in ("ล้างบัญชีหนี้", "ล้างหนี้", "ล้างบัญชี", "รีเซ็ตหนี้", "ล้างทั้งหมด"):
        send_reset_payable_confirm(event, acct)
        return True
    return False


def _delete_last_payable(event, group_id: str, table: str, label: str):
    with _db() as conn:
        row = conn.execute(f"SELECT id, amount FROM {table} WHERE group_id=? ORDER BY id DESC LIMIT 1",
                           (group_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📭 ไม่มี{label}ให้ลบ"))
            return
        conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
        conn.commit()
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🗑️ ลบ{label}ล่าสุด #{row['id']} ({float(row['amount']):,.2f} บาท) แล้ว\n─────────────────\n"
             f"💰 ค้างจ่ายสะสม: {_payable_outstanding(group_id):,.2f} บาท"))


def _cleanup_old_data():
    """ลบข้อมูลเก่าที่ไม่จำเป็น (วันละครั้ง) กัน DB บวม:
    - จองที่ผ่านวันมาแล้วเกิน RESV_KEEP_DAYS (ใช้ resv_date ถ้ามี ไม่งั้นใช้วันที่แจ้ง)
    - ตัวนับรูปตกหล่น (image_misses) เก่าเกิน MISS_KEEP_DAYS
    - สลิป (slips) เก่าเกิน SLIP_KEEP_DAYS"""
    today = datetime.now(TZ).date()
    resv_cutoff = (today - timedelta(days=RESV_KEEP_DAYS)).isoformat()
    miss_cutoff = (today - timedelta(days=MISS_KEEP_DAYS)).isoformat()
    slip_cutoff = (today - timedelta(days=SLIP_KEEP_DAYS)).isoformat()
    try:
        with _db() as conn:
            n_resv = conn.execute(
                "DELETE FROM reservations WHERE COALESCE(resv_date, substr(created_at,1,10)) < ?",
                (resv_cutoff,)).rowcount
            n_miss = conn.execute("DELETE FROM image_misses WHERE stat_date < ?", (miss_cutoff,)).rowcount
            n_slip = conn.execute("DELETE FROM slips WHERE slip_date < ?", (slip_cutoff,)).rowcount
            # ตัวจำ 'ส่งรายงานรายกลุ่มแล้ว' (sent:job:date:target) เก็บแค่ของวันนี้พอ — ของเก่าทิ้งกัน meta บวม
            conn.execute("DELETE FROM meta WHERE key LIKE 'sent:%' AND key NOT LIKE ?", (f"sent:%:{today.isoformat()}:%",))
            # ตัวกันกดปุ่มยืนยันซ้ำ (pb_done:*) เก่าเกิน 30 วัน → ลบทิ้ง (ปุ่มหมดอายุไปนานแล้ว)
            conn.execute("DELETE FROM meta WHERE key LIKE 'pb_done:%' AND value < ?",
                         ((today - timedelta(days=30)).isoformat(),))
            conn.commit()
        if n_resv or n_miss or n_slip:
            print(f"[cleanup] ลบจองเก่า {n_resv} + ตัวนับรูปเก่า {n_miss} + สลิปเก่า {n_slip}", flush=True)
    except Exception as e:
        print(f"[cleanup] error: {e}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Daily Report Scheduler (00:30 Asia/Bangkok) — ยิงจากการ ping /health + thread สำรอง (idempotent กันรีสตาร์ท/ส่งซ้ำ)
# ══════════════════════════════════════════════════════════════════════════════

_report_lock = threading.Lock()

def _get_meta(key: str):
    with _db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None

def _set_meta(key: str, value: str):
    with _db() as conn:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def _del_meta(key: str):
    with _db() as conn:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
        conn.commit()


def _already_sent(job: str, date: str, target: str) -> bool:
    """ส่ง job นี้ให้ target นี้ในวันนี้ไปแล้วหรือยัง (กันส่งซ้ำ 'รายกลุ่ม')"""
    return _get_meta(f"sent:{job}:{date}:{target}") == "1"

def _mark_sent(job: str, date: str, target: str):
    _set_meta(f"sent:{job}:{date}:{target}", "1")


# ── กลุ่มที่บอทถูกเตะออก/ส่งไม่ได้ (กันสแปม+กันค้าง retry) — มาร์คเมื่อ LeaveEvent หรือ push เจอ 400 ──
def _group_left(gid: str) -> bool:
    return _get_meta(f"left:{gid}") == "1"

def _mark_group_left(gid: str):
    """บอทไม่ได้อยู่ในกลุ่มนี้แล้ว → เลิกส่งรายงาน/สรุปจนกว่าจะกลับเข้ากลุ่ม"""
    _set_meta(f"left:{gid}", "1")
    print(f"[group] mark LEFT {gid} — เลิกส่งจนกว่าจะกลับเข้ากลุ่ม", flush=True)

def _clear_group_left(gid: str):
    """กลับเข้ากลุ่มแล้ว (มีข้อความ/ถูกเชิญใหม่) → ปลดมาร์ค กลับมาส่งปกติ (self-heal)"""
    if _group_left(gid):
        _del_meta(f"left:{gid}")
        print(f"[group] clear LEFT {gid} — กลับมาส่งปกติ", flush=True)

def _is_not_member_error(e: Exception) -> bool:
    """push เจอ 400 'Failed to send messages' = บอทไม่ได้อยู่ในกลุ่มนั้น (ต่างจาก 429/500 ที่เป็นชั่วคราว)"""
    return isinstance(e, LineBotApiError) and getattr(e, "status_code", None) == 400


def _report_dest(group_id: str) -> str:
    """ปลายทางจริงของรายงานกลุ่มนี้ — ถ้าตั้ง REPORT_REDIRECT ไว้ ให้ส่งไปกลุ่มปลายทางแทนกลุ่มต้นทาง"""
    return REPORT_REDIRECT.get(group_id, group_id)


def maybe_send_daily_report():
    """ส่งรายงานสรุป 'ของเมื่อวาน' เมื่อเลย 00:30 เวลาไทย วันละครั้ง
    เรียกได้บ่อย (จากทุก /health ping + thread สำรอง) — กันส่งซ้ำด้วย last_report_date + lock จึงทนรีสตาร์ท/หลาย thread
    สำคัญ: มาร์ค last_report_date *หลังส่งสำเร็จ* เท่านั้น ถ้า push พลาด (quota/เน็ต) จะ retry รอบ ping ถัดไปแทนที่จะเงียบทั้งวัน"""
    now = datetime.now(TZ)
    if (now.hour, now.minute) < (0, 30):
        return
    today = now.date().isoformat()
    # ถือ lock ตลอดการส่ง เพื่อกันสอง thread ผ่านเช็ค "ยังไม่ส่ง" พร้อมกันแล้วส่งซ้ำ (ping ถี่ ๆ)
    with _report_lock:
        if _get_meta("last_report_date") == today:
            return
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        with _db() as conn:
            group_ids = [r["group_id"] for r in conn.execute("SELECT group_id FROM groups").fetchall()]
        # ข้ามกลุ่มที่บอทถูกเตะออก (_group_left) / สั่งเมิน (IGNORE_GROUPS) — กันค้าง retry/สแปม
        targets = [g for g in group_ids
                   if ((not SLIP_GROUPS) or g in SLIP_GROUPS) and not _group_left(g)
                   and g not in IGNORE_GROUPS and g not in PAYABLE_GROUPS]
        print(f"[report] trigger {today} → ส่งรายงานวันที่ {yesterday} ให้ {len(targets)} กลุ่ม", flush=True)
        failed = 0
        # idempotent รายกลุ่ม: กลุ่มที่ส่งสำเร็จแล้ววันนี้จะไม่ส่งซ้ำ แม้กลุ่มอื่นพลาดแล้วต้อง retry
        for group_id in targets:
            if _already_sent("report", today, group_id):
                continue
            dest = _report_dest(group_id)   # อาจ redirect ไปกลุ่มอื่น (เนื้อหายังเป็นของ group_id)
            if _group_left(dest) or dest in IGNORE_GROUPS:
                continue
            try:
                line_bot_api.push_message(dest, TextSendMessage(text=build_daily_report(group_id, yesterday)))
                _mark_sent("report", today, group_id)
                print(f"[report] sent OK → {dest}" + (f" (redirect จาก {group_id})" if dest != group_id else ""), flush=True)
            except Exception as e:
                if _is_not_member_error(e):
                    _mark_group_left(dest)   # บอทไม่ได้อยู่ในกลุ่มปลายทางแล้ว → เลิก retry (ไม่นับเป็น fail)
                else:
                    failed += 1
                print(f"[report] push FAILED {dest}: {e}", flush=True)
        # สรุปจองล่วงหน้า → กลุ่มบาร์น้ำ (วันละครั้งพร้อมรายงานสลิป)
        if BAR_GROUP_ID and not _group_left(BAR_GROUP_ID) and not _already_sent("advance_resv", today, BAR_GROUP_ID):
            try:
                line_bot_api.push_message(BAR_GROUP_ID, TextSendMessage(text=build_advance_resv_report()))
                _mark_sent("advance_resv", today, BAR_GROUP_ID)
                print(f"[report] sent advance-resv → {BAR_GROUP_ID}", flush=True)
            except Exception as e:
                if _is_not_member_error(e):
                    _mark_group_left(BAR_GROUP_ID)
                else:
                    failed += 1
                print(f"[report] push FAILED advance-resv {BAR_GROUP_ID}: {e}", flush=True)
        # มาร์คว่า 'ส่งครบแล้ว' เฉพาะเมื่อไม่มีอันไหนล้มเหลว — ถ้ามี fail ปล่อยไว้ให้ ping รอบหน้า retry
        if failed == 0:
            _set_meta("last_report_date", today)
            _cleanup_old_data()          # ลบข้อมูลเก่าวันละครั้ง กัน DB บวม
            print(f"[report] done {today}", flush=True)
        else:
            print(f"[report] {failed} กลุ่มส่งไม่สำเร็จ — จะ retry รอบ ping ถัดไป (~5 นาที)", flush=True)


def maybe_send_resv_summary():
    """ส่งสรุปการจองวันนี้ เข้ากลุ่มบาร์น้ำ + กลุ่มรับจอง ในกรอบ 16:00–18:59 วันละครั้ง (idempotent)
    ถ้าไม่มีจองจะส่ง 'ไม่มีการจอง' ตามที่ขอ; เลย 19:00 แล้วยังไม่ส่ง = ข้ามวันนี้ (กันส่งดึกไม่มีประโยชน์)"""
    now = datetime.now(TZ)
    hm  = (now.hour, now.minute)
    if hm < (16, 0):
        return
    today = now.date().isoformat()
    with _report_lock:
        if _get_meta("last_resv_summary_date") == today:
            return
        if hm >= (19, 0):
            # เลยกรอบเวลาส่งแล้ว (เช่น บอทเพิ่งตื่น/deploy ดึก) → มาร์คข้ามวันนี้ ไม่ส่งสรุปดึก
            _set_meta("last_resv_summary_date", today)
            print("[resv-summary] เลย 19:00 แล้ว ข้ามวันนี้ (พรุ่งนี้ส่ง 16:00)", flush=True)
            return
        # ข้ามกลุ่มที่บอทถูกเตะออก (_group_left) / สั่งเมิน (IGNORE_GROUPS) — กันค้าง retry/สแปม
        targets = list(dict.fromkeys(
            [t for t in ([BAR_GROUP_ID] + list(RESV_GROUPS))
             if t and not _group_left(t) and t not in IGNORE_GROUPS]))
        if not targets:
            _set_meta("last_resv_summary_date", today)
            return
        text = build_resv_summary(None, "📋 สรุปการจองวันนี้ (16:00)")
        failed = 0
        # idempotent รายกลุ่ม: กลุ่มที่ส่งสำเร็จแล้ววันนี้จะไม่ส่งซ้ำ แม้กลุ่มอื่นพลาดแล้วต้อง retry
        for g in targets:
            if _already_sent("resv_summary", today, g):
                continue
            dest = _report_dest(g)
            if _group_left(dest) or dest in IGNORE_GROUPS:
                continue
            try:
                line_bot_api.push_message(dest, TextSendMessage(text=text))
                _mark_sent("resv_summary", today, g)
                print(f"[resv-summary] sent → {dest}" + (f" (redirect จาก {g})" if dest != g else ""), flush=True)
            except Exception as e:
                if _is_not_member_error(e):
                    _mark_group_left(dest)
                else:
                    failed += 1
                print(f"[resv-summary] FAILED {dest}: {e}", flush=True)
        if failed == 0:
            _set_meta("last_resv_summary_date", today)
            print(f"[resv-summary] done {today}", flush=True)
        else:
            print(f"[resv-summary] {failed} กลุ่มส่งไม่สำเร็จ — retry รอบหน้า", flush=True)


def maybe_send_payable_summary():
    """ส่งสรุปหนี้ 'ของเมื่อวาน' เข้ากลุ่มเจ้าหนี้ เมื่อเลย PAYABLE_SUMMARY_HOUR (ดีฟอลต์ ตี1) วันละครั้ง
    idempotent ด้วย last_payable_summary_date + รายกลุ่ม → ทนรีสตาร์ท/หลาย thread"""
    if not PAYABLE_GROUPS:
        return
    now = datetime.now(TZ)
    if now.hour < PAYABLE_SUMMARY_HOUR:
        return
    today = now.date().isoformat()
    with _report_lock:
        if _get_meta("last_payable_summary_date") == today:
            return
        failed = 0
        seen_acct = set()   # ส่งสรุปรายวันครั้งเดียวต่อบัญชี → กระจายให้กลุ่มผู้รับ (mirror: 2 กลุ่ม)
        for g in PAYABLE_GROUPS:
            acct = _payable_account_key(g)
            if acct in seen_acct:
                continue
            seen_acct.add(acct)
            for grp, with_total in _payable_report_recipients(acct):
                if _group_left(grp) or grp in IGNORE_GROUPS:
                    continue
                if _already_sent("payable_summary", today, grp):   # idempotent รายกลุ่มผู้รับ
                    continue
                try:
                    line_bot_api.push_message(grp, TextSendMessage(text=build_payable_ledger(acct, with_total)))
                    _mark_sent("payable_summary", today, grp)
                    print(f"[payable-summary] sent → {grp} (acct {acct}, total={with_total})", flush=True)
                except Exception as e:
                    if _is_not_member_error(e):
                        _mark_group_left(grp)
                    else:
                        failed += 1
                    print(f"[payable-summary] FAILED {grp}: {e}", flush=True)
        if failed == 0:
            _set_meta("last_payable_summary_date", today)
            print(f"[payable-summary] done {today}", flush=True)
        else:
            print(f"[payable-summary] {failed} กลุ่มส่งไม่สำเร็จ — retry รอบหน้า", flush=True)


def _report_backup_loop():
    """thread สำรอง — เผื่อ UptimeRobot ไม่ ping; เช็คทุก ~5 นาที"""
    while True:
        time.sleep(300)
        try:
            maybe_send_resv_summary()
            maybe_send_daily_report()
            maybe_send_payable_summary()
        except Exception as e:
            print(f"[report] backup loop error: {e}", flush=True)


threading.Thread(target=_report_backup_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# Table Reservation Alerts (แจ้งเตือนจองโต๊ะ)
# ══════════════════════════════════════════════════════════════════════════════

# คำที่เป็นไปได้ว่าเกี่ยวกับการจอง (เกตเบื้องต้นแบบประหยัด ก่อนส่งให้ AI ตัดสินจริง)
_RESV_HINTS = ("จอง", "โต๊ะ", "table", "reserve", "booking", "ลูกค้า")


def get_display_name(source) -> str:
    """ดึงชื่อผู้ส่ง/ผู้กดปุ่มจาก LINE (กรุ๊ปหรือแชทเดี่ยว) — ล้มเหลวก็คืน 'ไม่ทราบชื่อ'"""
    try:
        user_id = getattr(source, "user_id", None)
        if not user_id:
            return "ไม่ทราบชื่อ"
        if source.type == "group":
            return line_bot_api.get_group_member_profile(source.group_id, user_id).display_name
        if source.type == "room":
            return line_bot_api.get_room_member_profile(source.room_id, user_id).display_name
        return line_bot_api.get_profile(user_id).display_name
    except Exception as e:
        print(f"[resv] get_display_name failed: {e}", flush=True)
        return "ไม่ทราบชื่อ"


_TH_WD = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

def extract_reservation(text: str) -> dict:
    """ให้ Gemini ตัดสินว่าข้อความเป็นการจองโต๊ะหรือไม่ แล้วดึงรายละเอียด (รวมวันที่จริง resv_date)"""
    now = datetime.now(TZ)
    today_str = now.date().isoformat()
    weekday_th = _TH_WD[now.weekday()]
    prompt = (
        f"วันนี้คือ วัน{weekday_th} ที่ {today_str} (เวลาไทย)\n"
        "ข้อความต่อไปนี้จากแชทพนักงานร้าน เป็นการ 'จองโต๊ะ' ให้ลูกค้าหรือไม่ "
        "(ไม่ใช่การคุยเล่นที่บังเอิญมีคำว่าจอง/โต๊ะ) ตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_reservation":true,"is_advance":false,"customer":null,"people":null,"date":null,"resv_date":null,'
        '"datetime":null,"time_hhmm":null,"table":null,"note":null}\n\n'
        "is_reservation: true เฉพาะเมื่อเป็นการจองโต๊ะจริง (มีเจตนานัด/จองที่นั่งให้ลูกค้า)\n"
        "is_advance: true ถ้าเป็นการจอง 'ล่วงหน้า' (สำหรับวันอื่น/วันข้างหน้า เช่น พรุ่งนี้ เสาร์นี้ วันที่ 25 ฯลฯ), "
        "false ถ้าจองสำหรับ 'วันนี้/คืนนี้/ตอนนี้'\n"
        "customer: ชื่อลูกค้า/ผู้จอง (ถ้ามี)\n"
        "people: จำนวนคน เช่น '4 คน' (ถ้ามี)\n"
        "date: วันที่จองแบบอ่านง่าย เช่น 'วันนี้','พรุ่งนี้','เสาร์นี้','25 มิ.ย.' — จองวันนี้/คืนนี้ใส่ 'วันนี้'; "
        "ถ้าเป็นจองล่วงหน้าแต่ไม่ระบุว่าวันไหน ให้เป็น null\n"
        f"resv_date: วันที่จองเป็นตัวเลข YYYY-MM-DD คำนวณจากวันนี้ ({today_str}) เช่น "
        "จองวันนี้/คืนนี้=วันนี้, พรุ่งนี้=+1วัน, มะรืน=+2วัน, 'เสาร์นี้/ศุกร์นี้ฯลฯ'=วันนั้นที่จะถึงถัดไป, "
        "'วันที่ 25'=วันที่ 25 เดือนนี้ (ถ้าผ่านแล้วเป็นเดือนหน้า); ถ้าไม่ระบุวันให้ null\n"
        "datetime: วันและเวลาที่จองแบบอ่านง่าย เช่น 'วันนี้ 20:00', 'เสาร์นี้ 19:00' (ถ้ามี)\n"
        "time_hhmm: เฉพาะ 'เวลา' ในรูปแบบ 24 ชม. 'HH:MM' (เช่น '2 ทุ่ม'→'20:00', 'บ่าย 3'→'15:00', "
        "'หกโมงเย็น'→'18:00', '19.00'→'19:00', '1 ทุ่มครึ่ง'→'19:30') ถ้าไม่ระบุเวลาให้เป็น null\n"
        "table: โซน/โต๊ะที่นั่ง เช่น 'โซน A', 'ริมน้ำ', 'ห้องแอร์', 'โต๊ะ 5' (ถ้ามี) — "
        "ถ้าระบุเป็นรหัส 'ตัวอักษร+ตัวเลข' เช่น 'A12','B5','C3' ให้ตีความว่า ตัวอักษรหน้า=โซน เลข=เลขโต๊ะ "
        "แล้วใส่เป็น 'โซน A โต๊ะ 12' (จาก A12)\n"
        "note: รายละเอียดเพิ่มเติมอื่นๆ (ถ้ามี)\n"
        "ฟิลด์ที่ไม่มีข้อมูลให้เป็น null\n\n"
        f"ข้อความ: {text}"
    )
    response = _gemini_generate(prompt)
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _resv_detail_lines(r: dict) -> str:
    # รองรับทั้ง dict จาก AI (datetime/table) และแถวจาก DB (resv_datetime/table_no)
    cust = r.get("customer")
    ppl  = r.get("people")
    dt   = r.get("datetime") or r.get("resv_datetime")
    zone = r.get("table") or r.get("table_no")
    note = r.get("note")
    parts = []
    if cust: parts.append(f"👤 ลูกค้า: {cust}")
    if ppl:  parts.append(f"👥 จำนวน: {ppl}")
    if dt:   parts.append(f"🕗 เวลา: {dt}")
    if zone: parts.append(f"🪑 โซน: {zone}")
    if note: parts.append(f"📝 หมายเหตุ: {note}")
    return "\n".join(parts) if parts else "(ไม่มีรายละเอียดเพิ่มเติม)"


def _parse_hhmm(s):
    """แปลง 'HH:MM' หรือ 'HH.MM' → จำนวนนาทีตั้งแต่เที่ยงคืน (None ถ้าแปลงไม่ได้)"""
    if not s:
        return None
    try:
        parts = str(s).replace(".", ":").split(":")
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hh <= 24 and 0 <= mm < 60:
            return hh * 60 + mm
        return None
    except Exception:
        return None


def _within_open_hours(tmin: int) -> bool:
    """ร้านเปิด 11:00–00:00 → เวลาที่ใช้ได้คือ 11:00–23:59 และ 00:00 (เที่ยงคืน=ปิดพอดี)"""
    if tmin is None:
        return True
    return tmin == 0 or (OPEN_HOUR * 60 <= tmin <= CLOSE_HOUR * 60 - 1)


def save_reservation(origin_group_id: str, requested_by: str, info: dict, raw_text: str,
                     notify_group_id: str) -> int:
    created_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    resv_date = info.get("resv_date") if _valid_ymd(info.get("resv_date")) else None
    with _db() as conn:
        rid = conn.insert_returning_id(
            "INSERT INTO reservations "
            "(origin_group_id, requested_by, customer, people, resv_datetime, table_no, note, raw_text, "
            "status, created_at, notify_group_id, reminded_at, resv_date) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (origin_group_id, requested_by, info.get("customer"), info.get("people"),
             info.get("datetime"), info.get("table"), info.get("note"), raw_text,
             "PENDING", created_at, notify_group_id, created_at, resv_date)
        )
        conn.commit()
        return rid


def _valid_ymd(s) -> bool:
    if not s:
        return False
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return True
    except Exception:
        return False


def get_reservation(resv_id: int) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM reservations WHERE id=?", (resv_id,)).fetchone()
    return dict(row) if row else None


def mark_reservation_confirmed(resv_id: int, confirmed_by: str):
    confirmed_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE reservations SET status='CONFIRMED', confirmed_by=?, confirmed_at=? WHERE id=?",
            (confirmed_by, confirmed_at, resv_id)
        )
        conn.commit()


def handle_reservation_text(event, text: str, group_id: str):
    """จองวันนี้ → การ์ด+ปุ่มในกลุ่มเดิม (เฉพาะกลุ่มใน RESV_GROUPS)
    จองล่วงหน้า → ส่งการ์ด+ปุ่มไปกลุ่มบาร์น้ำ (BAR_GROUP_ID) จากกลุ่มไหนก็ได้
    ยกเว้นกลุ่มใน RESV_EXCLUDE_GROUPS → ไม่ยุ่งกับการจองเลย"""
    if group_id in RESV_EXCLUDE_GROUPS:
        return False
    in_resv = bool(RESV_GROUPS) and group_id in RESV_GROUPS
    # ทำงานถ้า: เป็นกลุ่มรับจองวันนี้ หรือ ตั้งบาร์น้ำไว้ (เพื่อรับจองล่วงหน้าจากทุกกลุ่ม)
    if not in_resv and not BAR_GROUP_ID:
        return False
    if not any(h in text.lower() for h in _RESV_HINTS):
        return False

    print(f"[resv] เช็คจอง group={group_id} text={text}", flush=True)
    try:
        info = extract_reservation(text)
    except Exception as e:
        print(f"[resv] extract failed: {e}", flush=True)
        return False
    if not info.get("is_reservation"):
        print(f"[resv] AI ว่าไม่ใช่การจอง → ข้าม", flush=True)
        return False

    is_advance = bool(info.get("is_advance"))
    # กลุ่มไม่ได้เปิดรับจองวันนี้ และไม่ใช่จองล่วงหน้า → ไม่ทำอะไร
    if not in_resv and not is_advance:
        print(f"[resv] กลุ่มไม่อยู่ใน RESV_GROUPS และไม่ใช่จองล่วงหน้า → ข้าม", flush=True)
        return False

    requested_by = get_display_name(event.source)
    # จองให้ตัวเอง: ไม่มีชื่อลูกค้า แต่ใช้สรรพนามแทนตัว (กู/ผม/ฉัน/เรา/หนู) → ใช้ชื่อคนแจ้งจองเป็นชื่อลูกค้า
    if not info.get("customer") and any(w in text for w in ("กู", "ผม", "ฉัน", "เรา", "หนู", "ข้า", "ชั้น", "ตัวเอง")):
        info["customer"] = requested_by

    # ── เช็คข้อมูลให้ครบ: 1.ชื่อ 2.จำนวนคน 3.เวลา 4.โซน — ถ้าขาดให้ถามกลับ ยังไม่บันทึก ──
    missing = []
    if not info.get("customer"):  missing.append("1. ชื่อผู้จอง")
    if not info.get("people"):    missing.append("2. จำนวนคน")
    # วันและเวลา: ต้องได้ 'วันที่จริง' (resv_date) — ถ้า AI แปลงวันไม่ได้/ไม่มั่นใจ ให้ถามวันที่ให้ชัด
    when_missing = []
    if not _valid_ymd(info.get("resv_date")): when_missing.append("วันที่ (ระบุให้ชัด เช่น พรุ่งนี้/25 มิ.ย.)")
    if not info.get("time_hhmm"):             when_missing.append("เวลา")
    if when_missing:                          missing.append("3. " + " + ".join(when_missing))
    if not info.get("table"):     missing.append("4. โซน")
    if missing:
        print(f"[resv] ข้อมูลไม่ครบ ขาด {missing} → ถามกลับ", flush=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="📝 ขอข้อมูลจองเพิ่มครับ ยังขาด:\n" + "\n".join(missing) +
                 "\n\nรบกวนแจ้งใหม่ให้ครบ: ชื่อ / จำนวนคน / วันเวลา / โซน"))
        return True

    # ── เช็คเวลาจองอยู่ในเวลาเปิดร้าน (11:00–00:00) ไหม — นอกเวลาแค่ 'เตือน' ไม่บล็อก ──
    tmin = _parse_hhmm(info.get("time_hhmm"))
    time_warn = "" if _within_open_hours(tmin) else \
        f"\n⚠️ เวลา {info.get('time_hhmm')} อยู่นอกเวลาเปิดร้าน (11:00–00:00) โปรดตรวจสอบ"

    # ปลายทาง: จองล่วงหน้า → กลุ่มบาร์น้ำ / จองวันนี้ → กลุ่มเดิม
    dest = BAR_GROUP_ID if (is_advance and BAR_GROUP_ID) else group_id
    resv_id = save_reservation(group_id, requested_by, info, text, dest)
    detail = _resv_detail_lines(info)
    head = "📅 จองล่วงหน้า" if is_advance else "🔔 จองโต๊ะ"

    detail_msg = TextSendMessage(
        text=f"{head}ใหม่ #{resv_id}\nจากคุณ {requested_by}\n─────────────────\n{detail}{time_warn}")
    confirm_msg = _resv_confirm_card(resv_id, head)

    print(f"[resv] จอง #{resv_id} advance={is_advance} → ส่งไป {dest}", flush=True)
    if dest == group_id:
        line_bot_api.reply_message(event.reply_token, [detail_msg, confirm_msg])
    else:
        line_bot_api.push_message(dest, [detail_msg, confirm_msg])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"📤 ส่งจองล่วงหน้า #{resv_id} ไปกลุ่มบาร์น้ำแล้ว รอคอนเฟิร์ม\n─────────────────\n{detail}{time_warn}"))
    return True


def _resv_confirm_card(resv_id: int, head: str = "🔔 จองโต๊ะ") -> TemplateSendMessage:
    """การ์ดปุ่มคอนเฟิร์มการจอง (ใช้ทั้งตอนจองใหม่และตอนแจ้งเตือนซ้ำ)
    ไม่ใส่ display_text เพราะ LINE จะ echo ทุกครั้งที่กด ทำให้ตอนกดซ้ำดูเหมือนคอนเฟิร์มสำเร็จ"""
    return TemplateSendMessage(
        alt_text=f"ยืนยันการจอง #{resv_id}",
        template=ButtonsTemplate(
            title=f"{head} #{resv_id}",
            text="กดยืนยันเมื่อรับจองเรียบร้อย",
            actions=[PostbackAction(
                label="✅ คอนเฟิร์มการจอง",
                data=f"confirm_resv:{resv_id}",
            )],
        ),
    )


def handle_reservation_confirm(event, resv_id: int):
    resv = get_reservation(resv_id)
    if not resv:
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text=f"❓ ไม่พบการจอง #{resv_id}"))
        return

    confirmer = get_display_name(event.source)
    detail = _resv_detail_lines(resv)
    pressed_group = getattr(event.source, "group_id", getattr(event.source, "user_id", None))

    if resv["status"] == "CONFIRMED":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"⚠️ การจอง #{resv_id} ถูกคอนเฟิร์มไปแล้ว!\n"
                 f"โดย {resv['confirmed_by']} ({resv['confirmed_at']})\n"
                 f"ไม่ต้องกดซ้ำครับ"))
        return

    mark_reservation_confirmed(resv_id, confirmer)
    # ตอบในกลุ่มที่กดปุ่มคอนเฟิร์ม
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"✅ การจอง #{resv_id} คอนเฟิร์มแล้ว!\nโดย {confirmer}\n─────────────────\n"
             f"ผู้แจ้งจอง: {resv.get('requested_by','-')}\n{detail}"))

    # แจ้งกลับกลุ่มต้นทาง ถ้าต่างจากกลุ่มที่กด (เคสล่วงหน้า: บาร์น้ำกด → แจ้งกลับกลุ่มที่แจ้ง)
    # ถ้าต้นทาง = กลุ่มที่กด (จองในกลุ่มเดียวกัน) → ไม่ต้องแจ้งซ้ำ
    origin = resv.get("origin_group_id")
    if origin and origin != pressed_group:
        try:
            line_bot_api.push_message(origin, TextSendMessage(
                text=f"✅ จองล่วงหน้า #{resv_id} ที่แจ้งไว้ บาร์น้ำคอนเฟิร์มแล้ว!\n"
                     f"โดย {confirmer}\n─────────────────\n{detail}"))
        except Exception as e:
            print(f"[resv] notify origin failed: {e}", flush=True)


def _touch_reminded(resv_id: int, when: str):
    with _db() as conn:
        conn.execute("UPDATE reservations SET reminded_at=? WHERE id=?", (when, resv_id))
        conn.commit()


def _reservation_reminder_loop():
    """แจ้งเตือนการจองที่ยัง PENDING ซ้ำในกลุ่มที่ส่งการ์ดไว้
    ทุก 5 นาทีช่วง 18:00–22:00 / ทุก 15 นาทีเวลาอื่น จนกว่าจะคอนเฟิร์ม (หรือเกิน RESV_NAG_MAX_HOURS)"""
    while True:
        time.sleep(60)
        try:
            now      = datetime.now(TZ)
            interval = 5 if (18 <= now.hour < 22) else 15      # นาที
            try:
                with _db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM reservations WHERE status=?", ("PENDING",)).fetchall()]
            except Exception:
                continue   # storage ยังไม่พร้อม → ข้ามรอบนี้
            for r in rows:
                notify = r.get("notify_group_id")
                if not notify:
                    continue
                if notify in IGNORE_GROUPS or _group_left(notify):
                    continue   # กลุ่มที่สั่งเมิน/บอทถูกเตะออก → ไม่ย้ำเตือน
                last = r.get("reminded_at") or r.get("created_at")
                created = r.get("created_at")
                try:
                    last_dt    = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    created_dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                except Exception:
                    continue
                # เกินเวลาตามตื๊อ → หยุดแจ้ง (กันแจ้งไม่รู้จบ)
                if RESV_NAG_MAX_HOURS and (now - created_dt).total_seconds() > RESV_NAG_MAX_HOURS * 3600:
                    continue
                if (now - last_dt).total_seconds() < interval * 60:
                    continue
                try:
                    line_bot_api.push_message(notify, [
                        TextSendMessage(text=f"⏰ ย้ำเตือน: การจอง #{r['id']} ยังไม่มีใครคอนเฟิร์ม!\n"
                                             f"─────────────────\n{_resv_detail_lines(r)}"),
                        _resv_confirm_card(r["id"]),
                    ])
                    _touch_reminded(r["id"], now.strftime("%Y-%m-%d %H:%M:%S"))
                    print(f"[resv] ย้ำเตือนจอง #{r['id']} → {notify} (ทุก {interval} นาที)", flush=True)
                except Exception as e:
                    print(f"[resv] reminder push failed #{r['id']}: {e}", flush=True)
        except Exception as e:
            print(f"[resv] reminder loop error: {e}", flush=True)


threading.Thread(target=_reservation_reminder_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# LINE Webhook
# ══════════════════════════════════════════════════════════════════════════════

def _slip_enabled(group_id: str) -> bool:
    """เช็คสลิป+เตือน+รายงาน เฉพาะกลุ่มใน SLIP_GROUPS (ถ้าไม่ตั้ง = ทุกกลุ่ม)"""
    return (not SLIP_GROUPS) or (group_id in SLIP_GROUPS)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


_last_admin_error_ts = 0.0
_last_group_error_ts = {}   # group_id -> เวลาที่เตือน error ในกรุ๊ปล่าสุด

def notify_group_error(event, group_id):
    """เตือนในกรุ๊ปว่ามีสลิปอ่านไม่สำเร็จ แบบจำกัด 1 ครั้ง/5 นาที/กรุ๊ป
    (สลิปที่พลาดจะไม่หายเงียบ คนส่งรู้ว่าต้องส่งใหม่ แต่ไม่สแปม)"""
    if time.time() - _last_group_error_ts.get(group_id, 0) < 300:
        return
    _last_group_error_ts[group_id] = time.time()
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="⚠️ มีสลิปบางรายการที่ระบบอ่านไม่ทัน กรุณาส่งสลิปนั้นใหม่อีกครั้ง"))
    except Exception:
        pass


def notify_admin_error(group_id, err):
    """แจ้ง Admin เวลาบอทอ่านสลิปพลาด แบบจำกัด 1 ครั้ง/30 นาที (กัน Admin โดนสแปม + ประหยัดโควต้า push)"""
    global _last_admin_error_ts
    if not ADMIN_USER_ID:
        return
    if time.time() - _last_admin_error_ts < 1800:
        return
    _last_admin_error_ts = time.time()
    try:
        line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(
            text=f"🛠️ [SYSTEM] บอทอ่านสลิปไม่สำเร็จ (group={group_id})\n"
                 f"สาเหตุ: {str(err)[:250]}\n"
                 "ถ้าเป็นช่วงเย็น/สลิปเยอะ อาจเป็นเพราะโควต้า Gemini ฟรีหมดรายวัน "
                 "→ พิจารณาเปิดบิลลิ่ง Gemini (ถูกมาก) เพื่อให้ไม่ขาดช่วง"))
    except Exception:
        pass


# จำกัดอ่านรูปพร้อมกันไม่กี่ใบ (คิวที่เหลือรอ) — กันรูปถล่ม 50-70 ใบรัวๆ ทำ memory พุ่งจน worker OOM/รีเซ็ต
# (worker ล่ม = storage รีเซ็ต = จอง/สลิปช่วงนั้นพลาด) — ค่าน้อยปลอดภัยกว่าเพราะ Render Starter แรมจำกัด
_slip_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SLIP_WORKERS", "2")),
                                thread_name_prefix="slip")


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    # ส่งเข้า pool (จำกัดจำนวนพร้อมกัน) → ตอบ LINE ทันที, รูปที่เกินจะเข้าคิวไม่ถล่ม memory
    _slip_pool.submit(_process_image_event, event)


def _process_image_event(event):
    group_id = getattr(event.source, "group_id", event.source.user_id)
    if group_id in IGNORE_GROUPS:
        return   # กลุ่มที่สั่งให้เมินทั้งหมด → ไม่ทำอะไรเลย
    _clear_group_left(group_id)   # มีรูปเข้ามา = บอทยังอยู่ในกลุ่ม → ปลดมาร์ค left ถ้าเคยติด (self-heal)
    if group_id in PAYABLE_GROUPS:
        _process_payable_image(event, group_id)   # กลุ่มเจ้าหนี้ → บิล/สลิปจ่าย (ไม่ทำระบบสลิปปกติ)
        return
    if not _slip_enabled(group_id):
        return   # ไม่ใช่กลุ่มที่เปิดเช็คสลิป → ไม่ยุ่งเลย
    msg_id = getattr(event.message, "id", None)
    # กันประมวลผลรูปเดิมซ้ำ (LINE ส่ง webhook ซ้ำ / บอทรีสตาร์ทแล้ว retry) → ไม่งั้นจะเจอ ref ตัวเองแล้วหาว่า 'สลิปซ้ำ'
    if _slip_message_seen(group_id, msg_id):
        print(f"[skip] รูปนี้ประมวลผลแล้ว (message_id ซ้ำ) group={group_id} msg={msg_id}", flush=True)
        return
    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        info        = extract_slip_info(image_bytes)

        # รอบแรกว่า 'ไม่ใช่สลิป' → ลองอ่านซ้ำอีกรอบด้วยคำสั่งเข้มขึ้น (กู้ใบก้ำกึ่ง: บิลคู่สลิป/ถ่ายจอ/เอียง/มืด)
        if not info.get("is_slip", True):
            try:
                info_retry = extract_slip_info(image_bytes, retry=True)
                # รอบสองดีกว่าถ้า is_slip=true หรืออ่านยอดเงินได้
                if info_retry.get("is_slip") or float(info_retry.get("amount") or 0) > 0:
                    info = info_retry
                    print(f"[slip] retry กู้สลิปได้ group={group_id}", flush=True)
            except Exception as e:
                print(f"[slip] retry อ่านซ้ำพลาด group={group_id}: {e}", flush=True)

        # กันพลาดสำคัญ: ถ้า AI อ่าน 'จำนวนเงิน' ได้ ให้ถือเป็นสลิปเสมอ แม้ is_slip จะตอบ false (AI ขัดแย้งในตัว)
        if not info.get("is_slip", True) and float(info.get("amount") or 0) > 0:
            info["is_slip"] = True
            print(f"[slip] is_slip=false แต่มียอด {info.get('amount')} → ถือเป็นสลิป group={group_id}", flush=True)

        # ยังไม่ใช่สลิปหลังอ่านซ้ำ → นับตกหล่น + เตือนในกลุ่ม (ถ้าเปิด) เพื่อให้รู้ว่าใบไหนบอทอ่านไม่ออก
        if not info.get("is_slip", True):
            print(f"[skip] not a slip, group={group_id}", flush=True)
            record_image_miss(group_id, "notslip")
            if SLIP_WARN_UNREAD:
                try:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text="🟡 บอทอ่านรูปนี้ไม่ออกว่าเป็นสลิป — ยังไม่นับให้\n"
                             "ถ้าเป็นสลิปโอน โปรดส่งใหม่ให้ชัดขึ้น (แนะนำเป็นภาพ screenshot จะแม่นสุด)"))
                except Exception:
                    pass
            return

        # อ่านเฉพาะรายรับ (เฉพาะกลุ่มที่กำหนด): บันทึกเฉพาะสลิปที่โอนเข้า 'บัญชีร้านที่ระบุ' เท่านั้น
        # บัญชีอื่น (ร้านจ่ายออก/โอนผิดบัญชี/บัญชีที่ไม่ได้ระบุ) → ข้าม ไม่บันทึก
        income_only = INCOME_ONLY or (group_id in INCOME_ONLY_GROUPS)
        if income_only and PAYEE_ACCOUNTS and _slip_account_label(info) is None:
            print(f"[skip] ไม่ใช่รายรับ (ไม่ตรงบัญชีร้านที่ระบุ) group={group_id}", flush=True)
            record_image_miss(group_id, "notincome")
            return

        # normalize เลขอ้างอิง (ตัวพิมพ์ใหญ่ + ตัดช่องว่าง) กันอ่านเพี้ยนเล็กน้อยแล้วเทียบไม่ตรง
        if info.get("ref_number"):
            info["ref_number"] = "".join(str(info["ref_number"]).upper().split())

        promptpay = verify_with_promptpay(image_bytes)
        # ล็อกช่วงเช็คซ้ำ+บันทึก ให้เป็นจังหวะเดียว กัน race ตอนส่งซ้ำพร้อมกันหลาย thread
        with _dup_lock:
            # เช็ค message_id ซ้ำอีกครั้งแบบ atomic ในล็อก (กันสองรอบที่ผ่านเช็คต้นทางพร้อมกัน)
            if _slip_message_seen(group_id, msg_id):
                print(f"[skip] รูปนี้ประมวลผลแล้ว (message_id ซ้ำ, in-lock) group={group_id} msg={msg_id}", flush=True)
                return
            dup_type, prev_amount = find_duplicate(group_id, info)
            verdict  = build_verdict(info, promptpay, dup_type, prev_amount)
            save_slip(group_id, info, verdict["status"], message_id=msg_id)

        # สลิปผ่าน (PASS) → เงียบไว้ ไม่รกแชท (ยังบันทึกไว้ ดูรวมได้ที่ "สรุป")
        # เตือนในกรุ๊ปเฉพาะที่มีปัญหา (WARN/FAIL)
        if verdict["status"] != "PASS":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=verdict["group_msg"]))
        if verdict["admin_msg"] and ADMIN_USER_ID:
            line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=verdict["admin_msg"]))
    except Exception as e:
        # ประมวลผลสลิปไม่สำเร็จ → เตือนแบบจำกัดความถี่ (สลิปไม่หายเงียบ แต่ไม่สแปม)
        # + เตือน Admin ส่วนตัวด้วย จะได้รู้ว่าระบบมีปัญหา
        print(f"[error] handle_image group={group_id}: {e}", flush=True)
        record_image_miss(group_id, "error")
        notify_group_error(event, group_id)
        notify_admin_error(group_id, e)


def _today_iso():
    return datetime.now(TZ).date().isoformat()


def delete_latest_slip(event, group_id):
    today = _today_iso()
    with _db() as conn:
        row = conn.execute(
            "SELECT id, sender, amount FROM slips WHERE group_id=? AND slip_date=? ORDER BY id DESC LIMIT 1",
            (group_id, today)
        ).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📊 ไม่มีสลิปวันนี้ให้ลบ"))
            return
        conn.execute("DELETE FROM slips WHERE id=?", (row["id"],))
        conn.commit()
    amt = f"{float(row['amount'] or 0):,.2f}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🗑️ ลบสลิปใบล่าสุดแล้ว: {row['sender'] or '?'} | {amt} บาท\n"
             "─────────────────\n" + build_daily_report(group_id)))


def delete_slip_by_index(event, group_id, n):
    today = _today_iso()
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, sender, amount FROM slips WHERE group_id=? AND slip_date=? ORDER BY id",
            (group_id, today)
        ).fetchall()
    if n < 1 or n > len(rows):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"❌ ไม่มีรายการที่ {n} (วันนี้มี {len(rows)} รายการ)\nพิมพ์ 'สรุป' ดูเลขรายการก่อน"))
        return
    row = rows[n - 1]
    with _db() as conn:
        conn.execute("DELETE FROM slips WHERE id=?", (row["id"],))
        conn.commit()
    amt = f"{float(row['amount'] or 0):,.2f}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🗑️ ลบรายการที่ {n} แล้ว: {row['sender'] or '?'} | {amt} บาท\n"
             "─────────────────\n" + build_daily_report(group_id)))


def send_reset_confirm(event, group_id):
    today = _today_iso()
    with _db() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM slips WHERE group_id=? AND slip_date=?", (group_id, today)
        ).fetchone()["c"]
    if cnt == 0:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📊 ไม่มีสลิปวันนี้ให้ล้าง"))
        return
    template = TemplateSendMessage(
        alt_text="ยืนยันล้างสลิปวันนี้",
        template=ButtonsTemplate(
            title="⚠️ ล้างสลิปวันนี้",
            text=f"จะลบสลิปวันนี้ทั้งหมด {cnt} รายการ (เฉพาะกรุ๊ปนี้) ยืนยันไหม?",
            actions=[PostbackAction(label="🗑️ ยืนยันล้าง",
                                    data=f"reset_today:{today}:{uuid.uuid4().hex[:8]}",
                                    display_text="ยืนยันล้างสลิปวันนี้")],
        ),
    )
    line_bot_api.reply_message(event.reply_token, template)


def do_reset_today(event, date_str):
    group_id = getattr(event.source, "group_id", event.source.user_id)
    with _db() as conn:
        cur = conn.execute("DELETE FROM slips WHERE group_id=? AND slip_date=?", (group_id, date_str))
        deleted = cur.rowcount
        conn.execute("DELETE FROM image_misses WHERE group_id=? AND stat_date=?", (group_id, date_str))
        conn.commit()
    confirmer = get_display_name(event.source)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🗑️ ล้างสลิปวันที่ {date_str} เรียบร้อย {deleted} รายการ (โดย {confirmer})\n"
             "─────────────────\n" + build_daily_report(group_id, date_str)))


def send_reset_all_confirm(event, group_id):
    """ยืนยันก่อนล้าง 'ทุกอย่าง' ของกลุ่มนี้ (สลิป+รูปตกหล่น+การจอง ทุกวัน) — ใช้ตอนเลิกใช้/รีเซ็ตกลุ่ม"""
    with _db() as conn:
        n_slip = conn.execute("SELECT COUNT(*) c FROM slips WHERE group_id=?", (group_id,)).fetchone()["c"]
        n_resv = conn.execute("SELECT COUNT(*) c FROM reservations WHERE origin_group_id=?", (group_id,)).fetchone()["c"]
    if n_slip == 0 and n_resv == 0:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 กลุ่มนี้ไม่มีข้อมูลให้ล้าง"))
        return
    line_bot_api.reply_message(event.reply_token, TemplateSendMessage(
        alt_text="ยืนยันล้างข้อมูลทั้งหมดของกลุ่มนี้",
        template=ButtonsTemplate(
            title="⚠️ ล้างข้อมูลทั้งหมด (กลุ่มนี้)",
            text=f"ลบสลิป {n_slip} + จอง {n_resv} ทั้งหมด (กู้คืนไม่ได้)",
            actions=[PostbackAction(label="🗑️ ยืนยันล้าง", data=f"reset_all:{uuid.uuid4().hex[:8]}")],
        ),
    ))


def do_reset_all(event):
    """ล้างข้อมูลทั้งหมดของกลุ่มนี้ — สลิป, รูปตกหล่น, การจอง (ที่เกิดในกลุ่มนี้)"""
    group_id = getattr(event.source, "group_id", event.source.user_id)
    with _db() as conn:
        n_slip = conn.execute("DELETE FROM slips WHERE group_id=?", (group_id,)).rowcount
        conn.execute("DELETE FROM image_misses WHERE group_id=?", (group_id,))
        n_resv = conn.execute("DELETE FROM reservations WHERE origin_group_id=?", (group_id,)).rowcount
        conn.commit()
    confirmer = get_display_name(event.source)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🧹 ล้างข้อมูลกลุ่มนี้เรียบร้อย (โดย {confirmer})\n"
             f"• สลิป {n_slip} รายการ\n• การจอง {n_resv} รายการ\n"
             "กลุ่มนี้พร้อมเริ่มใหม่แล้ว"))


def send_reset_payable_confirm(event, group_id):
    """ยืนยันก่อนล้าง 'บัญชีหนี้ทั้งหมด' ของกลุ่มนี้ (บิล+จ่าย+ยอดยกมา) — ใช้รีเซ็ตเริ่มนับใหม่"""
    with _db() as conn:
        n_bill = conn.execute("SELECT COUNT(*) c FROM payable_bills WHERE group_id=?", (group_id,)).fetchone()["c"]
        n_pay  = conn.execute("SELECT COUNT(*) c FROM payable_payments WHERE group_id=?", (group_id,)).fetchone()["c"]
    opening = _payable_opening(group_id)
    if n_bill == 0 and n_pay == 0 and opening == 0:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 บัญชีหนี้กลุ่มนี้ว่างอยู่แล้ว"))
        return
    line_bot_api.reply_message(event.reply_token, TemplateSendMessage(
        alt_text="ยืนยันล้างบัญชีหนี้ทั้งหมดของกลุ่มนี้",
        template=ButtonsTemplate(
            title="⚠️ ล้างบัญชีหนี้ (กลุ่มนี้)",
            text=f"ลบบิล {n_bill} + จ่าย {n_pay} ล้างยอดยกมา (กู้คืนไม่ได้)",
            actions=[PostbackAction(label="🗑️ ยืนยันล้าง", data=f"reset_payable:{uuid.uuid4().hex[:8]}")],
        ),
    ))


def do_reset_payable(event):
    """ล้างบัญชีหนี้ทั้งหมดของกลุ่มนี้ — บิล + เงินจ่าย + ยอดยกมา (เริ่มนับใหม่)"""
    group_id = _payable_account_key(getattr(event.source, "group_id", event.source.user_id))
    with _db() as conn:
        n_bill = conn.execute("DELETE FROM payable_bills WHERE group_id=?", (group_id,)).rowcount
        n_pay  = conn.execute("DELETE FROM payable_payments WHERE group_id=?", (group_id,)).rowcount
        conn.commit()
    _set_meta(f"payable_opening:{group_id}", "0")
    confirmer = get_display_name(event.source)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🧹 ล้างบัญชีหนี้กลุ่มนี้เรียบร้อย (โดย {confirmer})\n"
             f"• บิลซื้อ {n_bill} รายการ\n• เงินจ่าย {n_pay} รายการ\n• ยอดยกมา → 0\n"
             "พร้อมเริ่มนับใหม่แล้ว"))


def build_help(group_id: str) -> str:
    """เมนูคำสั่ง — แสดงเฉพาะหมวดที่กลุ่มนั้นใช้ได้ (สลิป/จอง/บัญชีหนี้)"""
    if group_id in PAYABLE_GROUPS:
        return "\n".join([
            f"🤖 คำสั่งบอทเอด — บัญชีหนี้ {PAYABLE_VENDOR}",
            "─────────────────",
            "💰 บัญชีเจ้าหนี้",
            "• ส่งรูปบิลซื้อ → บอทอ่านยอด เพิ่มหนี้ให้",
            "• ส่งรูปสลิปโอนจ่าย → บอทอ่านยอด ลดหนี้ให้",
            "• บิล 3500 → บันทึกบิลด้วยตัวเลข (กรณีไม่มีรูป)",
            "• บิล 6/6 24153 → ลงบิลย้อนวันที่ (วัน/เดือน ยอด)",
            "• ตั้งยอดยกมา 12000 → ตั้งยอดค้างเก่า (ครั้งเดียวตอนเริ่ม)",
            "• สรุปหนี้ → ดูยอดค้างวันนี้",
            "• สรุปหนี้ 2026-06-09 → ดูย้อนหลัง (ตามวันที่)",
            "• รายการบิล → ดูบิลทั้งหมด + ที่มา (เช็กว่ามาจากไหน)",
            "• ลบบิลล่าสุด / ลบจ่ายล่าสุด → แก้กรณีบันทึกผิด",
            "• ลบวันที่ 6/6 → ลบทุกบิล/จ่ายของวันนั้น",
            "• ล้างบัญชีหนี้ → ล้างทั้งหมด เริ่มนับใหม่ (มีปุ่มยืนยัน)",
            "• ทดสอบรายงานหนี้ → ส่งสรุปหนี้เดี๋ยวนี้ (เช็คการส่ง/redirect)",
            f"⏰ บอทสรุปหนี้เมื่อวานให้เองทุกวัน ตี{PAYABLE_SUMMARY_HOUR}",
            "─────────────────",
            "• groupid → ดู Group ID | help → เมนูนี้",
        ])
    lines = ["🤖 คำสั่งบอทเอด", "─────────────────"]
    if _slip_enabled(group_id):
        lines += [
            "📊 สลิป / รายงาน",
            "• ส่งรูปสลิป → บอทตรวจให้อัตโนมัติ (ผ่าน=เงียบ)",
            "• สรุป → รายงานสลิปวันนี้",
            "• สรุป 2026-06-09 → รายงานย้อนหลัง (ตามวันที่)",
            "• รายงานเมื่อวาน → ส่งรายงานเมื่อวานเข้ากลุ่ม",
            "• ลบล่าสุด / ลบ 3 → ลบสลิป (เลขดูจาก 'สรุป')",
            "• ล้างวันนี้ → ล้างสลิปวันนี้ทั้งหมด (มีปุ่มยืนยัน)",
            "",
        ]
    if (RESV_GROUPS and group_id in RESV_GROUPS) or BAR_GROUP_ID:
        lines += [
            "🔔 จองโต๊ะ",
            "• พิมพ์จองปกติ เช่น:",
            "  จองโต๊ะคุณเอ 4คน วันนี้ 2ทุ่ม โซนA",
            "• ต้องครบ: ชื่อ / จำนวนคน / วันเวลา / โซน (ขาด=บอทถาม)",
            "• จองล่วงหน้า: ใส่วัน เช่น พรุ่งนี้/เสาร์นี้",
            "• กดปุ่ม ✅ คอนเฟิร์มการจอง บนการ์ด",
            "• สรุปจอง → ดูรายการจอง (วันนี้+ล่วงหน้า)",
            "",
        ]
    lines += [
        "🛠️ ทั่วไป",
        "• groupid → ดู Group ID ของกลุ่ม",
        "• คู่มือ → วิธีใช้แบบละเอียด",
        "• ล้างทั้งหมด → ล้างข้อมูลกลุ่มนี้ทั้งหมด (สลิป+จอง มีปุ่มยืนยัน)",
        "• help / คำสั่ง → เมนูนี้",
    ]
    return "\n".join(lines)


def build_manual(group_id: str) -> str:
    """คู่มือใช้งานแบบย่อ (พิมพ์ 'คู่มือ') — แสดงเฉพาะหมวดที่กลุ่มนั้นใช้ได้"""
    if group_id in PAYABLE_GROUPS:
        return (
            f"📖 คู่มือ — บัญชีหนี้ {PAYABLE_VENDOR}\n\n"
            "💰 หลักการ\n"
            "• ส่งรูป 'บิลซื้อ' ในกลุ่ม → บอทอ่านยอดรวม แล้ว 'เพิ่มหนี้'\n"
            "• ส่งรูป 'สลิปโอนจ่าย' → บอทอ่านยอด แล้ว 'ลดหนี้' (กันสลิปซ้ำด้วยเลขอ้างอิง)\n"
            "• บอทตอบยอดค้างสะสมล่าสุดให้ทุกครั้ง\n\n"
            "🚀 เริ่มใช้ครั้งแรก\n"
            "• พิมพ์ 'ตั้งยอดยกมา 12000' ใส่ยอดค้างเก่าก้อนเดียว\n"
            "• หรือวางบล็อก 'ค้าง' (บันทึกยอดค้างรายวัน) → บอทใช้ 'วันล่าสุด' เป็นยอดยกมา:\n"
            "   ค้าง / 6/6=24153 / 13/6=8074 → ยอดยกมา = 8,074\n\n"
            "📊 ดูยอด\n"
            "• สรุปหนี้ → ยอดค้างวันนี้ (ยกเข้า + บิล − จ่าย = ค้างสะสม)\n"
            "• สรุปหนี้ 2026-06-09 → ดูย้อนหลังตามวันที่\n\n"
            "🧹 แก้ที่ผิด\n"
            "• ลบบิลล่าสุด / ลบจ่ายล่าสุด\n"
            "• ล้างบัญชีหนี้ → ล้างทั้งหมด เริ่มนับใหม่ (มีปุ่มยืนยัน)\n\n"
            f"⏰ บอทส่งสรุปหนี้เมื่อวานให้เองทุกวัน ตี{PAYABLE_SUMMARY_HOUR}\n"
            "ℹ️ help → เมนูคำสั่ง | groupid → ดูข้อมูลกลุ่ม"
        )
    slip_on = _slip_enabled(group_id)
    resv_on = (RESV_GROUPS and group_id in RESV_GROUPS) and group_id not in RESV_EXCLUDE_GROUPS
    blocks = ["📖 คู่มือใช้งานบอทเอด"]
    if slip_on:
        blocks.append(
            "\n📊 ตรวจสลิป\n"
            "• ส่งรูปสลิปในกลุ่ม → บอทตรวจให้อัตโนมัติ\n"
            "• ✅ ผ่าน = เงียบ (ปกติ)\n"
            "• ⚠️ น่าสังเกต / 🚨 ต้องสงสัย = บอทเตือน อย่าเพิ่งรับเงินจนกว่าจะตรวจ\n"
            "คำสั่ง:\n"
            "• สรุป → รายงานสลิปวันนี้\n"
            "• สรุป 2026-06-09 → รายงานย้อนหลัง (ปี-เดือน-วัน)\n"
            "• รายงานเมื่อวาน → ส่งรายงานเมื่อวานอีกครั้ง\n"
            "• ลบล่าสุด / ลบ 3 → ลบสลิป (เลขดูจาก 'สรุป')\n"
            "• ล้างวันนี้ → ล้างสลิปวันนี้ทั้งหมด (มีปุ่มยืนยัน)\n"
            "⏰ บอทส่งสรุปสลิปเมื่อวานให้เองทุก 00:30"
        )
    if resv_on:
        blocks.append(
            "\n🔔 จองโต๊ะ\n"
            "พิมพ์จองตามปกติ เช่น\n"
            "  จองโต๊ะคุณเอ 4คน วันนี้ 2ทุ่ม โซนA\n"
            "• ต้องครบ: ชื่อ / จำนวนคน / วันเวลา / โซน (ขาด=บอทถาม)\n"
            "• จองล่วงหน้า ใส่วัน เช่น พรุ่งนี้ / เสาร์นี้\n"
            "• กดปุ่ม ✅ คอนเฟิร์มการจอง บนการ์ดเมื่อรับจองแล้ว\n"
            "• ยังไม่กดคอนเฟิร์ม บอทจะย้ำเตือนซ้ำจนกว่าจะกด\n"
            "คำสั่ง:\n"
            "• สรุปจอง → ดูรายการจอง (วันนี้ + ล่วงหน้า)\n"
            "⏰ บอทส่งสรุปจองวันนี้ให้เองทุก 16:00"
        )
    blocks.append("\nℹ️ help → เมนูคำสั่ง | groupid → ดูข้อมูลกลุ่ม")
    return "\n".join(blocks)


@handler.add(LeaveEvent)
def handle_leave(event):
    """บอทถูกเตะออก/ออกจากกลุ่ม → มาร์คเลิกส่งรายงาน-สรุปกลุ่มนั้น (กันค้าง retry/สแปม)"""
    gid = getattr(event.source, "group_id", None) or getattr(event.source, "room_id", None)
    if gid:
        _mark_group_left(gid)


@handler.add(JoinEvent)
def handle_join(event):
    """บอทถูกเชิญเข้ากลุ่ม (กลับมา) → ปลดมาร์ค กลับมาส่งปกติ"""
    gid = getattr(event.source, "group_id", None) or getattr(event.source, "room_id", None)
    if gid:
        _clear_group_left(gid)


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text     = event.message.text.strip()
    group_id = getattr(event.source, "group_id", event.source.user_id)
    print(f"[GROUP_ID] source_type={event.source.type} id={group_id} text={text}", flush=True)
    if group_id in IGNORE_GROUPS:
        return   # กลุ่มที่สั่งให้เมินทั้งหมด → ไม่ทำอะไรเลย
    _clear_group_left(group_id)   # มีข้อความเข้ามา = บอทยังอยู่ในกลุ่ม → ปลดมาร์ค left ถ้าเคยติด (self-heal)
    # เมนูคำสั่ง
    if text.lower() in ("help", "คำสั่ง", "ช่วยเหลือ", "เมนู", "menu", "คำสั่งบอท"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_help(group_id)))
        return
    # คู่มือใช้งานแบบย่อ
    if text.lower() in ("คู่มือ", "วิธีใช้", "manual", "คู่มือการใช้งาน"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_manual(group_id)))
        return
    # พิมพ์ "groupid" → บอทตอบ Group ID + บทบาทของกลุ่มนี้ (ไว้เช็ก/ตั้งค่า env)
    if text.lower().replace(" ", "") == "groupid":
        roles = [
            "📊 เช็คสลิป/รายงาน: " + ("✅ ใช่" if _slip_enabled(group_id) else "❌ ไม่"),
            "🔔 รับจองวันนี้: "     + ("🚫 ปิดจอง (RESV_EXCLUDE_GROUPS)" if group_id in RESV_EXCLUDE_GROUPS
                                       else ("✅ ใช่" if (RESV_GROUPS and group_id in RESV_GROUPS) else "❌ ไม่ (ต้องเพิ่มใน RESV_GROUPS)")),
            "📅 กลุ่มบาร์น้ำ: "      + ("✅ ใช่" if (BAR_GROUP_ID and group_id == BAR_GROUP_ID) else "❌ ไม่"),
            f"💰 บัญชีหนี้ {PAYABLE_VENDOR}: " + ("✅ ใช่" if group_id in PAYABLE_GROUPS else "❌ ไม่ (ต้องเพิ่มใน PAYABLE_GROUPS)"),
        ]
        if group_id in REPORT_REDIRECT:
            roles.append(f"📤 รายงานส่งต่อไปกลุ่ม: {REPORT_REDIRECT[group_id]}")
        if group_id in REPORT_REDIRECT.values():
            roles.append("📥 กลุ่มนี้เป็นปลายทางรับรายงานจากกลุ่มอื่น")
        if group_id in PAYABLE_MIRROR:
            roles.append(f"🪞 บัญชีหนี้แบบ mirror: กลุ่มนี้=ส่งบิล/สลิป(เงียบ) เด้งผลที่ {PAYABLE_MIRROR[group_id]}")
        if group_id in _PAYABLE_PRIMARY_OF:
            roles.append("🪞 บัญชีหนี้แบบ mirror: กลุ่มนี้=กระจกเงา (ดูผล/ยอดของบัญชีร่วม)")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"🆔 Group ID:\n{group_id}\n─────────────────\n" + "\n".join(roles)))
        return
    # กลุ่มเจ้าหนี้การค้า → จัดการบัญชีหนี้ (บิล/จ่าย/สรุป) แล้วจบ ไม่ทำระบบสลิป/จองปกติ
    if group_id in PAYABLE_GROUPS:
        handle_payable_text(event, text, group_id)
        return
    # สรุปการจอง (วันนี้ + ล่วงหน้า) ของกลุ่มนี้ — ใช้ได้ทุกกลุ่ม
    if text.lower() in ("สรุปจอง", "สรุปการจอง", "รายการจอง", "จองวันนี้"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_resv_summary(group_id)))
        return
    # ล้างข้อมูลทั้งหมดของกลุ่มนี้ (สลิป+จอง ทุกวัน) — เตรียมเลิกใช้/รีเซ็ตกลุ่ม (มีปุ่มยืนยัน)
    if text.lower() in ("ล้างทั้งหมด", "ล้างกลุ่มนี้", "ล้างข้อมูลทั้งหมด", "รีเซ็ตทั้งหมด"):
        send_reset_all_confirm(event, group_id)
        return
    # คำสั่งเกี่ยวกับสลิป (สรุป/ลบ/ล้าง) → เฉพาะกลุ่มที่เปิดเช็คสลิปเท่านั้น
    if _slip_enabled(group_id):
        if text.lower() in ("สรุป", "รายงาน", "report", "summary"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_daily_report(group_id)))
            return

        # สรุปย้อนหลังรายวัน เช่น "สรุป 2026-06-03" (เอาไว้ตรวจเทียบยอด)
        if text.startswith("สรุป ") or text.startswith("รายงาน "):
            arg = text.split(" ", 1)[1].strip()
            try:
                datetime.strptime(arg, "%Y-%m-%d")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_daily_report(group_id, arg)))
                return
            except ValueError:
                pass

        # ทดสอบ/กู้รายงานอัตโนมัติ — push 'รายงานเมื่อวาน' เข้ากลุ่มนี้เดี๋ยวนี้
        # ใช้เช็คว่าระบบ push ใช้งานได้ไหม (ถ้าพลาดจะโชว์สาเหตุใน LINE เลย) + กู้รายงานที่ 00:30 พลาดไป
        if text.lower() in ("ทดสอบรายงาน", "รายงานเมื่อวาน", "force report", "test report"):
            yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=build_daily_report(group_id, yesterday)))
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="✅ push รายงานเมื่อวานสำเร็จ — ระบบ push ใช้งานได้ปกติ"))
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"🚨 push ล้มเหลว: {e}\n(นี่คือสาเหตุที่รายงาน 00:30 ไม่เด้ง)"))
            return

        # คำสั่งลบสลิป (กรณีส่งผิด/ซ้ำ)
        if text in ("ล้างวันนี้", "รีเซ็ตวันนี้", "ล้างสลิปวันนี้", "รีเซ็ต"):
            send_reset_confirm(event, group_id)
            return
        if text.startswith("ลบ"):
            arg = text[2:].strip()
            if arg in ("ล่าสุด", "ใบล่าสุด"):
                delete_latest_slip(event, group_id)
                return
            if arg.isdigit():
                delete_slip_by_index(event, group_id, int(arg))
                return

    # ตรวจจับการจองโต๊ะ → แจ้งเตือนไปกรุ๊ปบาร์น้ำ
    try:
        handle_reservation_text(event, text, group_id)
    except Exception as e:
        print(f"[resv] handle_text error group={group_id}: {e}", flush=True)


@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data or ""
    print(f"[POSTBACK] source_type={event.source.type} data={data}", flush=True)
    pb_group = getattr(event.source, "group_id", None)
    if pb_group and pb_group in IGNORE_GROUPS:
        return   # กลุ่มที่สั่งให้เมินทั้งหมด → ไม่ทำอะไรเลย
    parts = data.split(":")
    action = parts[0]

    def _already(msg="⚠️ ทำรายการนี้ไปแล้ว ไม่ต้องกดซ้ำครับ"):
        """กดปุ่มซ้ำ → ตอบเตือนสั้นๆ ไม่ทำงานซ้ำ"""
        print(f"[postback] กดซ้ำ data={data} — ข้าม", flush=True)
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        except Exception:
            pass

    if action == "confirm_resv":
        try:
            resv_id = int(parts[1])
        except (IndexError, ValueError):
            return
        if not _postback_once(f"resv:{resv_id}"):
            _already(f"⚠️ การจอง #{resv_id} ทำรายการไปแล้ว ไม่ต้องกดซ้ำครับ")
            return
        try:
            handle_reservation_confirm(event, resv_id)
        except Exception as e:
            print(f"[resv] confirm error: {e}", flush=True)
    elif action == "reset_today":
        if not _postback_once(f"pb:{data}"):
            _already(); return
        try:
            do_reset_today(event, parts[1])
        except Exception as e:
            print(f"[reset] error: {e}", flush=True)
    elif action == "reset_all":
        if not _postback_once(f"pb:{data}"):
            _already(); return
        try:
            do_reset_all(event)
        except Exception as e:
            print(f"[reset] reset_all error: {e}", flush=True)
    elif action == "reset_payable":
        if not _postback_once(f"pb:{data}"):
            _already(); return
        try:
            do_reset_payable(event)
        except Exception as e:
            print(f"[reset] reset_payable error: {e}", flush=True)


@app.route("/health", methods=["GET"])
def health():
    # ทุกครั้งที่ถูก ping (UptimeRobot ทุก 5 นาที) เช็คว่าถึงเวลาส่งรายงาน/สรุปจองไหม
    try:
        maybe_send_resv_summary()
        maybe_send_daily_report()
        maybe_send_payable_summary()
    except Exception as e:
        print(f"[report] health-trigger error: {e}", flush=True)
    try:
        with _db() as conn:
            today_count = conn.execute(
                "SELECT COUNT(*) c FROM slips WHERE slip_date=?", (datetime.now(TZ).date().isoformat(),)
            ).fetchone()["c"]
            group_count = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
        return {"status": "ok", "slips_today": today_count, "active_groups": group_count,
                "storage": STORAGE_DESC}
    except Exception as e:
        # DB ล่ม/เชื่อมไม่ได้ชั่วคราว — ตอบ 200 อยู่ (กัน UptimeRobot รีสตาร์ท) แต่บอกสถานะ
        return {"status": "db_error", "storage": f"{STORAGE_DESC} — {e}"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
