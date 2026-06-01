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
from linebot.models import MessageEvent, ImageMessage, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

LINE_TOKEN        = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET       = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
ADMIN_USER_ID     = os.environ.get("LINE_ADMIN_USER_ID", "")
PROMPTPAY_API_KEY = os.environ.get("PROMPTPAY_API_KEY", "")
TZ                = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))

line_bot_api = LineBotApi(LINE_TOKEN)
handler      = WebhookHandler(LINE_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
gemini       = genai.GenerativeModel("gemini-2.0-flash")

# ─── In-memory storage ────────────────────────────────────────────────────────
daily_slips      = {}
seen_ref_numbers = {}
active_groups    = set()
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
        "ถ้า is_slip เป็น false ให้ใส่ค่าที่เหลือเป็น null/0 ได้เลย\n"
        "fraud_score: 0-100 (0=ปลอดภัย, 100=น่าสงสัยมาก)\n"
        "fraud_reasons: เหตุผลที่น่าสงสัย เช่น ฟอนต์ผิดปกติ, ตัดต่อ, โลโก้ไม่ถูกต้อง"
    )
    img_part = {"mime_type": "image/jpeg", "data": image_bytes}
    response = gemini.generate_content([prompt, img_part])
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

def check_duplicate(group_id: str, ref_number: str) -> bool:
    if not ref_number:
        return False
    refs = seen_ref_numbers.setdefault(group_id, set())
    if ref_number in refs:
        return True
    refs.add(ref_number)
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Verdict Builder
# ══════════════════════════════════════════════════════════════════════════════

def build_verdict(info: dict, promptpay: dict, is_duplicate: bool) -> dict:
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

    if is_duplicate:
        issues.append(f"🔴 เลขอ้างอิง {info.get('ref_number')} เคยถูกส่งมาแล้ว! (สลิปซ้ำ)")

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
    today = date.today().isoformat()
    key   = f"{group_id}_{today}"
    daily_slips.setdefault(key, [])
    info["recorded_at"] = datetime.now(TZ).strftime("%H:%M:%S")
    info["verdict"]     = verdict_status
    daily_slips[key].append(info)
    active_groups.add(group_id)


def build_daily_report(group_id: str, report_date: str = None) -> str:
    report_date = report_date or date.today().isoformat()
    slips = daily_slips.get(f"{group_id}_{report_date}", [])
    if not slips:
        return f"📊 ไม่มีสลิปวันที่ {report_date}"

    total  = sum(float(s.get("amount") or 0) for s in slips)
    passed = sum(1 for s in slips if s.get("verdict") == "PASS")
    warned = sum(1 for s in slips if s.get("verdict") == "WARN")
    failed = sum(1 for s in slips if s.get("verdict") == "FAIL")
    icons  = {"PASS": "✅", "WARN": "⚠️", "FAIL": "🚨"}

    lines = [
        f"📊 รายงานสรุปประจำวัน {report_date}",
        "─────────────────",
        f"รวม {len(slips)} รายการ | {total:,.2f} บาท",
        f"✅ ผ่าน {passed}  ⚠️ น่าสงสัย {warned}  🚨 ปลอม {failed}", "",
    ]
    for i, s in enumerate(slips, 1):
        amt  = f"{float(s.get('amount', 0)):,.2f}" if s.get("amount") else "?"
        icon = icons.get(s.get("verdict", ""), "❓")
        lines.append(f"{i}. {icon} {s.get('sender','?')} | {amt} บาท | {s.get('recorded_at','')}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Midnight Scheduler (00:10 Asia/Bangkok)
# ══════════════════════════════════════════════════════════════════════════════

def _seconds_until_next_0010() -> float:
    now    = datetime.now(TZ)
    target = now.replace(hour=0, minute=10, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def midnight_report_loop():
    while True:
        time.sleep(_seconds_until_next_0010())
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        for group_id in list(active_groups):
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=build_daily_report(group_id, yesterday)))
            except Exception as e:
                print(f"[midnight] push failed {group_id}: {e}")
        time.sleep(60)


threading.Thread(target=midnight_report_loop, daemon=True).start()


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


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    group_id    = getattr(event.source, "group_id", event.source.user_id)
    content     = line_bot_api.get_message_content(event.message.id)
    image_bytes = b"".join(chunk for chunk in content.iter_content())

    try:
        info      = extract_slip_info(image_bytes)

        # ถ้าไม่ใช่สลิปโอนเงิน → เงียบไว้ ไม่ต้องตอบอะไร
        if not info.get("is_slip", True):
            print(f"[skip] not a slip, group={group_id}", flush=True)
            return

        promptpay = verify_with_promptpay(image_bytes)
        is_dup    = check_duplicate(group_id, info.get("ref_number"))
        verdict   = build_verdict(info, promptpay, is_dup)
        save_slip(group_id, info, verdict["status"])

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=verdict["group_msg"]))
        if verdict["admin_msg"] and ADMIN_USER_ID:
            line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=verdict["admin_msg"]))
    except Exception as e:
        # อ่าน error แล้วเงียบไว้ ไม่รบกวนกรุ๊ป (กันรูปที่ไม่ใช่สลิปโดนทัก)
        print(f"[error] handle_image group={group_id}: {e}", flush=True)


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text     = event.message.text.strip()
    group_id = getattr(event.source, "group_id", event.source.user_id)
    print(f"[GROUP_ID] source_type={event.source.type} id={group_id} text={text}", flush=True)
    if any(kw in text for kw in ["สรุป", "รายงาน", "report", "summary"]):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_daily_report(group_id)))


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "slips_today": sum(len(v) for v in daily_slips.values()),
            "active_groups": len(active_groups)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
