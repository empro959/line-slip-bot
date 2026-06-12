import os
import json
import base64
import requests
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, ImageMessage, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, ButtonsTemplate, PostbackAction,
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
# กลุ่ม "บาร์น้ำ+จองโต๊ะล่วงหน้า" — จองล่วงหน้าจากกลุ่มไหนก็ตามจะส่งการ์ด+ปุ่มมาที่นี่ (ดู id ด้วยคำสั่ง groupid)
BAR_GROUP_ID      = os.environ.get("BAR_GROUP_ID", "")
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
                recorded_at   TEXT
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
        conn.execute("CREATE TABLE IF NOT EXISTS groups (group_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
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

def extract_slip_info(image_bytes: bytes) -> dict:
    prompt = (
        "ดูรูปนี้ว่าเป็นสลิป/หลักฐานการชำระเงินให้ร้านหรือไม่ "
        "(โอนผ่านธนาคาร/แอปธนาคาร หรือจ่ายผ่านเป๋าตัง/G-Wallet/รัฐช่วยจ่าย เช่น ไทยช่วยไทย คนละครึ่ง เราชนะ) "
        "แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_slip":true,"sender":null,"amount":0.00,"datetime":null,"bank":null,'
        '"account":null,"receiver":null,"receiver_account":null,"ref_number":null,"cropped":false,"fraud_score":0,"fraud_reasons":[]}\n\n'
        "is_slip: true ถ้าเป็นหลักฐานการชำระเงินสำเร็จให้ร้านจริง (รวมสลิปเป๋าตัง/รัฐช่วยจ่ายที่ขึ้น 'ทำรายการสำเร็จ'), "
        "false ถ้าเป็นรูปอื่น (เช่น รูปคน อาหาร เมนู วิว หรือใบเสร็จเปล่าที่ไม่ใช่หลักฐานจ่ายเงิน)\n"
        "ถ้า is_slip เป็น false ให้ใส่ค่าที่เหลือเป็น null/0 ได้เลย\n"
        "amount: จำนวนเงินที่ 'ร้านได้รับจริง'\n"
        "  - สลิปโอนธนาคารทั่วไป = ยอดที่โอน\n"
        "  - สลิปจ่ายผ่านรัฐช่วยจ่าย/เป๋าตัง (เช่น 'ไทยช่วยไทย' 'คนละครึ่ง' '60/40') ที่มีหลายยอด: "
        "ให้ใช้ยอดบรรทัด 'ค่าสินค้า/บริการ' (ยอดเต็มที่ร้านได้รับ) เท่านั้น "
        "ห้ามใช้ 'จำนวนเงินที่ชำระ' (ส่วนที่ลูกค้าจ่ายเอง มักติดลบ) หรือ 'จดบันทึก' (ส่วนที่รัฐช่วย)\n"

        "receiver: ชื่อบัญชี 'ปลายทาง/ผู้รับเงิน' (ฝั่ง 'ไปยัง') ตามที่เห็นในสลิป (ถ้ามีข้อมูลเพิ่มในวงเล็บก็ใส่ด้วย)\n"
        "receiver_account: เลขบัญชีปลายทาง/ผู้รับ (ตามที่เห็น แม้ถูกปิดบางส่วน เช่น xxx-x-x4818-x ก็ใส่)\n"
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
    bank   = info.get("bank") or "?"
    ref    = info.get("ref_number") or "-"

    base_info = (
        f"👤 ผู้โอน: {sender}\n"
        f"💰 จำนวน: {amount_str} บาท\n"
        f"📅 วันเวลา: {dt}\n"
        f"🏦 ปลายทาง: {bank}\n"
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

def save_slip(group_id: str, info: dict, verdict_status: str):
    today       = datetime.now(TZ).date().isoformat()
    recorded_at = datetime.now(TZ).strftime("%H:%M:%S")
    with _db() as conn:
        conn.execute(
            "INSERT INTO slips (group_id, slip_date, sender, amount, bank, ref_number, slip_datetime, verdict, recorded_at, account_label) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (group_id, today, info.get("sender"), float(info.get("amount") or 0),
             info.get("bank"), info.get("ref_number"), info.get("datetime"), verdict_status, recorded_at,
             _slip_account_label(info))
        )
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()


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


def build_resv_summary(notify_group=None, title="📋 สรุปการจอง") -> str:
    """สรุปการจองที่ 'ยังไม่ถึงวัน/วันนี้' (resv_date >= วันนี้) แยกหัวข้อ วันนี้ / ล่วงหน้า
    notify_group=None → ทุกการจอง (รวมวันนี้+ล่วงหน้า) / ระบุ group → เฉพาะการจองที่ส่งการ์ดเข้ากลุ่มนั้น
    จองที่ไม่มีวันที่จริง (resv_date null) ใช้ fallback: แสดงถ้าแจ้งมาภายใน RESV_REPORT_DAYS วัน"""
    today  = datetime.now(TZ).date().isoformat()
    cutoff = (datetime.now(TZ) - timedelta(days=RESV_REPORT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    q = ("SELECT * FROM reservations WHERE "
         "((resv_date IS NOT NULL AND resv_date >= ?) OR (resv_date IS NULL AND created_at >= ?))")
    params = [today, cutoff]
    if notify_group:
        q += " AND notify_group_id=?"
        params.append(notify_group)
    q += " ORDER BY resv_date IS NULL, resv_date, created_at"
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if not rows:
        return f"{title}\n─────────────────\nไม่มีการจอง"
    today_rows = [r for r in rows if r.get("resv_date") == today]
    later_rows = [r for r in rows if r.get("resv_date") != today]   # ล่วงหน้า + ที่ไม่มีวันชัด
    confirmed = sum(1 for r in rows if r.get("status") == "CONFIRMED")
    lines = [title, "─────────────────",
             f"ทั้งหมด {len(rows)} | ✅ คอนเฟิร์ม {confirmed} | ⏳ รอ {len(rows) - confirmed}"]
    if today_rows:
        lines += ["", f"📍 วันนี้ ({len(today_rows)})"] + [_resv_line(r) for r in today_rows]
    if later_rows:
        lines += ["", f"📅 ล่วงหน้า ({len(later_rows)})"] + [_resv_line(r) for r in later_rows]
    return "\n".join(lines)


def build_advance_resv_report() -> str:
    """สรุปจองล่วงหน้า (เข้ากลุ่มบาร์น้ำ) — ใช้ตอนรายงาน 00:30"""
    return build_resv_summary(BAR_GROUP_ID, "📅 สรุปจองล่วงหน้า (ที่กำลังจะถึง)")


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
        targets = [g for g in group_ids if (not SLIP_GROUPS) or g in SLIP_GROUPS]
        print(f"[report] trigger {today} → ส่งรายงานวันที่ {yesterday} ให้ {len(targets)} กลุ่ม", flush=True)
        failed = 0
        for group_id in targets:
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=build_daily_report(group_id, yesterday)))
                print(f"[report] sent OK → {group_id}", flush=True)
            except Exception as e:
                failed += 1
                print(f"[report] push FAILED {group_id}: {e}", flush=True)
        # สรุปจองล่วงหน้า → กลุ่มบาร์น้ำ (วันละครั้งพร้อมรายงานสลิป)
        if BAR_GROUP_ID:
            try:
                line_bot_api.push_message(BAR_GROUP_ID, TextSendMessage(text=build_advance_resv_report()))
                print(f"[report] sent advance-resv → {BAR_GROUP_ID}", flush=True)
            except Exception as e:
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
    """ส่งสรุปการจอง (วันนี้ + ล่วงหน้า) เข้ากลุ่มบาร์น้ำ + กลุ่มรับจอง เวลา 16:00 วันละครั้ง (idempotent)
    ถ้าไม่มีจองจะส่ง 'ไม่มีการจอง' ตามที่ขอ"""
    now = datetime.now(TZ)
    if (now.hour, now.minute) < (16, 0):
        return
    today = now.date().isoformat()
    with _report_lock:
        if _get_meta("last_resv_summary_date") == today:
            return
        targets = list(dict.fromkeys([t for t in ([BAR_GROUP_ID] + list(RESV_GROUPS)) if t]))
        if not targets:
            _set_meta("last_resv_summary_date", today)
            return
        text = build_resv_summary(None, "📋 สรุปการจอง (16:00)")
        failed = 0
        for g in targets:
            try:
                line_bot_api.push_message(g, TextSendMessage(text=text))
                print(f"[resv-summary] sent → {g}", flush=True)
            except Exception as e:
                failed += 1
                print(f"[resv-summary] FAILED {g}: {e}", flush=True)
        if failed == 0:
            _set_meta("last_resv_summary_date", today)
            print(f"[resv-summary] done {today}", flush=True)
        else:
            print(f"[resv-summary] {failed} กลุ่มส่งไม่สำเร็จ — retry รอบหน้า", flush=True)


def _report_backup_loop():
    """thread สำรอง — เผื่อ UptimeRobot ไม่ ping; เช็คทุก ~5 นาที"""
    while True:
        time.sleep(300)
        try:
            maybe_send_resv_summary()
            maybe_send_daily_report()
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
    จองล่วงหน้า → ส่งการ์ด+ปุ่มไปกลุ่มบาร์น้ำ (BAR_GROUP_ID) จากกลุ่มไหนก็ได้"""
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
    if not _slip_enabled(group_id):
        return   # ไม่ใช่กลุ่มที่เปิดเช็คสลิป → ไม่ยุ่งเลย
    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        info        = extract_slip_info(image_bytes)

        # ถ้าไม่ใช่สลิปโอนเงิน → เงียบไว้ ไม่ต้องตอบอะไร (แต่ยังนับไว้กระทบยอด เผื่อ AI อ่านพลาด)
        if not info.get("is_slip", True):
            print(f"[skip] not a slip, group={group_id}", flush=True)
            record_image_miss(group_id, "notslip")
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
            dup_type, prev_amount = find_duplicate(group_id, info)
            verdict  = build_verdict(info, promptpay, dup_type, prev_amount)
            save_slip(group_id, info, verdict["status"])

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
            actions=[PostbackAction(label="🗑️ ยืนยันล้างทั้งหมด",
                                    data=f"reset_today:{today}",
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


def build_help(group_id: str) -> str:
    """เมนูคำสั่ง — แสดงเฉพาะหมวดที่กลุ่มนั้นใช้ได้ (สลิป/จอง)"""
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
        "• help / คำสั่ง → เมนูนี้",
    ]
    return "\n".join(lines)


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text     = event.message.text.strip()
    group_id = getattr(event.source, "group_id", event.source.user_id)
    print(f"[GROUP_ID] source_type={event.source.type} id={group_id} text={text}", flush=True)
    # เมนูคำสั่ง
    if text.lower() in ("help", "คำสั่ง", "ช่วยเหลือ", "เมนู", "menu", "คำสั่งบอท"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_help(group_id)))
        return
    # พิมพ์ "groupid" → บอทตอบ Group ID + บทบาทของกลุ่มนี้ (ไว้เช็ก/ตั้งค่า env)
    if text.lower().replace(" ", "") == "groupid":
        roles = [
            "📊 เช็คสลิป/รายงาน: " + ("✅ ใช่" if _slip_enabled(group_id) else "❌ ไม่"),
            "🔔 รับจองวันนี้: "     + ("✅ ใช่" if (RESV_GROUPS and group_id in RESV_GROUPS) else "❌ ไม่ (ต้องเพิ่มใน RESV_GROUPS)"),
            "📅 กลุ่มบาร์น้ำ: "      + ("✅ ใช่" if (BAR_GROUP_ID and group_id == BAR_GROUP_ID) else "❌ ไม่"),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"🆔 Group ID:\n{group_id}\n─────────────────\n" + "\n".join(roles)))
        return
    # สรุปการจอง (วันนี้ + ล่วงหน้า) ของกลุ่มนี้ — ใช้ได้ทุกกลุ่ม
    if text.lower() in ("สรุปจอง", "สรุปการจอง", "รายการจอง", "จองวันนี้"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_resv_summary(group_id)))
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
    if data.startswith("confirm_resv:"):
        try:
            resv_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        try:
            handle_reservation_confirm(event, resv_id)
        except Exception as e:
            print(f"[resv] confirm error: {e}", flush=True)
    elif data.startswith("reset_today:"):
        try:
            do_reset_today(event, data.split(":", 1)[1])
        except Exception as e:
            print(f"[reset] error: {e}", flush=True)


@app.route("/health", methods=["GET"])
def health():
    # ทุกครั้งที่ถูก ping (UptimeRobot ทุก 5 นาที) เช็คว่าถึงเวลาส่งรายงาน/สรุปจองไหม
    try:
        maybe_send_resv_summary()
        maybe_send_daily_report()
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
