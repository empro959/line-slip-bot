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
# กรุ๊ป "บาร์น้ำ+จองโต๊ะล่วงหน้า" สำหรับรับแจ้งเตือนการจอง (ดู Group ID จาก log [GROUP_ID] บน Render)
BAR_GROUP_ID      = os.environ.get("BAR_GROUP_ID", "")
TZ                = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))

line_bot_api = LineBotApi(LINE_TOKEN)
handler      = WebhookHandler(LINE_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
gemini       = genai.GenerativeModel("gemini-2.5-flash")

# ─── Persistent storage (SQLite) ──────────────────────────────────────────────
import sqlite3

# ใช้ disk ถาวรถ้ามี (Render Persistent Disk mount ที่ /var/data) ไม่งั้นใช้โฟลเดอร์ปัจจุบัน
DB_DIR  = "/var/data" if os.path.isdir("/var/data") else "."
DB_PATH = os.path.join(DB_DIR, "slips.db")

def _db():
    # timeout เผื่อหลาย thread เขียนพร้อมกัน (กัน 'database is locked' ตอนรูปเยอะ)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db() as conn:
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

init_db()
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — AI Visual Analysis (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

def extract_slip_info(image_bytes: bytes) -> dict:
    prompt = (
        "ดูรูปนี้ว่าเป็นสลิป/หลักฐานการโอนเงินจากธนาคารหรือแอปธนาคารหรือไม่ "
        "แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_slip":true,"sender":null,"amount":0.00,"datetime":null,"bank":null,'
        '"account":null,"ref_number":null,"fraud_score":0,"fraud_reasons":[]}\n\n'
        "is_slip: true ถ้าเป็นสลิปโอนเงินจริง, false ถ้าเป็นรูปอื่น (เช่น รูปคน อาหาร ใบเสร็จ เมนู วิว ฯลฯ)\n"
        "ถ้า is_slip เป็น false ให้ใส่ค่าที่เหลือเป็น null/0 ได้เลย\n\n"
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
        admin_msg  = (f"⚠️ [WARN] สลิปจาก {sender} มีจุดน่าสงสัย\n"
                      f"จำนวน: {amount_str} บาท | อ้างอิง: {ref}\n─────────────────\n{issue_text}")
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


def build_daily_report(group_id: str, report_date: str = None) -> str:
    report_date = report_date or datetime.now(TZ).date().isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM slips WHERE group_id=? AND slip_date=? ORDER BY id",
            (group_id, report_date)
        ).fetchall()
    slips = [dict(r) for r in rows]
    if not slips:
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
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Midnight Scheduler (00:30 Asia/Bangkok)
# ══════════════════════════════════════════════════════════════════════════════

def _seconds_until_next_report() -> float:
    now    = datetime.now(TZ)
    target = now.replace(hour=0, minute=30, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def midnight_report_loop():
    while True:
        time.sleep(_seconds_until_next_report())
        yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        with _db() as conn:
            group_ids = [r["group_id"] for r in conn.execute("SELECT group_id FROM groups").fetchall()]
        for group_id in group_ids:
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=build_daily_report(group_id, yesterday)))
            except Exception as e:
                print(f"[midnight] push failed {group_id}: {e}")
        time.sleep(60)


threading.Thread(target=midnight_report_loop, daemon=True).start()


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
        '{"is_reservation":true,"customer":null,"people":null,"datetime":null,'
        '"table":null,"note":null}\n\n'
        "is_reservation: true เฉพาะเมื่อเป็นการจองโต๊ะจริง (มีเจตนานัด/จองที่นั่งให้ลูกค้า)\n"
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
    """ตรวจจับการจองในกรุ๊ปทั่วไป แล้วส่งแจ้งเตือนไปกรุ๊ปบาร์น้ำพร้อมปุ่มคอนเฟิร์ม"""
    if not BAR_GROUP_ID:
        print("[resv] ยังไม่ได้ตั้งค่า BAR_GROUP_ID — ข้ามการแจ้งเตือนจอง", flush=True)
        return False
    # ไม่ตรวจในกรุ๊ปบาร์น้ำเอง (เป็นปลายทาง) และเกตคำเบื้องต้นเพื่อประหยัด quota
    if group_id == BAR_GROUP_ID or not any(h in text.lower() for h in _RESV_HINTS):
        return False

    try:
        info = extract_reservation(text)
    except Exception as e:
        print(f"[resv] extract failed: {e}", flush=True)
        return False
    if not info.get("is_reservation"):
        return False

    requested_by = get_display_name(event.source)
    resv_id = save_reservation(group_id, requested_by, info, text)
    detail = _resv_detail_lines(info)

    # การ์ดในกรุ๊ปบาร์น้ำ + ปุ่มคอนเฟิร์ม (postback ส่งกลับ id)
    body = f"จากคุณ {requested_by}\n─────────────────\n{detail}"
    template = TemplateSendMessage(
        alt_text=f"🔔 จองโต๊ะใหม่ #{resv_id}",
        template=ButtonsTemplate(
            title=f"🔔 จองโต๊ะใหม่ #{resv_id}",
            text=body[:160],
            actions=[PostbackAction(
                label="✅ คอนเฟิร์มการจอง",
                data=f"confirm_resv:{resv_id}",
                display_text="คอนเฟิร์มการจอง"
            )],
        ),
    )
    try:
        line_bot_api.push_message(BAR_GROUP_ID, template)
    except Exception as e:
        print(f"[resv] push to bar group failed: {e}", flush=True)
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="⚠️ ส่งแจ้งเตือนไปกรุ๊ปบาร์น้ำไม่สำเร็จ กรุณาตรวจสอบ BAR_GROUP_ID"))
        return True

    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"📤 ส่งคำขอจอง #{resv_id} ไปยังกรุ๊ปบาร์น้ำแล้ว รอคอนเฟิร์ม\n─────────────────\n{detail}"))
    return True


