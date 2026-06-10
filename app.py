import os
import json
import base64
import requests
import threading
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, ImageMessage, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, ButtonsTemplate, PostbackAction,
)
import google.generativeai as genai

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
TZ                = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))

line_bot_api = LineBotApi(LINE_TOKEN)
handler      = WebhookHandler(LINE_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
gemini       = genai.GenerativeModel("gemini-2.5-flash")

# ─── Persistent storage (SQLite) ──────────────────────────────────────────────
import sqlite3

# ใช้ disk ถาวรที่ /var/data (Render Persistent Disk)
# พิสูจน์แล้ว: Render mount disk 'ช้า' หลายนาทีหลังแอป start (df เห็น /var/data แต่ตอน start ยังไม่ mount)
# → ห้ามตัดสินที่เก็บ DB ตอน import ครั้งเดียว ไม่งั้นจะตกไปเขียนที่ชั่วคราว '.' แล้วข้อมูลหายทุก restart
# วิธี: รอ disk แบบ background (นานได้หลายนาที ไม่ block gunicorn boot) แล้วค่อยเลือกที่เก็บ + init
#       ส่วน _db() จะรอจน storage พร้อมก่อนแตะ DB (กัน webhook ช่วงแรกที่ disk ยังไม่ mount เขียนผิดที่)
_DISK_PATH     = "/var/data"
DB_DIR         = None
DB_PATH        = None
DB_PERSISTENT  = False
_storage_ready = threading.Event()

def _connect():
    # เชื่อม DB ตรงๆ (ใช้ภายใน init / หลัง storage พร้อม) — timeout กัน 'database is locked' ตอนรูปเยอะ
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def _db():
    # รอ disk mount + init เสร็จก่อน (Render mount ช้า) — _setup_storage จะ set ภายใน ~10 นาทีเสมอ
    if not _storage_ready.wait(timeout=660):
        raise RuntimeError("storage ยังไม่พร้อม (disk ยังไม่ mount)")
    return _connect()

def _setup_storage():
    """รอ /var/data mount (สูงสุด ~10 นาที) แล้วเลือกที่เก็บ DB + init — รันใน background ไม่ block boot
    Render ปล่อย/ผูก disk ระหว่าง instance ช้า (โดยเฉพาะตอน deploy ถี่ๆ) จึงต้องใจเย็นรอ"""
    global DB_DIR, DB_PATH, DB_PERSISTENT
    for _i in range(300):          # 300 รอบ × 2 วิ = สูงสุด 10 นาที
        if os.path.isdir(_DISK_PATH):
            break
        if _i % 15 == 0:           # log ทุก ~30 วิ ให้เห็นว่ายังรออยู่
            print(f"[STORAGE] รอ {_DISK_PATH} mount (Render mount ช้า) ... {_i*2}s", flush=True)
        time.sleep(2)
    DB_PERSISTENT = os.path.isdir(_DISK_PATH)
    DB_DIR  = _DISK_PATH if DB_PERSISTENT else "."
    DB_PATH = os.path.join(DB_DIR, "slips.db")
    if DB_PERSISTENT:
        print(f"[STORAGE] ✅ ใช้ disk ถาวรที่ {DB_DIR}", flush=True)
    else:
        print("🔴🔴🔴 [STORAGE] /var/data ไม่ mount หลังรอ 5 นาที — ใช้ที่ชั่วคราว '.' "
              "(ข้อมูลจะหายตอน restart!) ตรวจ Persistent Disk ที่ Render", flush=True)
    init_db()
    _storage_ready.set()

def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slips (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id      TEXT,
                slip_date     TEXT,
                sender        TEXT,
                amount        REAL,
                bank          TEXT,
                ref_number    TEXT,
                slip_datetime TEXT,
                verdict       TEXT,
                recorded_at   TEXT
            )
        """)
        # migration: เพิ่มคอลัมน์ slip_datetime ให้ DB เก่าที่ยังไม่มี (ไว้จับซ้ำด้วยยอด+เวลา)
        try:
            conn.execute("ALTER TABLE slips ADD COLUMN slip_datetime TEXT")
        except Exception:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS groups (group_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        # นับรูปที่รับเข้ามาแต่ "ไม่ได้บันทึกเป็นสลิป" (อ่านไม่ออก/ไม่ใช่สลิป/error) ไว้กระทบยอดในรายงาน
        conn.execute("""
            CREATE TABLE IF NOT EXISTS image_misses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT,
                stat_date   TEXT,
                reason      TEXT,
                recorded_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
                confirmed_at    TEXT
            )
        """)
        conn.commit()

# เริ่มรอ disk แบบ background แล้วค่อย init_db (ไม่ block gunicorn boot) — Render mount disk ช้า
threading.Thread(target=_setup_storage, daemon=True).start()
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
        "fraud_score: 0-100 ยึดสิ่งที่เห็นจริง อย่าเดา:\n"
        "  • 0-39 = ไม่พบร่องรอยตัดต่อ ดูปกติ (สลิปทั่วไปควรอยู่ช่วงนี้)\n"
        "  • 40-69 = พบจุดน่าสงสัยแต่ไม่ฟันธง\n"
        "  • 70-100 = เห็นร่องรอยตัดต่อชัดเจน (เช่น ตัวเลขเงินถูกแก้ทับ ฟอนต์ตัวเลขเพี้ยน สีพื้นรอบตัวเลขไม่เนียน)\n"
        "ห้ามให้คะแนนสูงเพราะสิ่งเหล่านี้ (เป็นเรื่องปกติของสลิปจริง ไม่ใช่การตัดต่อ): "
        "ลายน้ำ/พื้นหลังโลโก้ธนาคาร (เช่น KBIZ/K+ มีรูปตึก), ถ่ายภาพหน้าจอเอียง/แสงสะท้อน/เบลอทั้งรูป, "
        "ชื่อบริษัท/ผู้โอนที่อ่านจากรูปไม่ชัดเพราะ OCR เพี้ยน, รูปแบบสลิปของแอปธุรกิจที่ต่างจากแอปบุคคล\n"
        "fraud_reasons: ระบุร่องรอยที่เจอพร้อมตำแหน่งให้ชัด (เช่น 'ตัวเลขจำนวนเงินเบลอกว่าตัวอื่น') ถ้าไม่เจอให้เป็น []"
    )
    img_part = {"mime_type": "image/jpeg", "data": image_bytes}
    # ลองใหม่ได้ 1 ครั้ง เผื่อชนลิมิตชั่วคราว (rate limit ต่อนาที)
    last_err = None
    for attempt in range(2):
        try:
            response = gemini.generate_content([prompt, img_part])
            raw = response.text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
    raise last_err


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
            "INSERT INTO slips (group_id, slip_date, sender, amount, bank, ref_number, slip_datetime, verdict, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group_id, today, info.get("sender"), float(info.get("amount") or 0),
             info.get("bank"), info.get("ref_number"), info.get("datetime"), verdict_status, recorded_at)
        )
        conn.execute("INSERT OR IGNORE INTO groups (group_id) VALUES (?)", (group_id,))
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
    misses   = sum(miss_counts.values())             # รูปที่รับมาแต่ไม่ได้เป็นสลิป
    received = len(slips) + misses                    # รับรูปทั้งหมด (ไว้กระทบยอดนับมือ)

    # กระทบยอด: บรรทัดท้ายบอกว่ารับรูปกี่ใบ / อ่านเป็นสลิปได้กี่ใบ / อ่านไม่ออกกี่ใบ
    def _recon_lines():
        if misses == 0:
            return []
        detail = []
        if miss_counts.get("notslip"):
            detail.append(f"อ่านไม่ออก/ไม่ใช่สลิป {miss_counts['notslip']}")
        if miss_counts.get("error"):
            detail.append(f"ประมวลผลพลาด {miss_counts['error']}")
        return ["", "─────────────────",
                f"ℹ️ รับรูป {received} | อ่านเป็นสลิป {len(slips)} | ตกหล่น {misses}",
                "   (" + ", ".join(detail) + " — ตามดูในกลุ่มว่าใบไหนบอทไม่ตอบ)"]

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
    for i, s in enumerate(slips, 1):
        amt  = f"{float(s.get('amount', 0)):,.2f}" if s.get("amount") else "?"
        icon = icons.get(s.get("verdict", ""), "❓")
        lines.append(f"{i}. {icon} {s.get('sender','?')} | {amt} บาท | {s.get('recorded_at','')}")
    lines += _recon_lines()
    return "\n".join(lines)


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
        # มาร์คว่า 'ส่งครบแล้ว' เฉพาะเมื่อไม่มีอันไหนล้มเหลว — ถ้ามี fail ปล่อยไว้ให้ ping รอบหน้า retry
        if failed == 0:
            _set_meta("last_report_date", today)
            print(f"[report] done {today}", flush=True)
        else:
            print(f"[report] {failed} กลุ่มส่งไม่สำเร็จ — จะ retry รอบ ping ถัดไป (~5 นาที)", flush=True)


def _report_backup_loop():
    """thread สำรอง — เผื่อ UptimeRobot ไม่ ping; เช็คทุก ~5 นาที"""
    while True:
        time.sleep(300)
        try:
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


def extract_reservation(text: str) -> dict:
    """ให้ Gemini ตัดสินว่าข้อความเป็นการจองโต๊ะหรือไม่ แล้วดึงรายละเอียด"""
    prompt = (
        "ข้อความต่อไปนี้จากแชทพนักงานร้าน เป็นการ 'จองโต๊ะ' ให้ลูกค้าหรือไม่ "
        "(ไม่ใช่การคุยเล่นที่บังเอิญมีคำว่าจอง/โต๊ะ) ตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_reservation":true,"is_advance":false,"customer":null,"people":null,"datetime":null,'
        '"table":null,"note":null}\n\n'
        "is_reservation: true เฉพาะเมื่อเป็นการจองโต๊ะจริง (มีเจตนานัด/จองที่นั่งให้ลูกค้า)\n"
        "is_advance: true ถ้าเป็นการจอง 'ล่วงหน้า' (สำหรับวันอื่น/วันข้างหน้า เช่น พรุ่งนี้ เสาร์นี้ วันที่ 25 ฯลฯ), "
        "false ถ้าจองสำหรับ 'วันนี้/คืนนี้/ตอนนี้'\n"
        "customer: ชื่อลูกค้า/ผู้จอง (ถ้ามี)\n"
        "people: จำนวนคน เช่น '4 คน' (ถ้ามี)\n"
        "datetime: วันและเวลาที่จอง เช่น 'วันนี้ 20:00' (ถ้ามี)\n"
        "table: โต๊ะ/โซน (ถ้ามี)\n"
        "note: รายละเอียดเพิ่มเติม (ถ้ามี)\n"
        "ฟิลด์ที่ไม่มีข้อมูลให้เป็น null\n\n"
        f"ข้อความ: {text}"
    )
    response = gemini.generate_content(prompt)
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _resv_detail_lines(r: dict) -> str:
    parts = []
    if r.get("customer"): parts.append(f"👤 ลูกค้า: {r['customer']}")
    if r.get("people"):   parts.append(f"👥 จำนวน: {r['people']}")
    if r.get("datetime"): parts.append(f"🕗 เวลา: {r['datetime']}")
    if r.get("table"):    parts.append(f"🪑 โต๊ะ: {r['table']}")
    if r.get("note"):     parts.append(f"📝 หมายเหตุ: {r['note']}")
    return "\n".join(parts) if parts else "(ไม่มีรายละเอียดเพิ่มเติม)"


def save_reservation(origin_group_id: str, requested_by: str, info: dict, raw_text: str) -> int:
    created_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO reservations "
            "(origin_group_id, requested_by, customer, people, resv_datetime, table_no, note, raw_text, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (origin_group_id, requested_by, info.get("customer"), info.get("people"),
             info.get("datetime"), info.get("table"), info.get("note"), raw_text, "PENDING", created_at)
        )
        conn.commit()
        return cur.lastrowid


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
    resv_id = save_reservation(group_id, requested_by, info, text)
    detail = _resv_detail_lines(info)
    head = "📅 จองล่วงหน้า" if is_advance else "🔔 จองโต๊ะ"

    detail_msg = TextSendMessage(
        text=f"{head}ใหม่ #{resv_id}\nจากคุณ {requested_by}\n─────────────────\n{detail}")
    confirm_msg = TemplateSendMessage(
        alt_text=f"ยืนยันการจอง #{resv_id}",
        template=ButtonsTemplate(
            title=f"{head} #{resv_id}",
            text="กดยืนยันเมื่อรับจองเรียบร้อย",
            actions=[PostbackAction(
                label="✅ คอนเฟิร์มการจอง",
                data=f"confirm_resv:{resv_id}",
                display_text="คอนเฟิร์มการจอง"
            )],
        ),
    )

    # ปลายทาง: จองล่วงหน้า → กลุ่มบาร์น้ำ / จองวันนี้ → กลุ่มเดิม
    dest = BAR_GROUP_ID if (is_advance and BAR_GROUP_ID) else group_id
    print(f"[resv] จอง #{resv_id} advance={is_advance} → ส่งไป {dest}", flush=True)
    if dest == group_id:
        line_bot_api.reply_message(event.reply_token, [detail_msg, confirm_msg])
    else:
        line_bot_api.push_message(dest, [detail_msg, confirm_msg])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"📤 ส่งจองล่วงหน้า #{resv_id} ไปกลุ่มบาร์น้ำแล้ว รอคอนเฟิร์ม\n─────────────────\n{detail}"))
    return True


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
            text=f"ℹ️ การจอง #{resv_id} ถูกคอนเฟิร์มไปแล้วโดย {resv['confirmed_by']} "
                 f"({resv['confirmed_at']})"))
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


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    # ประมวลผลแบบ background → ตอบ LINE ทันที (กันคอขวด/โทเค็นหมดอายุตอนส่งรูปทีละหลายๆ รูป)
    threading.Thread(target=_process_image_event, args=(event,), daemon=True).start()


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


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text     = event.message.text.strip()
    group_id = getattr(event.source, "group_id", event.source.user_id)
    print(f"[GROUP_ID] source_type={event.source.type} id={group_id} text={text}", flush=True)
    # พิมพ์ "groupid" → บอทตอบ Group ID กลับมาในแชท (ไว้ก๊อปไปตั้งค่า RESV_GROUPS/อื่นๆ ได้ง่าย)
    if text.lower().replace(" ", "") == "groupid":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🆔 Group ID:\n{group_id}"))
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
    # ช่วง start อาจยังรอ disk mount อยู่ — ตอบเร็วโดยไม่แตะ DB กัน /health ค้าง
    if not _storage_ready.is_set():
        return {"status": "starting", "storage": "waiting for /var/data mount..."}
    # ทุกครั้งที่ถูก ping (UptimeRobot ทุก 5 นาที) เช็คว่าถึงเวลาส่งรายงานประจำวันไหม
    try:
        maybe_send_daily_report()
    except Exception as e:
        print(f"[report] health-trigger error: {e}", flush=True)
    with _db() as conn:
        today_count = conn.execute(
            "SELECT COUNT(*) c FROM slips WHERE slip_date=?", (datetime.now(TZ).date().isoformat(),)
        ).fetchone()["c"]
        group_count = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
    return {"status": "ok", "slips_today": today_count, "active_groups": group_count,
            "storage": "persistent" if DB_PERSISTENT else "EPHEMERAL (data lost on restart! add disk at /var/data)"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