def handle_reservation_confirm(event, resv_id: int):
    resv = get_reservation(resv_id)
    if not resv:
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text=f"❓ ไม่พบการจอง #{resv_id}"))
        return

    confirmer = get_display_name(event.source)
    detail = _resv_detail_lines(resv)

    if resv["status"] == "CONFIRMED":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"ℹ️ การจอง #{resv_id} ถูกคอนเฟิร์มไปแล้วโดย {resv['confirmed_by']} "
                 f"({resv['confirmed_at']})"))
        return

    mark_reservation_confirmed(resv_id, confirmer)

    # ตอบกลับในกรุ๊ปบาร์น้ำ
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"✅ คอนเฟิร์มการจอง #{resv_id} เรียบร้อย\nโดย {confirmer}\n─────────────────\n{detail}"))

    # แจ้งกลับกรุ๊ปต้นทาง + สรุป
    if resv.get("origin_group_id"):
        try:
            line_bot_api.push_message(resv["origin_group_id"], TextSendMessage(
                text=f"✅ การจอง #{resv_id} ได้รับการคอนเฟิร์มแล้ว!\n"
                     f"ยืนยันโดย {confirmer} (บาร์น้ำ)\n─────────────────\n"
                     f"ผู้แจ้งจอง: {resv.get('requested_by','-')}\n{detail}"))
        except Exception as e:
            print(f"[resv] notify origin failed: {e}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# LINE Webhook
# ══════════════════════════════════════════════════════════════════════════════

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
    """แจ้ง Admin เวลาบอทอ่านสลิปพลาด แบบจำกัด 1 ครั้ง/10 นาที (กัน Admin โดนสแปม)"""
    global _last_admin_error_ts
    if not ADMIN_USER_ID:
        return
    if time.time() - _last_admin_error_ts < 600:
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
    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        info        = extract_slip_info(image_bytes)

        # ถ้าไม่ใช่สลิปโอนเงิน → เงียบไว้ ไม่ต้องตอบอะไร
        if not info.get("is_slip", True):
            print(f"[skip] not a slip, group={group_id}", flush=True)
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
    with _db() as conn:
        today_count = conn.execute(
            "SELECT COUNT(*) c FROM slips WHERE slip_date=?", (datetime.now(TZ).date().isoformat(),)
        ).fetchone()["c"]
        group_count = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
    return {"status": "ok", "slips_today": today_count, "active_groups": group_count}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
