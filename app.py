import os
import re
import json
import hmac
import hashlib
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
    LeaveEvent, JoinEvent, UnsendEvent,
)
from google import genai
from google.genai import types

app = Flask(__name__)

LINE_TOKEN        = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET       = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
# เจ้าของ 1 คนหรือหลายคนก็ได้ — ใส่หลายรหัสคั่นด้วยคอมมา (เช่น "U111...,U222...")
ADMIN_USER_IDS    = [u.strip() for u in os.environ.get("LINE_ADMIN_USER_ID", "").replace("\n", ",").split(",") if u.strip()]
ADMIN_USER_ID     = ADMIN_USER_IDS[0] if ADMIN_USER_IDS else ""   # ตัวแรก = ใช้กับแจ้งเตือนระบบ (mem/error) พอ
PROMPTPAY_API_KEY = os.environ.get("PROMPTPAY_API_KEY", "")
# กลุ่มที่ให้ "เช็คสลิป + เตือน + ส่งรายงาน" เท่านั้น (คั่นด้วยคอมมา) — เว้นว่าง = ทุกกลุ่ม
SLIP_GROUPS       = [g.strip() for g in os.environ.get("SLIP_GROUPS", "").split(",") if g.strip()]
# กลุ่มที่ "เปิดรับจองโต๊ะ" (คั่นด้วยคอมมา) — จองวันนี้ การ์ด+ปุ่มขึ้นในกลุ่มนั้น / เว้นว่าง = ปิด
RESV_GROUPS       = [g.strip() for g in os.environ.get("RESV_GROUPS", "").split(",") if g.strip()]
# กลุ่มที่ "ปิดการจองโต๊ะ" — ไม่ยุ่งกับการจองเลย (ทั้งจองวันนี้และล่วงหน้า) เช่นกลุ่ม the riches
RESV_EXCLUDE_GROUPS = [g.strip() for g in os.environ.get("RESV_EXCLUDE_GROUPS", "").split(",") if g.strip()]
# แผน B: กลุ่มที่ "เด้งข้อมูลจอง (ไม่มีปุ่ม) ให้รับรู้" เช่น บาร์น้ำ, sound — คอนเฟิร์มยังทำที่กลุ่มรับจอง (นับสลิป) ที่เดียว
# ตั้งไว้ = เปิดโหมดแผน B: จองทุกใบคอนเฟิร์มที่กลุ่มเดิม + เด้งสำเนาข้อมูลไปกลุ่มพวกนี้ (push ผ่าน OA ของกลุ่มนั้น)
RESV_INFO_GROUPS  = [g.strip() for g in os.environ.get("RESV_INFO_GROUPS", "").split(",") if g.strip()]
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
# กลุ่ม "staff" — จองวันนี้จากกลุ่มไหนก็ตามส่งการ์ด+ปุ่มมาที่นี่ให้ staff คอนเฟิร์ม + เป็นปลายทางสรุปจองรายวัน
# ดีฟอลต์ = กลุ่มส่งสลิป (SLIP_GROUPS[0]) เพราะเจ้าของยืนยันว่า staff = กลุ่มเดียวกับกลุ่มส่งสลิป
STAFF_GROUP_ID    = os.environ.get("STAFF_GROUP_ID", "") or (SLIP_GROUPS[0] if SLIP_GROUPS else "")
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
# ── ระบบกระทบบิล-สลิป (โอนขาด/เกิน) — เฉพาะ DINING_GROUPS ───────────────────────
# เทียบ "ยอดบิลร้าน (ใบแจ้งรายการ)" กับ "ยอดสลิปที่ลูกค้าโอน": โอนขาด>เกณฑ์ → เตือน, โอนเกิน → สะสมเป็นยอดทริป
# เว้นว่าง = ปิดฟีเจอร์ทั้งหมด (บิลถูกมองเป็น 'ไม่ใช่สลิป' ตามเดิม). ใส่ group id (คั่นคอมมา) เพื่อเปิด — ต้องเป็นกลุ่มใน SLIP_GROUPS ด้วย
DINING_GROUPS      = [g.strip() for g in os.environ.get("DINING_GROUPS", "").split(",") if g.strip()]
DINING_SHORT_BAHT  = float(os.environ.get("DINING_SHORT_BAHT", "1"))  # เตือนเมื่อโอน 'ขาด' ตั้งแต่กี่บาทขึ้นไป (ดีฟอลต์ ≥1)
DINING_MATCH_MIN   = int(os.environ.get("DINING_MATCH_MIN", "45"))    # บิลค้างรอจับคู่สลิปได้นานกี่นาที (เกินนี้=หมดอายุ ไม่จับคู่)
# หน่วงเวลาก่อน 'จับคู่บิล-สลิป' (วินาที) — รอให้บิลที่ส่งไล่ๆ กันถูกอ่านเข้าคิวครบก่อน กันจับคู่สลับลำดับ
# (ยอดสลิป/ตรวจปลอม ยังบันทึก+ตอบทันที ไม่หน่วง; หน่วงเฉพาะการเทียบบิล → เตือนโอนขาดแบบ push)
DINING_MATCH_DELAY = int(os.environ.get("DINING_MATCH_DELAY", "90"))
# เฝ้า memory: ถ้า RSS เกินค่านี้ (MB) บอทจะ DM เตือนแอดมิน (Render Starter ลิมิต 512MB) — กันก่อน OOM/restart
MEM_WARN_MB = float(os.environ.get("MEM_WARN_MB", "430"))
# จองที่ยังไม่คอนเฟิร์ม (ทั้งจองวันนี้และจองล่วงหน้า) จะแจ้งเตือนซ้ำทุก RESV_NAG_INTERVAL_MIN นาที (ทุกช่วงเวลาเท่ากัน)
# ตื๊อได้ไม่เกิน RESV_NAG_MAX_HOURS ชั่วโมงนับจากตอนรับจอง (กันตื๊อไม่จบ) — จองล่วงหน้ายังโผล่ในสรุปรอบเช้าวันงานอีกครั้ง
RESV_NAG_MAX_HOURS   = float(os.environ.get("RESV_NAG_MAX_HOURS", "3"))
RESV_NAG_INTERVAL_MIN = int(os.environ.get("RESV_NAG_INTERVAL_MIN", "30"))
# รายงานจองอัตโนมัติ 2 รอบ/วัน (ส่งครั้งเดียว/วันต่อรอบ ในกรอบ [ชม.นั้น, +3)) — ตั้งชั่วโมง 0-23
RESV_ADVANCE_SUMMARY_HOUR = int(os.environ.get("RESV_ADVANCE_SUMMARY_HOUR", "11"))  # รายงาน 'จองล่วงหน้าที่ถึงวันงานวันนี้' รอบเช้า
RESV_TODAY_SUMMARY_HOUR   = int(os.environ.get("RESV_TODAY_SUMMARY_HOUR", "16"))    # สรุป 'จองในวัน' (จองวันนี้เพื่อวันนี้) รอบบ่าย
# รายงานสรุปจองล่วงหน้า (เข้ากลุ่มบาร์น้ำหลังเที่ยงคืน) — แสดงจองล่วงหน้าที่แจ้งมาภายในกี่วันล่าสุด (ใช้กับจองที่ไม่มีวันที่จริง)
RESV_REPORT_DAYS   = int(os.environ.get("RESV_REPORT_DAYS", "7"))
# ลบข้อมูลเก่าอัตโนมัติ (วันละครั้ง) กัน DB บวม
RESV_KEEP_DAYS     = int(os.environ.get("RESV_KEEP_DAYS", "15"))  # จองที่ผ่านวันมาแล้ว เก็บ 15 วัน
MISS_KEEP_DAYS     = int(os.environ.get("MISS_KEEP_DAYS", "14"))  # ตัวนับรูปตกหล่น เก็บ 14 วัน
SLIP_KEEP_DAYS     = int(os.environ.get("SLIP_KEEP_DAYS", "60"))  # สลิป เก็บ 60 วัน
# เวลาเปิดร้าน (ชั่วโมง) สำหรับตรวจเวลาจอง — เปิด 11:00 ถึง 00:00 (เที่ยงคืน)
OPEN_HOUR, CLOSE_HOUR = 11, 24
# ช่วง 'เงียบลึก' (ร้านปิดดึก–เช้า ไม่มีงานตามเวลา: nag ดึกจบแล้ว, หลังรายงาน 00:30, ก่อนรายงานจอง 11:00)
# ในช่วงนี้ background loops + /health จะ 'หยุด query DB' → ปล่อยให้ Neon (serverless) หลับ ประหยัด compute (กันชนลิมิต free)
QUIET_START_HOUR = int(os.environ.get("QUIET_START_HOUR", "3"))
QUIET_END_HOUR   = int(os.environ.get("QUIET_END_HOUR", "10"))
RESV_LOOP_SEC    = int(os.environ.get("RESV_LOOP_SEC", "120"))  # รอบเช็คจอง PENDING (nag ทุก 30 นาทีอยู่แล้ว 120s พอ)
TZ                = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))


def _in_deep_quiet() -> bool:
    """อยู่ช่วง 'เงียบลึก' ไหม — ใช้หยุด poll DB ปล่อย Neon หลับ (ประหยัด compute) เมื่อไม่มีงานตามเวลา"""
    h = datetime.now(TZ).hour
    if QUIET_START_HOUR <= QUIET_END_HOUR:
        return QUIET_START_HOUR <= h < QUIET_END_HOUR
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR   # เผื่อ window ข้ามเที่ยงคืน

line_bot_api = LineBotApi(LINE_TOKEN)
handler      = WebhookHandler(LINE_SECRET)

# แจ้งเตือนเจ้าของ/กลุ่ม management: ส่งผ่าน OA แยกได้ (ตั้ง env LINE_ADMIN_PUSH_TOKEN = access token ของ OA อื่น เช่น OA2)
# ไม่ตั้ง = ใช้ OA เดิม (บอทเช็กสลิป) เหมือนเดิม
_ADMIN_PUSH_TOKEN = os.environ.get("LINE_ADMIN_PUSH_TOKEN", "").strip()
admin_push_api = LineBotApi(_ADMIN_PUSH_TOKEN) if _ADMIN_PUSH_TOKEN else line_bot_api
# ── Failover หลาย LINE OA เพื่อประหยัดค่าโควต้า push ────────────────────────────
# reply ฟรีไม่นับโควต้า (ผูก reply_token) — แต่ push/broadcast กินโควต้าฟรีเดือนละ ~300 ข้อความ/OA
# แนวคิด: ใช้ OA1 (ตัวหลัก รับ event+reply) จนใกล้เต็มโควต้าเดือนนี้ แล้วสลับ push ไป OA2, OA3, ... อัตโนมัติ
#   → ไม่ต้องบีบให้อยู่ใต้ 300 (เดี๋ยวข้อความตกหล่น) และไม่ต้องจ่ายแพ็กเกจเสียเงิน
# OA สำรองต้อง: (1) เชิญเข้าทุกกลุ่มเหมือน OA1 (2) ปิด auto-reply + ปิด webhook (ตัวสำรอง push อย่างเดียว)
# ตั้ง token สำรองใน env: LINE_CHANNEL_ACCESS_TOKEN_2, _3, _4, _5 — ใส่แค่ตัวไหนก็เปิดใช้ตัวนั้นทันที
# โควต้า push ฟรี/เดือน/OA — แผนฟรีของไทย = 300/เดือน (แพ็กเสียเงินสูงกว่านี้ เช่น 15,000)
# ตั้ง env ให้ตรงแพ็กเกจจริงของ OA ที่ใช้อยู่ (เช่นตอนเอดมีแพ็ก 15,000 → PUSH_FREE_LIMIT=15000)
# ⚠️ LINE นับ 'push เข้ากลุ่ม' = จำนวนสมาชิกกลุ่ม (ไม่ใช่ 1) — ดู _recipient_count
PUSH_FREE_LIMIT = int(os.environ.get("PUSH_FREE_LIMIT", "300"))
# เตือนเจ้าของครั้งเดียว/เดือน เมื่อ 'OA ตัวสุดท้าย' (ไม่มีตัวสำรองต่อ) push ใกล้เต็ม — ใช้เป็นสัญญาณว่า
# 'push ไม่พอแล้ว' ถึงเวลาพิจารณาแยกกลุ่มไปอีก OA (แผน B) หรือลด push ก่อนข้อความตกหล่น
PUSH_WARN_RATIO = float(os.environ.get("PUSH_WARN_RATIO", "0.8"))
PUSH_WARN_AT    = max(1, int(PUSH_FREE_LIMIT * PUSH_WARN_RATIO))
_push_apis = [line_bot_api]        # index 0 = OA1 (ตัวหลัก)
for _i in range(2, 10):            # รองรับ OA2..OA9 (เพิ่ม OA ได้แค่ตั้ง env LINE_CHANNEL_ACCESS_TOKEN_6/_7/... )
    _tok2 = os.environ.get(f"LINE_CHANNEL_ACCESS_TOKEN_{_i}")
    if _tok2:
        _push_apis.append(LineBotApi(_tok2))
# แผน B (แยกกลุ่มคนละ OA): แมป 'กลุ่ม → OA ตัวไหน' — LINE ให้ OA อยู่ได้กลุ่มละตัว จึง push กลุ่มนั้นด้วย token ตัวนั้น
# รูปแบบ env OA_ROUTE="Cxxxx:2,Cyyyy:3,Czzzz:4" (groupid:เลข OA แบบ 1-based). ไม่ตั้ง = โหมดเดิม (OA1 + failover cascade)
_oa_route = {}     # gid -> [ลำดับ OA (0-based)]; หลายตัว = สลับอัตโนมัติเมื่อเต็ม เช่น "Cxxxx:3/4/2"
for _pair in os.environ.get("OA_ROUTE", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _gid, _nums = _pair.rsplit(":", 1)
        _ixs = []
        for _n in _nums.replace("|", "/").split("/"):   # รองรับหลาย OA คั่นด้วย / (หรือ |)
            try:
                _k = int(_n.strip()) - 1
            except ValueError:
                continue
            if _k >= 0 and _k not in _ixs:
                _ixs.append(_k)
        if _gid.strip() and _ixs:
            _oa_route[_gid.strip()] = _ixs
_push_lock = threading.Lock()      # กัน race นับโควต้าจากหลาย thread (background jobs + handlers)
# รับชื่อย่อ (flash/pro/flash-lite) → เติมเป็นชื่อโมเดลเต็มให้อัตโนมัติ กันพลาดตอนตั้ง env ผิด (เช่นใส่แค่ 'flash')
_GEMINI_ALIASES = {"flash": "gemini-2.5-flash", "pro": "gemini-2.5-pro", "flash-lite": "gemini-2.5-flash-lite"}
def _norm_gemini_model(m: str) -> str:
    m = (m or "").strip()
    return _GEMINI_ALIASES.get(m.lower(), m)
GEMINI_MODEL = _norm_gemini_model(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
# โมเดลสำหรับ 'อ่านซ้ำ' เมื่อรอบแรกอ่านไม่ออก — เดิม pro (เก่งกู้ใบยาก แต่ลิมิต RPM ต่ำ); ตั้ง flash เพื่อหนีคอขวด pro ช่วงพีค
GEMINI_MODEL_RETRY = _norm_gemini_model(os.environ.get("GEMINI_MODEL_RETRY", "gemini-2.5-pro"))
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def _gemini_generate(contents, attempts=4, model=None, json_mode=False):
    """เรียก Gemini (SDK google-genai) พร้อม retry + backoff — กัน 503 overload/สะดุดชั่วคราว
    contents เป็น str (ข้อความ) หรือ list [str, รูป]; รันใน background thread จึงรอ backoff ได้
    model=None → ใช้ GEMINI_MODEL (flash); ระบุได้เพื่ออ่านซ้ำด้วยตัวเก่งขึ้น (pro)
    json_mode=True → บังคับให้ตอบเป็น JSON (กันโมเดลตอบเป็นข้อความบรรยายแล้ว parse พัง)"""
    use_model = model or GEMINI_MODEL
    config = types.GenerateContentConfig(response_mime_type="application/json") if json_mode else None
    last = None
    for i in range(attempts):
        try:
            return gemini_client.models.generate_content(model=use_model, contents=contents, config=config)
        except Exception as e:
            last = e
            if i < attempts - 1:
                print(f"[gemini] retry {i+1}/{attempts}: {str(e)[:100]}", flush=True)
                time.sleep(2 * (2 ** i))   # 2, 4, 8 วินาที
    raise last


def _parse_gemini_json(response, fallback: dict) -> dict:
    """แปลงผลลัพธ์ Gemini เป็น dict อย่างทนทาน — กันเคสโมเดลตอบไม่เป็น JSON (เช่น โน๊ตเขียนมือ/รูปกำกวม
    ทำให้ตอบเป็นข้อความบรรยาย) แล้ว json.loads โยน error หลุดไป 'error path' → เตือนผิดว่า 'ส่งสลิปใหม่'
    ถ้า parse ตรงๆ ไม่ได้ จะลองดึงบล็อก {...} แรกออกมา; ถ้ายังไม่ได้คืน fallback (ถือว่าไม่ใช่สลิป — เงียบ)"""
    raw = (getattr(response, "text", None) or "").strip().replace("```json", "").replace("```", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    pass
    print(f"[gemini] parse JSON ไม่ได้ → ใช้ fallback: {raw[:120]!r}", flush=True)
    return dict(fallback)

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
                recorded_at TEXT,
                paid        {_REAL} DEFAULT 0
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
                recorded_at   TEXT,
                allocated     {_REAL} DEFAULT 0,
                settle_note   TEXT
            )
        """)
        # ── ระบบกระทบบิล-สลิป (โอนขาด/เกิน) — DINING_GROUPS ──
        # บิลร้าน (ใบแจ้งรายการ) ที่ส่งเข้ามา รอจับคู่กับสลิปโอน (FIFO ตามเวลา); matched: 0=รอ 1=จับคู่/รีเซ็ตแล้ว 2=หมดอายุ
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS dining_bills (
                id          {_PK},
                group_id    TEXT,
                bill_date   TEXT,
                table_no    TEXT,
                amount      {_REAL},
                recorded_at TEXT,
                message_id  TEXT,
                matched     INTEGER DEFAULT 0
            )
        """)
        # ผลการจับคู่บิล↔สลิป (diff = สลิป − บิล; ลบ=โอนขาด) ไว้สรุป/ตรวจย้อน
        # collected: โอนขาดที่พนักงาน 'เก็บส่วนต่างครบแล้ว' (กดปุ่มเคลียร์โต๊ะนั้น) → ตัดออกจากยอดสะสม
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS dining_pairs (
                id          {_PK},
                group_id    TEXT,
                pair_date   TEXT,
                table_no    TEXT,
                bill_amount {_REAL},
                slip_amount {_REAL},
                diff        {_REAL},
                recorded_at TEXT,
                source      TEXT,
                collected   INTEGER DEFAULT 0
            )
        """)
        # migration: collected + slip_id ให้ตาราง dining_pairs ที่สร้างก่อนมีคอลัมน์นี้
        # slip_id = id แถวใน slips ของสลิปคู่นี้ (ไว้ให้ปุ่ม 'บอทอ่านผิด' แก้ยอดในตาราง slips ได้)
        for _dcol in ("collected INTEGER DEFAULT 0", "slip_id INTEGER"):
            if USE_PG:
                conn.execute(f"ALTER TABLE dining_pairs ADD COLUMN IF NOT EXISTS {_dcol}")
            else:
                try:
                    conn.execute(f"ALTER TABLE dining_pairs ADD COLUMN {_dcol}")
                except Exception:
                    pass
        # นับรูปที่รับเข้ามาแต่ "ไม่ได้บันทึกเป็นสลิป" (อ่านไม่ออก/ไม่ใช่สลิป/error) ไว้กระทบยอดในรายงาน
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS image_misses (
                id          {_PK},
                group_id    TEXT,
                stat_date   TEXT,
                reason      TEXT,
                recorded_at TEXT,
                detail      TEXT
            )
        """)
        # migration: detail = ยอด/ปลายทางของใบที่ถูกข้าม (ไว้ดูว่า 'ข้ามไม่ใช่รายรับ' เข้าบัญชีอะไร) — ทั้ง PG/SQLite
        if USE_PG:
            conn.execute("ALTER TABLE image_misses ADD COLUMN IF NOT EXISTS detail TEXT")
        else:
            try:
                conn.execute("ALTER TABLE image_misses ADD COLUMN detail TEXT")
            except Exception:
                pass
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
        # migration: allocated = ยอดของสลิปจ่ายที่ถูก 'ตัด' เข้ากับบรรทัดบิล/ค้างโดยตรง (กันนับซ้ำ — ส่วนที่เหลือถึงลดยอดรวม)
        #            settle_note = วันที่ของบรรทัดค้างที่สลิปนี้ไปตัด (ไว้โชว์ในรายงานว่า 'จ่ายตัดของวันไหน' กันจับผิดเงียบ)
        for _pcol in (f"allocated {_REAL} DEFAULT 0", "settle_note TEXT"):
            if USE_PG:
                conn.execute(f"ALTER TABLE payable_payments ADD COLUMN IF NOT EXISTS {_pcol}")
            else:
                try:
                    conn.execute(f"ALTER TABLE payable_payments ADD COLUMN {_pcol}")
                except Exception:
                    pass
        # migration: paid = ยอดที่ถูกจ่ายตัดของบรรทัดบิล/ค้างนั้น (ไม่ลบบรรทัด — โชว์ในรายงานรอบนี้ แล้วลบบรรทัดที่จ่ายครบตอนสรุปรอบถัดไป)
        if USE_PG:
            conn.execute(f"ALTER TABLE payable_bills ADD COLUMN IF NOT EXISTS paid {_REAL} DEFAULT 0")
        else:
            try:
                conn.execute(f"ALTER TABLE payable_bills ADD COLUMN paid {_REAL} DEFAULT 0")
            except Exception:
                pass
        conn.commit()
try:
    _ensure_init()
    print(f"[STORAGE] ✅ ใช้ {STORAGE_DESC}", flush=True)
except Exception as e:
    print(f"🔴 [STORAGE] init ตอน start ยังไม่ได้ (จะลองใหม่ตอนมี request): {e}", flush=True)
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — AI Visual Analysis (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

def extract_slip_info(image_bytes: bytes, retry: bool = False, dining: bool = False) -> dict:
    retry_note = (
        "🔁 นี่คือการอ่าน 'รอบสอง' — รอบแรกตอบว่าไม่ใช่สลิป แต่รูปนี้ถูกส่งในกลุ่มรับสลิป "
        "จึงมีโอกาสสูงที่จะเป็นสลิป หรือมี 'สลิปโอน' อยู่ในรูป (อาจถ่ายคู่กับใบบิลร้าน/ถ่ายจากหน้าจอ/เอียง/เบลอ/แสงน้อย) "
        "โปรดเพ่งดูให้ละเอียดที่สุด ถ้าพอเห็นจำนวนเงิน + ร่องรอยการโอน/จ่ายสำเร็จ ให้ is_slip=true ไว้ก่อน "
        "(แต่ถ้าเป็น 'กระดาษเขียนด้วยลายมือ-ปากกา' ยังคง is_slip=false, amount=0 — ลายมือไม่ใช่สลิป)\n\n"
    ) if retry else ""
    # คำสั่ง 'อ่านบิลร้าน' ใส่เฉพาะกลุ่มที่เปิดระบบกระทบบิล-สลิป (DINING_GROUPS) เท่านั้น
    # → กลุ่มสลิปอื่นได้ prompt เดิมเป๊ะ ไม่กระทบการอ่านสลิปปกติ
    _bill_json = ',"is_bill":false,"bill_total":0.00,"bill_table":null' if dining else ''
    _bill_instr = (
        "📋 บิลร้านอาหาร (ใบแจ้งรายการ): ถ้าในรูปมี 'บิลร้าน' (มีเลขโต๊ะ + รายการอาหาร + ยอดรวม/สุทธิ) ให้ดึงเพิ่ม:\n"
        "  - bill_total: ยอด 'สุทธิ' หรือ 'ยอดรวมที่ต้องจ่าย' บนบิลร้าน (เลขรวมสุดท้ายที่ลูกค้าต้องจ่าย — ไม่ใช่ยอดก่อน VAT/ยอดย่อยรายหมวด)\n"
        "  - bill_table: เลขโต๊ะบนบิล (เช่น B16, A3) ถ้ามี ไม่งั้น null\n"
        "  - is_bill: true 'เฉพาะ' เมื่อรูปนี้เป็นบิลร้านล้วนๆ ไม่มีสลิปโอน/หลักฐานจ่ายเงินในรูป "
        "(กรณีนี้ is_slip=false); ถ้ามีสลิปโอนในรูปด้วย (เดี่ยวหรือคู่กับบิล) → is_slip=true, is_bill=false ตามเดิม "
        "แต่ยังคงดึง bill_total/bill_table จากบิลที่เห็นในรูปมาด้วย\n"
    ) if dining else ''
    prompt = (
        retry_note +
        "ดูรูปนี้ว่าเป็นสลิป/หลักฐานการชำระเงินให้ร้านหรือไม่ "
        "(โอนผ่านธนาคาร/แอปธนาคาร หรือจ่ายผ่านเป๋าตัง/G-Wallet/รัฐช่วยจ่าย เช่น ไทยช่วยไทย คนละครึ่ง เราชนะ) "
        "แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"is_slip":true,"sender":null,"amount":0.00,"datetime":null,"bank":null,'
        '"account":null,"receiver":null,"receiver_account":null,"ref_number":null,"cropped":false,"fraud_score":0,"fraud_reasons":[],"is_card_settlement":false'
        + _bill_json + '}\n\n'
        "is_slip: true ถ้าเป็นหลักฐานการชำระเงิน/โอนเงิน/เติมเงินสำเร็จ "
        "(สลิปโอนทุกธนาคาร, เติมเงินพร้อมเพย์/วอลเล็ต, เป๋าตัง/รัฐช่วยจ่าย). "
        "ตีความกว้างไว้ก่อน: เห็นจำนวนเงิน + คำว่าสำเร็จ/วันเวลา/เลขอ้างอิง แม้ถ่ายจากจอ/เอียง/เบลอ → true. "
        "false เฉพาะรูปที่ไม่ใช่การเงินชัดๆ (คน/อาหาร/เมนู/วิว/สติกเกอร์)\n"
        "💳 'ใบรูดบัตรเครดิต/เดบิต' จากเครื่อง EDC (มีคำว่า SETTLEMENT REPORT / SALES / CARD TOTALS / Card Name / "
        "MASTERCARD / VISA / MERCHANT COPY / Settlement Successful / รหัส TID·MID·BATCH) = จ่ายด้วยบัตร ไม่ใช่สลิปโอนธนาคาร "
        "→ ตั้ง is_card_settlement=true (อ่าน amount ตามปกติได้); สลิปโอน/จ่ายบิลผ่านแอปธนาคารทั่วไป (K+/Krungthai NEXT/ฯลฯ) ตั้ง is_card_settlement=false\n"
        "❌ สำคัญมาก: 'กระดาษ/โน้ตที่เขียนด้วยลายมือ-ปากกา' (เช่น จดสต๊อก/จดของ/จดรายการ/จดยอด เขียนมือ) "
        "ไม่ใช่สลิป/หลักฐานการโอน → is_slip=false และ amount=0 'เสมอ' แม้จะเห็นตัวเลขบนกระดาษ; "
        "สลิป/หลักฐานโอนจริงเป็น 'ภาพพิมพ์จากแอปธนาคาร/วอลเล็ต' (ฟอนต์พิมพ์ ปุ่ม โลโก้ เลขอ้างอิง คำว่าสำเร็จ) ไม่ใช่ลายมือบนกระดาษ\n"
        "⚠️ กรณีที่ 'ยังนับเป็นสลิป (is_slip=true)' แม้รูปแบบต่างไป:\n"
        "  - สลิปภาษาอังกฤษ ('Transaction successful', 'From/To', 'Bill Payment Successful', 'Top up') = สลิปเหมือนกัน\n"
        "  - 'จ่ายบิล/ชำระเงินสำเร็จ/Scan to Pay/เติมเงิน' เข้า 'ร้านค้า' (มีรหัสร้านค้า เช่น KBxxxxxxxxx / SAI YANG SOI 4) = หลักฐานจ่ายให้ร้าน → true, amount = จำนวนเงินที่จ่าย\n"
        "  - สลิปที่มี 'ลายตกแต่ง/ธีมการ์ตูน/ดอกไม้/เทศกาล' เป็นพื้นหลัง (เช่น K+ ธีม My Melody/Sanrio, 'มีสุข สมหวัง') = ยังเป็นสลิป ไม่ใช่สติกเกอร์ → อ่านข้อความรายการ (ยอด/ผู้รับ/เลขที่รายการ) ตามปกติ\n"
        "  - ถ้ามี 'แถบแจ้งเตือน/ป๊อปอัป' เด้งทับด้านบนจอ (เช่น 'บันทึกสลิป/eSlip saved', 'เงินออก -xxx', ปุ่ม Mute/Reply) ให้ 'มองข้ามแถบนั้น' แล้วอ่านสลิปที่อยู่ข้างล่าง — แถบเด้งไม่ทำให้ไม่ใช่สลิป\n"
        "ถ้ารูปมีทั้งใบบิลร้านและสลิปโอนคู่กัน → true และอ่านยอดจาก 'สลิปโอน' (ไม่ใช่ยอดบนบิลร้าน)\n"
        + _bill_instr +
        "สำคัญ: ถ้าเห็นจำนวนเงินบนรูป ให้อ่าน amount มาเสมอ แม้ไม่แน่ใจว่าเป็นสลิป (อย่าใส่ 0 ทั้งที่เห็นยอด) "
        "— ยกเว้นกรณีเดียว: ถ้าเป็น 'กระดาษเขียนมือ' ให้ amount=0 (ไม่ต้องอ่านเลขจากลายมือ)\n"
        "amount: จำนวนเงินที่ 'ร้านได้รับจริง' (อ่านจากสลิปโอน ไม่ใช่จากบิลร้าน)\n"
        "  - สลิปโอนธนาคารทั่วไป = ยอดที่โอน\n"
        "  - สลิปจ่ายผ่านรัฐช่วยจ่าย/เป๋าตัง (เช่น 'ไทยช่วยไทย' 'คนละครึ่ง' '60/40') ที่มีหลายยอด: "
        "ให้ใช้ยอดบรรทัด 'ค่าสินค้า/บริการ' (ยอดเต็มที่ร้านได้รับ) เท่านั้น "
        "ห้ามใช้ 'จำนวนเงินที่ชำระ' (ส่วนที่ลูกค้าจ่ายเอง มักติดลบ) หรือ 'จดบันทึก' (ส่วนที่รัฐช่วย)\n"

        "sender: ชื่อบัญชี 'ผู้โอน/ต้นทาง' (ฝั่ง 'จาก') ตามที่เห็นในสลิป\n"
        "receiver: ชื่อบัญชี 'ปลายทาง/ผู้รับเงิน' (ฝั่ง 'ไปยัง') ตามที่เห็นในสลิป (ถ้ามีข้อมูลเพิ่มในวงเล็บก็ใส่ด้วย)\n"
        "⚠️ สลิป 'จ่ายบิล/ชำระเงินร้านค้า/Scan to Pay' (Krungthai/ttb ฯลฯ): receiver = 'ชื่อร้านค้าที่รับเงิน' ที่อยู่ใต้ลูกศร/ฝั่งผู้รับ "
        "(เช่น SAI YANG SOI 4) — ⚠️ ห้ามเอาชื่อคนฝั่ง 'จาก/ผู้จ่าย' (เช่น นาย...) มาเป็น receiver เด็ดขาด; "
        "receiver_account = 'รหัสร้านค้า' (เช่น KB000001944107) ถ้ามี\n"
        "receiver_account: เลขบัญชีปลายทาง/ผู้รับ (ตามที่เห็น แม้ถูกปิดบางส่วน เช่น xxx-x-x4818-x ก็ใส่)\n"
        "ref_number: ใช้ 'รหัสอ้างอิง/เลขที่รายการ' ที่ 'ไม่ซ้ำต่อรายการ' (เลขยาวสุ่ม เช่น 202606235fy5l3i8dHouzVOLz หรือ C20260624617516105918)\n"
        "  ⚠️ สลิป 'จ่ายบิล/ชำระร้านค้า' (Krungthai/KBank): 'ห้าม' ใช้ 'รหัสธุรกรรม/รหัสร้านค้า' (เช่น EMPKB000001944107005, KB000001944107) เป็น ref_number เด็ดขาด — "
        "พวกนี้เป็นรหัสของ 'ร้าน' ซ้ำทุกใบทุกคน (ลูกค้าทุกคนได้เลขนี้เหมือนกัน) → ให้ใช้ 'รหัสอ้างอิง' ที่ไม่ซ้ำแทน; ถ้าหาเลขที่ไม่ซ้ำไม่เจอให้ null\n"
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
        "  - ⚠️ 'แถบแจ้งเตือน/ป๊อปอัป' ของแอป (เช่น notification 'เงินออก -xxx', ปุ่ม Mute/Reply, banner เด้งทับด้านบนจอ) "
        "เป็นการแจ้งเตือนสดของมือถือ ไม่ใช่การตัดต่อ — ห้ามใช้เป็นเหตุผล fraud; "
        "การที่ 'จำนวนเงินซ้ำ 2 ที่' (บนแถบแจ้งเตือน + ในตัวสลิป) เป็นเรื่องปกติ ไม่ใช่การแก้\n"
        "  - จุด/เม็ด noise สี (ม่วง/ชมพู/เขียว) กระจายทั่วรูป = artifact จากการถ่ายจอ/บีบอัดไฟล์ ไม่ใช่ร่องรอยแก้ตัวเลข\n"
        "ถ้าไม่แน่ใจ หรือร่องรอยไม่ได้อยู่ที่ 'ตัวเลขจำนวนเงิน' โดยตรง → ให้ fraud_score < 40 เสมอ\n"
        "fraud_reasons: ระบุเฉพาะร่องรอยที่ 'ตัวเลขจำนวนเงิน' ถูกแก้ พร้อมตำแหน่ง (ห้ามพูดเรื่องโลโก้/แบรนด์) ถ้าไม่เจอให้เป็น []"
    )
    img_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    # รอบแรกใช้ flash; รอบสอง (retry) ใช้โมเดลเก่งขึ้น (pro) เพื่อกู้สลิปที่ flash อ่านไม่ออก
    response = _gemini_generate([prompt, img_part], model=(GEMINI_MODEL_RETRY if retry else None), json_mode=True)
    # parse แบบทนทาน: ถ้าโมเดลตอบไม่เป็น JSON ให้ถือว่า 'ไม่ใช่สลิป' (เงียบ) แทนที่จะ error → เตือนผิด
    return _parse_gemini_json(response, {"is_slip": False})


def extract_payable_doc(image_bytes: bytes, retry: bool = False) -> dict:
    """อ่านรูปในกลุ่มบัญชีเจ้าหนี้ → แยกว่าเป็น 'บิลซื้อ' หรือ 'สลิปโอนจ่าย' พร้อมยอดเงิน
    คืน JSON: doc_type=bill|payment|other, amount, ref_number(สำหรับสลิป), sender
    retry=True → อ่านซ้ำด้วยโมเดลเก่งขึ้น (pro) เมื่อรอบแรกอ่านไม่ออก"""
    prompt = (
        f"รูปนี้อยู่ในกลุ่มซื้อ-ขายระหว่างร้านอาหารกับร้านขายเครื่องดื่ม/สุรา ({PAYABLE_VENDOR}) "
        "ช่วยดูว่าเป็นเอกสารแบบไหน แล้วตอบ JSON เท่านั้น ไม่มีข้อความอื่น:\n\n"
        '{"doc_type":"bill","amount":0.00,"ref_number":null,"sender":null,"doc_date":null,"pay_for_date":null}\n\n'
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
        "pay_for_date (เฉพาะ payment): ถ้าบนสลิปมี 'โน้ตเขียน/พิมพ์กำกับ' ว่าจ่ายค่าของวันไหน "
        "เช่น '(6/6)', 'ค่าของ 6/6', 'จ่าย 8/6' → ดึงวันนั้นเป็น ค.ศ. YYYY-MM-DD (พ.ศ. ลบ 543). "
        "เป็นคนละอย่างกับ doc_date (วันที่โอน) — เอาเฉพาะ 'วันที่ที่โน้ตบอกว่าจ่ายให้บิลไหน'; ถ้าไม่มีโน้ตใส่ null\n"
        "ถ้า doc_type='other' ให้ amount=0"
    )
    img_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    response = _gemini_generate([prompt, img_part], model=(GEMINI_MODEL_RETRY if retry else None), json_mode=True)
    # parse แบบทนทาน: ถ้าโมเดลตอบไม่เป็น JSON ให้ถือว่า 'อื่นๆ' (ไม่ใช่บิล/สลิป) แทนที่จะ error
    return _parse_gemini_json(response, {"doc_type": "other", "amount": 0})


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
    """หาความซ้ำ/ความผิดปกติเทียบกับใบก่อนหน้าในกรุ๊ป — คืน (type, prev_amount, prev)
    prev = dict ข้อมูลสลิปใบก่อนที่ตรง (sender/slip_datetime/ref_number/recorded_at) ไว้โชว์ว่าซ้ำกับใบไหน
    - ref_mismatch: เลขอ้างอิงเดียวกันแต่ยอดเงินไม่ตรง = ถูกตัดต่อ! (ฟันธง)
    - ref: เลขอ้างอิงตรง + ยอดตรง = สลิปซ้ำจริง
    - amount_time: ยอดเงิน + วันเวลาบนสลิปตรงกัน 'และผู้โอนคนเดียวกัน' (กันกรณี ref อ่านเพี้ยน)
      *ถ้าผู้โอนคนละคน = ลูกค้าคนละโต๊ะบังเอิญจ่ายยอดเท่ากัน ไม่ใช่สลิปซ้ำ → ไม่เตือน (กัน false positive)"""
    ref    = info.get("ref_number")
    amount = float(info.get("amount") or 0)
    dt     = info.get("datetime")
    cur_s  = _norm_match_text((info.get("sender") or "").strip())
    with _db() as conn:
        if ref:
            row = conn.execute(
                "SELECT amount, sender, ref_number, slip_datetime, recorded_at FROM slips WHERE group_id=? AND ref_number=? ORDER BY id LIMIT 1",
                (group_id, ref)).fetchone()
            if row:
                prev = float(row["amount"] or 0)
                # ref เดียวกันแต่ 'คนละผู้โอน' = มักเป็นรหัสร้านค้า/รหัสธุรกรรมที่ซ้ำทุกใบ
                # (เช่น จ่ายบิล Krungthai/KBank รหัส EMPKB...ของร้าน) ลูกค้าหลายคนได้รหัสเดียวกัน
                # → ไม่ใช่สลิปซ้ำ/ตัดต่อ ข้ามไป (กัน false positive ปฏิเสธเงินลูกค้าจริง)
                prev_s = _norm_match_text((row["sender"] or "").strip())
                diff_sender = cur_s and prev_s and cur_s != prev_s
                if not diff_sender:
                    if amount and prev and abs(prev - amount) > 0.01:
                        return ("ref_mismatch", prev, dict(row))
                    return ("ref", None, dict(row))
        if amount and dt:
            row = conn.execute(
                "SELECT amount, sender, ref_number, slip_datetime, recorded_at FROM slips "
                "WHERE group_id=? AND amount=? AND slip_datetime=? ORDER BY id DESC LIMIT 1",
                (group_id, amount, dt)).fetchone()
            if row:
                prev_s = _norm_match_text((row["sender"] or "").strip())
                # ยอด+เวลาตรง แต่ 'คนละผู้โอน' → บังเอิญคนละบิลจ่ายยอดเท่ากัน ไม่ใช่สลิปซ้ำ
                # (อ่านชื่อไม่ได้สักฝั่ง = เตือนไว้ก่อนแบบ conservative)
                if cur_s and prev_s and cur_s != prev_s:
                    return (None, None, None)
                return ("amount_time", None, dict(row))
    return (None, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# Verdict Builder
# ══════════════════════════════════════════════════════════════════════════════

def _norm_match_text(s: str) -> str:
    """normalize ข้อความ/เลขบัญชี ก่อนเทียบ PAYEE — กัน OCR เพี้ยนทำให้เทียบไม่ตรง:
    lower + แปลง ใ→ไ (OCR สับสนไม้ม้วน/ไม้มลายบ่อย) + ตัดช่องว่าง/ขีด/จุด/วงเล็บ/สแลช
    เช่น 'ไส้ ย่างซอย 4' / 'ใส้ย่าง' / '460-5949' → เทียบ substring กับ keyword ได้ทนขึ้น"""
    return re.sub(r"[\s\-\.\(\)/]", "", (s or "").lower().replace("ใ", "ไ"))


def _payee_hay(info: dict) -> str:
    return _norm_match_text(f"{info.get('receiver') or ''} {info.get('receiver_account') or ''} {info.get('bank') or ''}")


def _payee_matches(info: dict) -> bool:
    """ปลายทางบนสลิปตรงกับบัญชีร้านไหม (เทียบกับ PAYEE_KEYWORDS) — ถ้าไม่ตั้งค่าไว้ถือว่าผ่าน"""
    if not PAYEE_KEYWORDS:
        return True
    hay = _payee_hay(info)
    return any(_norm_match_text(k) in hay for k in PAYEE_KEYWORDS)


def _slip_account_label(info: dict):
    """จับว่าสลิปโอนเข้าบัญชีไหน (ตาม PAYEE_ACCOUNTS) — คืน label หรือ None ถ้าไม่ตรง/ไม่ได้ตั้งค่า"""
    if not PAYEE_ACCOUNTS:
        return None
    hay = _payee_hay(info)
    for label, kws in PAYEE_ACCOUNTS:
        if any(_norm_match_text(k) in hay for k in kws):
            return label
    return None


def _is_income(info: dict) -> bool:
    """สลิปนี้เป็น 'รายรับ' (โอนเข้าบัญชีร้าน) ไหม — ตรง PAYEE_KEYWORDS หรือ PAYEE_ACCOUNTS อย่างใดอย่างหนึ่ง"""
    hay = _payee_hay(info)
    if any(_norm_match_text(k) in hay for k in PAYEE_KEYWORDS):
        return True
    return _slip_account_label(info) is not None


def build_verdict(info: dict, promptpay: dict, dup_type=None, prev_amount=None, prev=None) -> dict:
    issues = []
    fraud_score = info.get("fraud_score", 0)

    # AI 'สงสัยว่าปลอม' = เตือนให้ตรวจเท่านั้น (⚠️ WARN) ไม่ฟันธง FAIL —
    # เพราะตรวจจากรูปถ่ายจอไม่แม่น ออกหลอกบ่อย (false positive) ทำให้เสี่ยงปฏิเสธเงินลูกค้าจริง
    # ฟันธง 'ปลอม/ตัดต่อ' (🔴 FAIL) เฉพาะสัญญาณที่เชื่อถือได้จริง = เลขอ้างอิงซ้ำแต่ยอดต่าง (ref_mismatch) ด้านล่าง
    if fraud_score >= 40:
        issues.append(f"🟡 AI พบจุดน่าสงสัย (คะแนน {fraud_score}/100) — โปรดตรวจก่อนรับ (ไม่ฟันธงว่าปลอม)")
        for r in info.get("fraud_reasons", []):
            issues.append(f"   • {r}")

    if promptpay.get("verified") is False:
        issues.append(f"🔴 PromptPay API: ไม่ผ่านการตรวจสอบ ({promptpay.get('error','')})")
    elif promptpay.get("verified") is True:
        api_amt  = promptpay.get("api_amount")
        slip_amt = info.get("amount")
        if api_amt and slip_amt and abs(float(api_amt) - float(slip_amt)) > 0.01:
            issues.append(f"🔴 ยอดเงินไม่ตรง: สลิปแสดง {slip_amt} แต่ API พบ {api_amt} บาท")

    # บรรทัด "ซ้ำกับใบไหน" — โชว์ผู้โอน/เวลา/เลขอ้างอิงของสลิปใบก่อนที่ตรง ให้ตรวจสอบง่าย
    def _dup_ref_line(p):
        if not p:
            return ""
        ptime = p.get("slip_datetime") or p.get("recorded_at") or "-"
        return f"\n   ↳ ซ้ำกับใบก่อน: {p.get('sender') or '-'} · เวลา {ptime} · อ้างอิง {p.get('ref_number') or '-'}"

    if dup_type == "ref_mismatch":
        cur_amt  = f"{float(info.get('amount') or 0):,.2f}"
        prev_amt = f"{float(prev_amount or 0):,.2f}"
        issues.append(f"🔴 เลขอ้างอิงเดียวกับสลิปใบก่อน แต่ยอดเงินไม่ตรง! "
                      f"(ใบก่อน {prev_amt} / ใบนี้ {cur_amt} บาท) → สลิปถูกตัดต่อ!{_dup_ref_line(prev)}")
    elif dup_type == "ref":
        issues.append(f"🔴 เลขอ้างอิง {info.get('ref_number')} เคยถูกส่งมาแล้ว! (สลิปซ้ำ){_dup_ref_line(prev)}")
    elif dup_type == "amount_time":
        issues.append(f"🔴 ยอด {info.get('amount')} บาท + วันเวลา + ผู้โอนเดียวกับสลิปใบก่อนหน้า → น่าจะเป็นสลิปซ้ำ (โปรดตรวจสอบ){_dup_ref_line(prev)}")

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

def save_slip(group_id: str, info: dict, verdict_status: str, message_id: str = None, slip_date: str = None):
    today       = slip_date or datetime.now(TZ).date().isoformat()
    recorded_at = datetime.now(TZ).strftime("%H:%M:%S")
    with _db() as conn:
        rid = conn.insert_returning_id(
            "INSERT INTO slips (group_id, slip_date, sender, amount, bank, ref_number, slip_datetime, verdict, recorded_at, account_label, message_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, today, info.get("sender"), float(info.get("amount") or 0),
             info.get("bank"), info.get("ref_number"), info.get("datetime"), verdict_status, recorded_at,
             _slip_account_label(info), message_id)
        )
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()
    return rid


def _slip_message_seen(group_id: str, message_id: str) -> bool:
    """รูปนี้ (message_id เดียวกัน) เคยถูกบันทึกเป็นสลิปในกรุ๊ปนี้แล้วหรือยัง — กัน LINE ส่ง webhook ซ้ำ/รีสตาร์ทแล้วนับซ้ำ"""
    if not message_id:
        return False
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM slips WHERE group_id=? AND message_id=? LIMIT 1",
            (group_id, message_id)).fetchone() is not None


def record_image_miss(group_id: str, reason: str, detail: str = None):
    """บันทึกรูปที่รับเข้ามาแต่ไม่ได้กลายเป็นสลิป (reason='notslip' อ่านไม่ออก/ไม่ใช่สลิป, 'error' พัง, 'notincome' ข้ามไม่ใช่รายรับ)
    detail = ยอด/ปลายทาง (ไว้ดูใบที่ถูกข้าม) — ห้าม throw ซ้อน (best-effort)"""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail) VALUES (?,?,?,?,?)",
                (group_id, datetime.now(TZ).date().isoformat(), reason,
                 datetime.now(TZ).strftime("%H:%M:%S"), detail)
            )
            conn.commit()
    except Exception as e:
        print(f"[miss] record failed group={group_id}: {e}", flush=True)


def build_skipped_list(group_id: str, report_date: str = None) -> str:
    """รายการใบที่ถูกข้าม 'ไม่ใช่รายรับ' (โอนเข้าบัญชีที่ไม่ตรง PAYEE) พร้อมยอด+ปลายทาง
    ไว้เช็กว่าใบที่ข้ามเป็นเงินร้านจริงไหม (ถ้าใช่ = ต้องเพิ่มบัญชีใน PAYEE_ACCOUNTS)"""
    d = report_date or datetime.now(TZ).date().isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT recorded_at, detail FROM image_misses "
            "WHERE group_id=? AND stat_date=? AND reason='notincome' ORDER BY id", (group_id, d)).fetchall()
    if not rows:
        return f"📭 วันที่ {d} ไม่มีใบที่ถูกข้าม (ไม่ใช่รายรับ)"
    lines = [f"⏭️ ใบที่ถูกข้าม — ไม่ตรงบัญชีร้าน ({d})", f"ทั้งหมด {len(rows)} ใบ", "━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(f"{r['recorded_at'] or '-'}  {r['detail'] or '(ไม่มีรายละเอียด)'}")
    lines += ["━━━━━━━━━━━━━", "ℹ️ ใบไหนเป็นบัญชีร้านจริง → แจ้งเพิ่มใน PAYEE_ACCOUNTS จะได้นับเป็นรายรับ"]
    return "\n".join(lines)


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
    lines += _dining_report_lines(group_id, report_date)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# ระบบกระทบบิล-สลิป (โอนขาด/เกิน) — เฉพาะ DINING_GROUPS
#   บิลร้าน (ใบแจ้งรายการ) เข้าคิว → สลิปโอนเข้ามาจับคู่ (รูปเดียว=เทียบตรง / แยกรูป=FIFO ตามเวลา)
#   โอนเกิน → สะสมเข้า 'ยอดทริป' (เงียบ) ; โอนขาด > DINING_SHORT_BAHT → เตือนในกลุ่ม
#   รีเซ็ตยอดเมื่อพนักงานพิมพ์ 'เก็บครบแล้ว' (ยืนยันเก็บเงินครบ)
# ══════════════════════════════════════════════════════════════════════════════

_dining_lock = threading.Lock()   # กัน 2 สลิป (หลาย worker) จับคู่บิลค้างใบเดียวกันพร้อมกัน

def _dining_enabled(group_id: str) -> bool:
    return group_id in DINING_GROUPS

def _dining_balance(group_id: str) -> float:
    try:
        return float(_get_meta(f"dining_balance:{group_id}") or 0)
    except (TypeError, ValueError):
        return 0.0

def _dining_set_balance(group_id: str, value: float):
    _set_meta(f"dining_balance:{group_id}", f"{value:.2f}")

def _minutes_since(hhmmss: str):
    """กี่นาทีผ่านมาแล้วจากเวลา HH:MM:SS (วันนี้) — ใช้เช็คบิลค้างหมดอายุ; อ่านไม่ได้คืน None"""
    try:
        now = datetime.now(TZ)
        t   = datetime.strptime(hhmmss, "%H:%M:%S").time()
        bdt = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        return (now - bdt).total_seconds() / 60.0
    except Exception:
        return None

def _dining_bill_seen(group_id: str, message_id: str) -> bool:
    """รูปบิลนี้ (message_id เดียวกัน) เคยเก็บแล้วหรือยัง — กัน LINE ส่ง webhook ซ้ำ/รีสตาร์ทแล้วนับซ้ำ"""
    if not message_id:
        return False
    with _db() as conn:
        return conn.execute("SELECT 1 FROM dining_bills WHERE group_id=? AND message_id=? LIMIT 1",
                            (group_id, message_id)).fetchone() is not None

def _dining_save_bill(group_id: str, amount: float, table_no, message_id, bill_date: str = None):
    d = bill_date or datetime.now(TZ).date().isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO dining_bills (group_id, bill_date, table_no, amount, recorded_at, message_id, matched) "
            "VALUES (?,?,?,?,?,?,0)",
            (group_id, d, table_no, float(amount),
             datetime.now(TZ).strftime("%H:%M:%S"), message_id))
        conn.commit()

def _dining_after_slip(group_id: str, info: dict, slip_date: str = None):
    """หลังบันทึกสลิปรายรับ → เทียบกับบิลร้าน
    คืน dict {note, pair_id, table_no, short} ถ้าโอนขาด (ไว้เด้งปุ่มเคลียร์โต๊ะนั้น) ; ไม่งั้น None
    โอนเกิน/พอดี = สะสมเข้ายอดทริป เงียบ. best-effort — ไม่ throw ออกไปกระทบ flow หลัก"""
    if not _dining_enabled(group_id):
        return None
    try:
        slip_amt = float(info.get("amount") or 0)
        if slip_amt <= 0:
            return None
        today = slip_date or datetime.now(TZ).date().isoformat()
        bill_amt, table_no, source = None, None, None
        if float(info.get("bill_total") or 0) > 0:
            # บิลอยู่ในรูปเดียวกับสลิป → เทียบตรง (แม่นสุด ไม่ต้องเดาคู่)
            bill_amt = float(info["bill_total"])
            table_no = info.get("bill_table")
            source   = "รูปเดียว"
        else:
            # จับคู่บิลค้าง — ล็อกกัน 2 สลิป (หลาย worker) จับใบเดียวกันพร้อมกัน
            with _dining_lock, _db() as conn:
                # หมดอายุบิลค้างที่เกินกรอบเวลา (จ่ายสด/ไม่มีสลิป) ทิ้งก่อน กันไปจับคู่ผิด
                for r in conn.execute(
                        "SELECT id, recorded_at FROM dining_bills "
                        "WHERE group_id=? AND bill_date=? AND matched=0", (group_id, today)).fetchall():
                    g = _minutes_since(r["recorded_at"])
                    if g is not None and g > DINING_MATCH_MIN:
                        conn.execute("UPDATE dining_bills SET matched=2 WHERE id=?", (r["id"],))
                # 1) จับ 'ยอดตรงเป๊ะ' ก่อน (เคสปกติ: สแกน QR บิลจ่ายเต็มจำนวน)
                #    → กันจับคู่สลับลำดับเมื่อสลิปประมวลผลเสร็จไม่เรียงกัน (หลาย worker / Gemini เร็ว-ช้าไม่เท่า)
                row = conn.execute(
                    "SELECT id, table_no, amount FROM dining_bills "
                    "WHERE group_id=? AND bill_date=? AND matched=0 AND ABS(amount-?)<0.01 ORDER BY id LIMIT 1",
                    (group_id, today, slip_amt)).fetchone()
                if not row:
                    # 2) ไม่มียอดตรง = ลูกค้าโอนเกิน/ขาดจริง → จับบิลค้างเก่าสุด (FIFO)
                    row = conn.execute(
                        "SELECT id, table_no, amount FROM dining_bills "
                        "WHERE group_id=? AND bill_date=? AND matched=0 ORDER BY id LIMIT 1",
                        (group_id, today)).fetchone()
                if row:
                    bill_amt = float(row["amount"])
                    table_no = row["table_no"]
                    source   = "ยอดตรง" if abs(bill_amt - slip_amt) < 0.01 else "จับคู่เวลา"
                    conn.execute("UPDATE dining_bills SET matched=1 WHERE id=?", (row["id"],))
                conn.commit()
        if bill_amt is None:
            return None   # ไม่มีบิลให้เทียบ (เช่น หลังเที่ยงคืน/จ่ายสด) → บันทึกสลิปเฉยๆ
        diff = round(slip_amt - bill_amt, 2)
        with _db() as conn:
            pair_id = conn.insert_returning_id(
                "INSERT INTO dining_pairs (group_id, pair_date, table_no, bill_amount, slip_amount, diff, recorded_at, source, collected, slip_id) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (group_id, today, table_no, bill_amt, slip_amt, diff,
                 datetime.now(TZ).strftime("%H:%M:%S"), source, info.get("_slip_id")))
            conn.commit()
        tbl = f"โต๊ะ {table_no}" if table_no else "บิลนี้"
        if diff <= -DINING_SHORT_BAHT:
            # โอนขาด → หักออกจากยอดสะสมทันที + ปุ่มเก็บส่วนต่าง
            bal = round(_dining_balance(group_id) + diff, 2)
            _dining_set_balance(group_id, bal)
            note = (f"⚠️ โอนขาด! {tbl}\n─────────────────\n"
                    f"บิล {bill_amt:,.2f} / โอน {slip_amt:,.2f}\n"
                    f"ขาด {abs(diff):,.2f} บาท\n"
                    f"ยอดสะสมทริป: {bal:+,.2f} บาท\n"
                    f"👇 เก็บส่วนต่างจากลูกค้าแล้ว กดปุ่มเคลียร์โต๊ะนี้")
            return {"kind": "under", "note": note, "pair_id": pair_id, "table_no": table_no, "short": abs(diff)}
        if diff >= DINING_SHORT_BAHT:
            # โอนเกิน → 'ยังไม่' เก็บเข้าทริป รอพนักงานยืนยันว่าเกินจริงหรือบอทอ่านผิด
            note = (f"⚠️ โอนเกิน! {tbl}\n─────────────────\n"
                    f"บิล {bill_amt:,.2f} / โอน {slip_amt:,.2f}\n"
                    f"เกิน {diff:,.2f} บาท\n"
                    f"👇 ลูกค้าโอนเกินจริง หรือ บอทอ่านผิด?")
            return {"kind": "over", "note": note, "pair_id": pair_id, "table_no": table_no, "over": diff}
        return None   # พอดี → เงียบ
    except Exception as e:
        print(f"[dining] match error group={group_id}: {e}", flush=True)
        return None

def _dining_compare_and_push(group_id: str, info: dict):
    """จับคู่บิล-สลิป (เรียกแบบหน่วงเวลา) — ถ้าโอนขาด push เตือน+ปุ่มเคลียร์เข้ากลุ่ม
    หน่วงไว้เพื่อรอบิลที่ส่งไล่ๆ กันถูกอ่านเข้าคิวครบก่อนจับคู่ (กันจับคู่สลับลำดับ). best-effort"""
    try:
        res = _dining_after_slip(group_id, info)
        if not res:
            return
        if res["kind"] == "over":
            card = _dining_over_card(res["pair_id"], res["table_no"], res["over"])
        else:
            card = _dining_clear_card(res["pair_id"], res["table_no"], res["short"])
        _push(group_id, [TextSendMessage(text=res["note"]), card])
    except Exception as e:
        if _is_not_member_error(e):
            _mark_group_left(group_id)
        print(f"[dining] delayed compare/push error group={group_id}: {e}", flush=True)


# คิวหน่วงจับคู่บิล-สลิป — ใช้ thread เดียวเดินตรวจ แทน threading.Timer ต่อสลิป
# (Timer ต่อใบ = สร้าง thread ต่อสลิป ตอนพีคสลิปรัวๆ → thread พุ่ง → memory เกิน/restart)
_dining_q = []                       # [(due_epoch, group_id, info), ...]
_dining_q_lock = threading.Lock()

def _dining_schedule(group_id: str, info: dict):
    """ตั้งคิวจับคู่บิล-สลิปแบบหน่วง DINING_MATCH_DELAY วิ (เก็บในลิสต์ ให้ scheduler เดียวดึงไปทำ)"""
    with _dining_q_lock:
        _dining_q.append((time.time() + DINING_MATCH_DELAY, group_id, info))

def _dining_scheduler_loop():
    """thread เดียวเดินตรวจคิวทุก ~5 วิ — ถึงเวลาแล้วค่อยจับคู่+เตือน (ประหยัด thread/memory กว่า Timer ต่อใบ)"""
    while True:
        try:
            now = time.time()
            with _dining_q_lock:
                due = [x for x in _dining_q if x[0] <= now]
                if due:
                    _dining_q[:] = [x for x in _dining_q if x[0] > now]
            for _due, gid, info in due:
                _dining_compare_and_push(gid, info)
        except Exception as e:
            print(f"[dining-sched] loop error: {e}", flush=True)
        time.sleep(5)


def build_dining_summary(group_id: str, report_date: str = None) -> str:
    d   = report_date or datetime.now(TZ).date().isoformat()
    bal = _dining_balance(group_id)
    with _db() as conn:
        pairs = [dict(r) for r in conn.execute(
            "SELECT * FROM dining_pairs WHERE group_id=? AND pair_date=? ORDER BY id",
            (group_id, d)).fetchall()]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM dining_bills WHERE group_id=? AND matched=0",
            (group_id,)).fetchone()["c"]
    shorts    = [p for p in pairs if p["diff"] <= -DINING_SHORT_BAHT]
    uncleared = [p for p in shorts if not p["collected"]]
    cleared   = [p for p in shorts if p["collected"]]
    lines = [f"🧾 กระทบบิล-สลิป {d}",
             "─────────────────",
             f"จับคู่ได้ {len(pairs)} คู่ | โอนขาด {len(shorts)} คู่ (เคลียร์แล้ว {len(cleared)})",
             f"ยอดสะสมทริป: {bal:+,.2f} บาท",
             f"บิลค้างยังไม่จับคู่: {pending} ใบ"]
    if uncleared:
        lines += ["─────────────────", "⚠️ ยังไม่เคลียร์ (ต้องเก็บส่วนต่าง):"]
        for p in uncleared:
            tbl = f"โต๊ะ {p['table_no']}" if p["table_no"] else "บิล"
            lines.append(f"• {tbl} ({p['recorded_at']}): บิล {p['bill_amount']:,.2f} / โอน {p['slip_amount']:,.2f} → ขาด {abs(p['diff']):,.2f}")
        lines.append("ℹ️ เคลียร์ทีละโต๊ะได้จากปุ่มใต้ข้อความเตือนตอนสลิปเข้า")
    lines += ["─────────────────", "👇 สิ้นวันเก็บครบทุกโต๊ะแล้ว กดปุ่มปิดยอดทริป (หรือพิมพ์ 'เก็บครบแล้ว')"]
    return "\n".join(lines)

def _dining_report_lines(group_id: str, d: str):
    """บรรทัดสรุปบิล-สลิปของวันนั้น (ต่อท้ายรายงานสรุปประจำวัน) — ไม่มีคู่ = []"""
    if not _dining_enabled(group_id):
        return []
    with _db() as conn:
        pairs = [dict(r) for r in conn.execute(
            "SELECT * FROM dining_pairs WHERE group_id=? AND pair_date=? ORDER BY id",
            (group_id, d)).fetchall()]
    if not pairs:
        return []
    shorts    = [p for p in pairs if p["diff"] <= -DINING_SHORT_BAHT]
    uncleared = [p for p in shorts if not p["collected"]]
    net    = sum(p["diff"] for p in pairs)
    out = ["", "─────────────────",
           f"🧾 บิล-สลิป: จับคู่ {len(pairs)} คู่ | สุทธิ {net:+,.2f} บาท",
           f"โอนขาด {len(shorts)} คู่ (ยังไม่เคลียร์ {len(uncleared)})"]
    for p in uncleared:
        tbl = f"โต๊ะ {p['table_no']}" if p["table_no"] else "บิล"
        out.append(f"   ⚠️ {tbl}: บิล {p['bill_amount']:,.2f}/โอน {p['slip_amount']:,.2f} ขาด {abs(p['diff']):,.2f}")
    return out

def _dining_clear_card(pair_id, table_no, short) -> TemplateSendMessage:
    """การ์ดปุ่มเคลียร์ 'โต๊ะนั้น' โดยเฉพาะ — เด้งมากับข้อความเตือนโอนขาด (กดเมื่อเก็บส่วนต่างครบ)"""
    tbl = f"โต๊ะ {table_no}" if table_no else "บิลนี้"
    return TemplateSendMessage(
        alt_text=f"ยืนยันเก็บส่วนต่าง {tbl}",
        template=ButtonsTemplate(
            title=f"เคลียร์ {tbl}",
            text=f"เก็บส่วนต่าง {short:,.2f} บาท จากลูกค้าครบแล้ว?",
            actions=[PostbackAction(label="✅ เก็บครบแล้ว", data=f"dining_clear:{pair_id}")],
        ),
    )


def _dining_clear_pair(event, group_id: str, pair_id: int):
    """เคลียร์โอนขาด 'เฉพาะโต๊ะนั้น' (เก็บส่วนต่างครบ) → ตัดยอดขาดของคู่นี้ออกจากยอดสะสม"""
    with _db() as conn:
        row = conn.execute("SELECT table_no, diff, collected FROM dining_pairs WHERE id=? AND group_id=?",
                           (pair_id, group_id)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่พบรายการนี้แล้ว"))
            return
        if row["collected"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ โต๊ะนี้เคลียร์ไปแล้ว ไม่ต้องกดซ้ำครับ"))
            return
        conn.execute("UPDATE dining_pairs SET collected=1 WHERE id=?", (pair_id,))
        conn.commit()
    diff = float(row["diff"])
    bal  = round(_dining_balance(group_id) - diff, 2)   # diff ติดลบ → ตัดยอดขาดออก = ยอดสะสมเพิ่มขึ้น
    _dining_set_balance(group_id, bal)
    confirmer = get_display_name(event.source)
    tbl = f"โต๊ะ {row['table_no']}" if row["table_no"] else "บิลนี้"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=(f"✅ เคลียร์ {tbl} แล้ว (โดย {confirmer})\n"
              f"เก็บส่วนต่าง +{abs(diff):,.2f} บาท\n"
              f"ยอดสะสมคงเหลือ: {bal:+,.2f} บาท")))


def _dining_over_card(pair_id, table_no, over) -> TemplateSendMessage:
    """การ์ดยืนยัน 'โอนเกิน' — ให้พนักงานเลือก: เกินจริง(เก็บทริป) / บอทอ่านผิด(ใช้ยอดบิล)"""
    tbl = f"โต๊ะ {table_no}" if table_no else "บิลนี้"
    return TemplateSendMessage(
        alt_text=f"ยืนยันโอนเกิน {tbl}",
        template=ButtonsTemplate(
            title=f"โอนเกิน {tbl}",
            text=f"เกิน {over:,.2f} บาท — ลูกค้าโอนเกินจริง หรือบอทอ่านผิด?",
            actions=[
                PostbackAction(label="✅ โอนเกินจริง (เก็บทริป)", data=f"dining_over_keep:{pair_id}"),
                PostbackAction(label="🔧 บอทอ่านผิด (ใช้ยอดบิล)", data=f"dining_over_fix:{pair_id}"),
            ],
        ),
    )


def _dining_over_keep(event, group_id: str, pair_id: int):
    """พนักงานยืนยัน 'ลูกค้าโอนเกินจริง' → เก็บส่วนเกินเข้ายอดสะสมทริป"""
    with _db() as conn:
        row = conn.execute("SELECT table_no, diff, collected FROM dining_pairs WHERE id=? AND group_id=?",
                           (pair_id, group_id)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่พบรายการนี้แล้ว")); return
        if row["collected"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ รายการนี้ยืนยันไปแล้ว ไม่ต้องกดซ้ำครับ")); return
        conn.execute("UPDATE dining_pairs SET collected=1 WHERE id=?", (pair_id,))
        conn.commit()
    diff = float(row["diff"])   # บวก (เกิน)
    bal  = round(_dining_balance(group_id) + diff, 2)
    _dining_set_balance(group_id, bal)
    confirmer = get_display_name(event.source)
    tbl = f"โต๊ะ {row['table_no']}" if row["table_no"] else "บิลนี้"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=(f"✅ {tbl} โอนเกินจริง — เก็บเข้าทริป +{diff:,.2f} บาท (โดย {confirmer})\n"
              f"ยอดสะสมทริป: {bal:+,.2f} บาท")))


def _dining_over_fix(event, group_id: str, pair_id: int):
    """พนักงานยืนยัน 'บอทอ่านผิด' → แก้ยอดสลิปในตาราง slips ให้เท่ายอดบิล (ยอดรับถูกต้อง)"""
    with _db() as conn:
        row = conn.execute(
            "SELECT table_no, diff, bill_amount, slip_amount, slip_id, collected FROM dining_pairs WHERE id=? AND group_id=?",
            (pair_id, group_id)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่พบรายการนี้แล้ว")); return
        if row["collected"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ รายการนี้แก้ไปแล้ว ไม่ต้องกดซ้ำครับ")); return
        conn.execute("UPDATE dining_pairs SET collected=1, diff=0 WHERE id=?", (pair_id,))
        fixed = False
        if row["slip_id"]:
            conn.execute("UPDATE slips SET amount=? WHERE id=? AND group_id=?",
                         (float(row["bill_amount"]), row["slip_id"], group_id))
            fixed = True
        conn.commit()
    confirmer = get_display_name(event.source)
    tbl = f"โต๊ะ {row['table_no']}" if row["table_no"] else "บิลนี้"
    diff = float(row["diff"] or 0)
    if fixed:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=(f"🔧 แก้ยอด {tbl} เป็น {float(row['bill_amount']):,.2f} บาท (ตามบิล) แล้ว (โดย {confirmer})\n"
                  f"ยอดรับลดลง {diff:,.2f} บาท จากที่บอทอ่านเกิน — ไม่เก็บเข้าทริป")))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=(f"🔧 {tbl}: รับทราบว่าบอทอ่านผิด — แต่หาสลิปต้นทางไม่เจอ\n"
                  f"รบกวนแก้ยอดเองด้วย: ลบใบนี้แล้ว 'เพิ่มสลิป {float(row['bill_amount']):,.0f}'")))


def _dining_confirm_card() -> TemplateSendMessage:
    """การ์ดปุ่มให้พนักงานกด 'ยืนยันเก็บเงินครบ' → รีเซ็ตยอดทริป (กดทีเดียว = ยืนยันเลย, กันกดซ้ำด้วย postback)"""
    return TemplateSendMessage(
        alt_text="ยืนยันเก็บเงินครบแล้ว เพื่อปิดยอดทริป",
        template=ButtonsTemplate(
            title="ปิดยอดทริป",
            text="กดเมื่อเก็บเงินลูกค้าครบแล้ว → รีเซ็ตยอดสะสมเป็น 0",
            actions=[PostbackAction(label="✅ เก็บครบแล้ว", data=f"dining_confirm:{uuid.uuid4().hex[:8]}")],
        ),
    )


def _dining_reset(event, group_id: str):
    """พนักงานยืนยัน 'เก็บครบแล้ว' → ล้างบิลค้าง + รีเซ็ตยอดทริปเป็น 0 (เริ่มนับใหม่)"""
    bal = _dining_balance(group_id)
    with _db() as conn:
        pend = conn.execute("SELECT COUNT(*) c FROM dining_bills WHERE group_id=? AND matched=0",
                            (group_id,)).fetchone()["c"]
        conn.execute("UPDATE dining_bills SET matched=1 WHERE group_id=? AND matched=0", (group_id,))
        # ปิดยอดทั้งทริป = เก็บครบทุกโต๊ะแล้ว → เคลียร์รายการโอนขาดที่ยังค้างด้วย (สรุปจะสะอาด)
        n_short = conn.execute("UPDATE dining_pairs SET collected=1 WHERE group_id=? AND collected=0",
                               (group_id,)).rowcount
        conn.commit()
    _dining_set_balance(group_id, 0.0)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=(f"✅ ยืนยันเก็บเงินครบแล้ว — รีเซ็ตยอดทริป\n"
              f"ยอดก่อนรีเซ็ต: {bal:+,.2f} บาท\n"
              f"ล้างบิลค้างจับคู่: {pend} ใบ\n"
              f"เริ่มนับทริปใหม่จาก 0 บาท")))


def _resv_when_label(r: dict) -> str:
    """คำบอกวันเวลาที่คำนวณจาก resv_date เทียบ 'วันนี้' ปัจจุบัน (กันคำว่า 'พรุ่งนี้' ค้างจากวันที่จอง)
    + เวลา HH:MM ที่ดึงจากสตริงเดิม. ถ้าไม่มี resv_date ชัด → คืนสตริงเดิม (resv_datetime/datetime)"""
    raw = r.get("resv_datetime") or r.get("datetime") or ""
    rd  = r.get("resv_date")
    m   = re.search(r"(\d{1,2})[:.](\d{2})", raw)
    tm  = f"{int(m.group(1)):02d}:{m.group(2)}" if m else None
    if not _valid_ymd(rd):
        return raw   # ไม่มีวันชัด → ใช้ของเดิม
    try:
        d = date.fromisoformat(rd)
    except ValueError:
        return raw
    delta = (d - datetime.now(TZ).date()).days
    if   delta == 0: day = "วันนี้"
    elif delta == 1: day = "พรุ่งนี้"
    elif delta == 2: day = "มะรืน"
    else:            day = f"วัน{_TH_WD[d.weekday()]} {d.day} {_TH_MON[d.month - 1]}"
    return f"{day} {tm}" if tm else day


def _resv_line(r: dict) -> str:
    icon = "✅" if r.get("status") == "CONFIRMED" else "⏳"
    cust = r.get("customer") or "?"
    ppl  = f" {r['people']}" if r.get("people") else ""
    _wl  = _resv_when_label(r)
    when = f" | {_wl}" if _wl else ""
    zone = f" | {r['table_no']}" if r.get("table_no") else ""
    by   = f" (โดย {r['confirmed_by']})" if r.get("status") == "CONFIRMED" and r.get("confirmed_by") else ""
    return f"{icon} #{r['id']} {cust}{ppl}{when}{zone}{by}"


def build_resv_summary(notify_group=None, title="📋 สรุปการจองวันนี้", upcoming=False, skip_if_empty=False, match_origin=False, date_mode=None) -> str:
    """สรุปการจอง
    upcoming=False (ดีฟอลต์): เฉพาะจอง 'ของวันนี้' (resv_date = วันนี้) — จองล่วงหน้าจะโผล่เฉพาะวันที่ถึง
    upcoming=True: จองที่กำลังจะถึง (resv_date >= วันนี้) แยกหัวข้อ วันนี/ล่วงหน้า — ใช้กับ digest ล่วงหน้า
    date_mode='advance_arrived': จองล่วงหน้าที่ 'ถึงวันงานวันนี้' (resv_date=วันนี้ + จองมาตั้งแต่วันก่อน) — รายงาน 11:00
       → โผล่เฉพาะวันงานของมัน ไม่โผล่ทุกวันก่อนถึง
    date_mode='sameday': จอง 'ในวัน' (สร้างวันนี้ เพื่อวันนี้/ไม่ระบุวัน) — สรุป 16:00
    notify_group=None → ทุกการจอง / ระบุ group → เฉพาะการจองที่ส่งการ์ดเข้ากลุ่มนั้น
    match_origin=True → นับจองที่ 'เกิดในกลุ่มนี้ (origin) หรือส่งเข้ากลุ่มนี้ (notify)' —
       ใช้กับคำสั่ง 'สรุปจอง' ในกลุ่มต้นทาง เพราะจองล่วงหน้าถูกส่ง notify ไปกลุ่มบาร์ (จะได้เห็นล่วงหน้าด้วย)
    skip_if_empty=True → ถ้าไม่มีจองคืน None (ไว้ให้รายงานอัตโนมัติ 'ไม่ส่ง' วันที่ไม่มีจอง — ประหยัด push)"""
    today  = datetime.now(TZ).date().isoformat()
    if date_mode == "advance_arrived":
        # จองล่วงหน้าที่วันงานคือวันนี้ (จองมาก่อนหน้า) — โผล่เฉพาะวันงาน ไม่โผล่ทุกวันตั้งแต่จอง
        date_cond = "(resv_date = ? AND substr(created_at,1,10) < ?)"
        params = [today, today]
    elif date_mode == "sameday":
        # จองในวัน: สร้างวันนี้ + เป็นของวันนี้ (หรือไม่ระบุวัน=ถือว่าวันนี้) — ไม่รวมจองล่วงหน้าเพื่อวันอื่น
        date_cond = "(substr(created_at,1,10) = ? AND (resv_date = ? OR resv_date IS NULL))"
        params = [today, today]
    elif upcoming:
        # "วันงานจริง" ต้อง >= วันนี้ — ใช้ resv_date ถ้ามี ไม่งั้น fallback วันที่แจ้ง (กันจองที่เลยวันไปแล้วค้างในรายการล่วงหน้า)
        date_cond = "(COALESCE(resv_date, substr(created_at,1,10)) >= ?)"
        params = [today]
    else:
        # วันนี้เท่านั้น: resv_date=วันนี้ (จองล่วงหน้าโผล่เฉพาะวันถึง) + จองที่ไม่มีวันชัดแต่แจ้งวันนี้
        date_cond = "((resv_date = ?) OR (resv_date IS NULL AND substr(created_at,1,10) = ?))"
        params = [today, today]
    q = f"SELECT * FROM reservations WHERE {date_cond}"
    if notify_group:
        if match_origin:
            q += " AND (notify_group_id=? OR origin_group_id=?)"
            params += [notify_group, notify_group]
        else:
            q += " AND notify_group_id=?"
            params.append(notify_group)
    q += " ORDER BY resv_date IS NULL, resv_date, created_at"
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if not rows:
        return None if skip_if_empty else f"{title}\n─────────────────\nไม่มีการจอง"
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


def build_advance_resv_report(skip_if_empty=False) -> str:
    """digest จองล่วงหน้าที่กำลังจะถึง (เข้ากลุ่มบาร์น้ำ) — ใช้ตอนรายงาน 00:30
    skip_if_empty=True → ไม่มีจองล่วงหน้าคืน None (ไม่ส่ง)"""
    return build_resv_summary(BAR_GROUP_ID, "📅 จองล่วงหน้าที่กำลังจะถึง", upcoming=True, skip_if_empty=skip_if_empty)


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
    """ตอบยืนยันบิล/สลิป 'ในกลุ่มที่ส่ง' ด้วย reply (ฟรี ไม่กินโควต้า push)
    กลุ่ม mirror (กลุ่ม 2) จะรับ 'เฉพาะสรุปรายวัน' ไม่เด้งทุกบิล/สลิป — ประหยัดโควต้า
    (dest_group เก็บไว้เพื่อความเข้ากันได้ของ signature เดิม ไม่ได้ใช้ push แล้ว)"""
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
    except Exception as e:
        # reply token หมดอายุ/ใช้แล้ว (เช่นประมวลผลนาน) → fallback push เข้ากลุ่มที่ส่งเอง กันยืนยันหาย
        print(f"[payable] reply ไม่สำเร็จ src={source_group}: {e} — ลอง push สำรอง", flush=True)
        try:
            _push(source_group, TextSendMessage(text=text))
        except Exception as e2:
            print(f"[payable] push สำรองไม่สำเร็จ src={source_group}: {e2}", flush=True)


# marker note สำหรับ 'ยอดค้างยกมา' ที่กระจายเป็นบิลรายวัน (จากบล็อก 'ค้าง') — แยกจากบิลซื้อปกติในรายงาน
_PAYABLE_CARRY_NOTE = "ยอดค้างยกมา"


def _payable_opening(group_id: str) -> float:
    """ยอดค้างยกมา (ตั้งครั้งเดียวตอนเริ่ม) ของกลุ่มนี้"""
    v = _get_meta(f"payable_opening:{group_id}")
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0

def _payable_sum(table: str, group_id: str, before_date=None, on_date=None, net_allocated=False, net_paid=False) -> float:
    """รวมยอดบิล/เงินจ่ายของกลุ่ม — เลือกกรองทั้งหมด / ก่อนวันที่ / เฉพาะวันที่
    net_allocated=True (payable_payments): รวมเฉพาะ 'ส่วนที่ยังไม่ถูกตัดเข้าบิล' (amount − allocated)
    net_paid=True (payable_bills): รวมเฉพาะ 'ส่วนที่ยังค้างจริง' (amount − paid)
    — กันนับซ้ำ เพราะส่วน allocated/paid คือเงินก้อนเดียวกัน (สลิปไปตัดบรรทัดบิล)"""
    if net_allocated:
        col = "amount - COALESCE(allocated,0)"
    elif net_paid:
        col = "amount - COALESCE(paid,0)"
    else:
        col = "amount"
    q = f"SELECT COALESCE(SUM({col}),0) s FROM {table} WHERE group_id=?"
    params = [group_id]
    if before_date is not None:
        q += " AND doc_date < ?"; params.append(before_date)
    if on_date is not None:
        q += " AND doc_date = ?"; params.append(on_date)
    with _db() as conn:
        return float(conn.execute(q, tuple(params)).fetchone()["s"] or 0)

def _payable_outstanding(group_id: str) -> float:
    """ยอดค้างจ่ายสะสมปัจจุบัน = ยกมา + บิลที่ยังค้าง(amount−paid) − จ่ายที่ยังไม่ถูกตัดเข้าบิล(amount−allocated)
    [Σpaid = Σallocated เป็นเงินก้อนเดียวกัน → สูตรนี้ = ยกมา + Σบิล − Σจ่าย พอดี ไม่นับซ้ำ]"""
    return (_payable_opening(group_id)
            + _payable_sum("payable_bills", group_id, net_paid=True)
            - _payable_sum("payable_payments", group_id, net_allocated=True))

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
        # บิล 'จริง' (ไม่ใช่ค้างยกมา) เข้ามา → ถ้ามี 'ยกมา' ยอด+วันเดียวกันที่ยังไม่ถูกจ่ายค้างอยู่
        # ให้ลบทิ้ง 1 ใบ กันนับเบิ้ล (ยกมาเป็นตัวแทนชั่วคราว พอบิลจริงมาก็แทนที่)
        if note != _PAYABLE_CARRY_NOTE:
            dup = conn.execute(
                "SELECT id FROM payable_bills WHERE group_id=? AND doc_date=? AND note=? "
                "AND ABS(amount-?)<0.01 AND COALESCE(paid,0)<0.01 ORDER BY id LIMIT 1",
                (group_id, doc_date, _PAYABLE_CARRY_NOTE, float(amount))).fetchone()
            if dup:
                conn.execute("DELETE FROM payable_bills WHERE id=?", (dup["id"],))
                print(f"[payable] ลบ 'ยกมา' ซ้ำ {amount:,.2f} วันที่ {doc_date} (มีบิลจริงแล้ว) group={group_id}", flush=True)
        conn.commit()
    return rid

def save_payable_payment(group_id: str, amount: float, sender=None, ref_number=None, slip_dt=None, doc_date: str = None, allocated: float = 0, settle_note: str = None) -> int:
    doc_date = doc_date or datetime.now(TZ).date().isoformat()
    now   = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        rid = conn.insert_returning_id(
            "INSERT INTO payable_payments (group_id, doc_date, amount, sender, ref_number, slip_datetime, recorded_at, allocated, settle_note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group_id, doc_date, float(amount), sender, ref_number, slip_dt, now, float(allocated or 0), settle_note))
        conn.execute("INSERT INTO groups (group_id) VALUES (?) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()
    return rid


def _payable_settle(group_id: str, pay_for_date: str, amount: float):
    """ตัดยอดจ่ายเข้ากับ 'บรรทัดบิล/ค้าง' โดยตรง (ดูจากวันที่บนสลิป → ถ้าไม่มี ดูยอดเงินที่ตรง)
    จ่ายครบ = ลบบรรทัดนั้น, จ่ายบางส่วน = ลดยอดบรรทัดเหลือเท่าที่ค้าง (เรียงค้างยกมาก่อน แล้ว FIFO)
    คืน (allocated_total, [ข้อความสรุปบรรทัดที่ตัด], settle_note) — ส่วนที่ตัดได้จะถูกหักจาก amount ที่ลดยอดรวม กันนับซ้ำ
    settle_note = วันที่ของบรรทัดที่ถูกตัด (แบบสั้น คั่นด้วย ,) ไว้โชว์ในรายงานว่า 'จ่ายตัดของวันไหน'"""
    remaining = float(amount)
    allocated_total = 0.0
    settled = []
    settle_dates = []

    def fdate(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%y")
        except (ValueError, TypeError):
            return d or "-"

    def fdate_short(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m")
        except (ValueError, TypeError):
            return d or "-"

    with _db() as conn:
        # เลือกบรรทัดที่ 'ยังค้างจริง' (amount − paid > 0); ค้างยกมาก่อน
        if pay_for_date:   # มีวันที่บนสลิป → ตัดบิล/ค้างของวันนั้น
            # เลือก 'บิลที่ยอดค้างตรงกับยอดจ่ายเป๊ะ' ก่อน (เช่น จ่าย 6,835 → ตัดบิล 6,835 ให้ครบ)
            # แล้วค่อยค้างยกมาก่อน → FIFO (กันเอาไปตัดบิลก้อนใหญ่บางส่วนทั้งที่มีบิลตรงยอด)
            rows = conn.execute(
                "SELECT id, doc_date, amount, COALESCE(paid,0) paid FROM payable_bills "
                "WHERE group_id=? AND doc_date=? AND (amount - COALESCE(paid,0))>0.01 "
                "ORDER BY CASE WHEN ABS((amount - COALESCE(paid,0)) - ?)<0.01 THEN 0 ELSE 1 END, "
                "CASE WHEN note=? THEN 0 ELSE 1 END, id",
                (group_id, pay_for_date, float(amount), _PAYABLE_CARRY_NOTE)).fetchall()
        else:              # ไม่มีวันที่ → จับยอดค้างคงเหลือที่ตรงกับบรรทัดเดียว
            rows = conn.execute(
                "SELECT id, doc_date, amount, COALESCE(paid,0) paid FROM payable_bills "
                "WHERE group_id=? AND ABS((amount - COALESCE(paid,0)) - ?)<0.01 AND (amount - COALESCE(paid,0))>0.01 "
                "ORDER BY CASE WHEN note=? THEN 0 ELSE 1 END, id LIMIT 1", (group_id, float(amount), _PAYABLE_CARRY_NOTE)).fetchall()
        for r in rows:
            if remaining <= 0.01:
                break
            line_remaining = float(r["amount"] or 0) - float(r["paid"] or 0)
            take = min(remaining, line_remaining)
            # ไม่ลบบรรทัด — แค่บวก 'paid' (บรรทัดยังอยู่ในรายงานรอบนี้ โชว์ว่าจ่ายแล้ว; ลบตอนสรุปรอบถัดไป)
            conn.execute("UPDATE payable_bills SET paid=COALESCE(paid,0)+? WHERE id=?", (take, r["id"]))
            remaining -= take
            allocated_total += take
            full = (line_remaining - take) <= 0.01
            settled.append(f"{fdate(r['doc_date'])} −{take:,.2f}" + (" (ครบ)" if full else f" (เหลือ {line_remaining - take:,.2f})"))
            settle_dates.append(fdate_short(r["doc_date"]))
        conn.commit()
    return allocated_total, settled, (",".join(settle_dates) or None)

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
    bills_before = _payable_sum("payable_bills", group_id, before_date=d, net_paid=True)
    pays_before  = _payable_sum("payable_payments", group_id, before_date=d, net_allocated=True)
    carry_in     = opening + bills_before - pays_before
    bills_today  = _payable_sum("payable_bills", group_id, on_date=d, net_paid=True)
    pays_today   = _payable_sum("payable_payments", group_id, on_date=d, net_allocated=True)
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
    """สรุปหนี้แบบ 'รายวัน' — โชว์ทุกบิล/สลิปเป็นบรรทัดของตัวเอง (ไม่รวมวันเดียวกัน); ไม่โชว์ยอดค้างรายบรรทัด
    with_total=True → ต่อท้ายด้วย 'ยอดค้างสะสมรวม' บรรทัดเดียว (กลุ่ม 2 mirror)
    with_total=False → ไม่มียอดรวม (กลุ่ม 1 primary)"""
    with _db() as conn:
        bill_rows = conn.execute(
            "SELECT doc_date, amount, COALESCE(paid,0) paid, note FROM payable_bills "
            "WHERE group_id=? ORDER BY doc_date, id", (acct,)).fetchall()
        pay_rows = conn.execute(
            "SELECT doc_date, amount, COALESCE(allocated,0) allocated FROM payable_payments "
            "WHERE group_id=? ORDER BY doc_date, id", (acct,)).fetchall()
    opening = _payable_opening(acct)
    bar = "━━━━━━━━━━━━━"

    def fdate(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%y")
        except (ValueError, TypeError):
            return d or "-"

    def paid_tag(gross, paid):   # ป้ายต่อท้ายบรรทัดบิล/ค้าง บอกว่าจ่ายไปเท่าไหร่
        if paid >= gross - 0.01:
            return "  ✅ จ่ายครบ"
        if paid > 0.01:
            return f"  💸 จ่าย {paid:,.2f} (เหลือ {gross - paid:,.2f})"
        return ""

    # รวมเป็น 'รายการ' เรียงตามวัน แต่คงแต่ละบิล/สลิปเป็นบรรทัดของตัวเอง (วันเดียวกันก็แยกบรรทัด)
    events = []   # (doc_date, order, idx, kind, amount, paid)
    for i, r in enumerate(bill_rows):
        is_carry = (r["note"] == _PAYABLE_CARRY_NOTE)
        events.append((r["doc_date"], 0 if is_carry else 1, i,
                       "carry" if is_carry else "bill", float(r["amount"] or 0), float(r["paid"] or 0)))
    for i, r in enumerate(pay_rows):
        net = float(r["amount"] or 0) - float(r["allocated"] or 0)   # ส่วนที่ยังไม่ถูกตัดเข้าบิล
        if net > 0.01:
            events.append((r["doc_date"], 2, i, "pay", net, 0.0))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    if not events and not opening:
        return f"📋 สรุปหนี้ {PAYABLE_VENDOR} — รายวัน\n{bar}\nยังไม่มีรายการ"

    has_carry = any(e[3] == "carry" for e in events)
    lines = [f"📋 สรุปหนี้ {PAYABLE_VENDOR} — รายวัน", bar]
    if opening:   # โชว์ยอดยกมา 'ก้อนเดียว' เฉพาะกรณีตั้งด้วยคำสั่ง 'ตั้งยอดยกมา' (บล็อกค้างจะกระจายเป็นรายวันแทน)
        lines += [f"📌 ยอดยกมา   {opening:,.2f}", bar]
    running = opening
    sep_added = False   # คั่นบล็อก 'ยอดค้างยกมา' ออกจากบิล/จ่ายปกติ 1 เส้น
    for d, _o, _i, kind, amt, paid in events:
        if kind == "carry":
            part = f"ยกมา {amt:,.2f}" + paid_tag(amt, paid); running += amt - paid
        elif kind == "bill":
            part = f"📥+{amt:,.2f}" + paid_tag(amt, paid);   running += amt - paid
        else:   # pay
            part = f"💸−{amt:,.2f}";                          running -= amt
        if has_carry and not sep_added and kind != "carry":   # แถวแรกที่ไม่ใช่ยกมา → เส้นคั่น
            lines.append(bar); sep_added = True
        lines.append(f"{fdate(d)}  {part}")
    if with_total:   # กลุ่ม 2: โชว์เฉพาะ 'บรรทัดสุดท้าย' (ยอดค้างสะสมรวม) ไม่โชว์ยอดค้างรายบรรทัด
        lines += [bar, f"💰 ค้างจ่ายสะสม   {running:,.2f} บาท"]
    return "\n".join(lines)


def _payable_report_recipients(acct: str):
    """คืน [(group, with_total), ...] ที่ต้องส่งสรุปหนี้รายวันของบัญชี acct ตอนตี1
    - บัญชี mirror: ส่ง 2 กลุ่ม → กลุ่ม 1 (primary, 'ไม่มียอดรวม') + กลุ่ม 2 (mirror, 'มียอดรวม')
      กลุ่ม 1 ไม่แจ้งเตือนรายตัว แต่ได้สรุปรายวันแบบไม่มียอดค้างสะสม
    - บัญชีปกติ: ส่งกลุ่มเดียว (ใส่ยอดรวม)"""
    if acct in PAYABLE_MIRROR:
        return [(acct, False), (PAYABLE_MIRROR[acct], True)]
    return [(acct, True)]


def _payable_push_summary(event, source_group: str, acct: str):
    """เด้ง 'สรุปหนี้รายวัน' ให้กลุ่มผู้รับ (กลุ่ม 1 ไม่มียอดรวม / กลุ่ม 2 มียอดรวม)
    กลุ่มต้นทาง = reply (ฟรี), กลุ่มอื่น = push — ใช้ตอน 'มีการจ่าย' (ไม่ใช้กับบิลซื้อ)"""
    replied = False
    for grp, with_total in _payable_report_recipients(acct):
        if _group_left(grp) or grp in IGNORE_GROUPS:
            continue
        text = build_payable_ledger(acct, with_total)
        try:
            if grp == source_group and not replied and getattr(event, "reply_token", None):
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
                replied = True
            else:
                _push(grp, TextSendMessage(text=text))
        except Exception as e:
            print(f"[payable] ส่งสรุปไม่สำเร็จ grp={grp}: {e}", flush=True)


def _payable_cleanup_paid(acct: str):
    """ลบบรรทัดบิล/ค้างที่ 'จ่ายครบแล้ว' (amount−paid ≤ 0) — เรียกหลังเด้งสรุป → รอบถัดไปบรรทัดนั้นหาย
    ปลอดภัยต่อยอดรวม: บรรทัดที่จ่ายครบมี (amount−paid)=0 อยู่แล้ว ลบทิ้งยอดค้างไม่เปลี่ยน"""
    with _db() as conn:
        conn.execute(
            "DELETE FROM payable_bills WHERE group_id=? AND COALESCE(paid,0)>0.01 AND (amount - COALESCE(paid,0))<=0.01",
            (acct,))
        conn.commit()


def _payable_reconcile(acct: str):
    """จัดยอดใหม่: จับคู่ 'จ่าย'↔'บิล' ใหม่ (แก้ตัดผิดใบ) โดย 'คงยอดค้างสะสมเดิมเป๊ะ'

    สูตรยอดค้าง = opening + Σ(บิล.amount − บิล.paid) − Σ(จ่าย.amount − จ่าย.allocated)
                = opening + Σบิล − Σจ่าย + orphan   [orphan = Σจ่าย.allocated − Σบิล.paid]

    บั๊กเดิม (เคสจริง 128,206 → −194,437): บิลที่ 'จ่ายครบ' ถูก cleanup ลบทิ้ง แต่ 'จ่าย'
    ที่เคยตัดบิลนั้นยังมี allocated>0 (นี่แหละ orphan). ของเดิม reset allocated=0 ทั้งหมด →
    orphan หาย → จ่ายก้อนนั้นลอย ไม่มีบิลรองรับ → ยอดค้างถูกหักซ้ำจนติดลบ.

    วิธีแก้ (atomic + self-verify — 'ยอมยกเลิก ดีกว่าทำเพี้ยน'):
      1) โหลดข้อมูล + คำนวณ FIFO ใหม่ 'ในหน่วยความจำ' ทั้งหมดก่อน (ยังไม่แตะ DB)
      2) 'ฉีด orphan กลับ' เข้า allocated (ส่วนที่จ่ายบิลเก่าที่ถูกลบไปแล้ว) เพื่อคงยอด
      3) ตรวจ total_before == total_after; ไม่ตรง/ผิดปกติ → raise (ไม่เขียน DB เลย)
      4) ผ่านแล้วค่อยเขียนกลับใน transaction เดียว
    คืน (total_before, total_after)"""
    def _short(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m")
        except (ValueError, TypeError):
            return d or "-"

    with _db() as conn:
        bills = [dict(r) for r in conn.execute(
            "SELECT id, doc_date, amount, COALESCE(paid,0) paid, note FROM payable_bills "
            "WHERE group_id=? ORDER BY doc_date, id", (acct,)).fetchall()]
        pays = [dict(r) for r in conn.execute(
            "SELECT id, doc_date, amount, COALESCE(allocated,0) allocated FROM payable_payments "
            "WHERE group_id=? ORDER BY doc_date, id", (acct,)).fetchall()]
    opening = _payable_opening(acct)

    sum_bill  = sum(float(b["amount"] or 0) for b in bills)
    sum_pay   = sum(float(p["amount"] or 0) for p in pays)
    sum_paid  = sum(float(b["paid"] or 0) for b in bills)
    sum_alloc = sum(float(p["allocated"] or 0) for p in pays)
    orphan    = sum_alloc - sum_paid   # จ่ายที่เคยตัดบิลที่ถูกลบไปแล้ว — ต้องคงไว้ ห้ามทิ้ง
    total_before = opening + sum_bill - sum_pay + orphan
    if orphan < -0.01:
        raise ValueError(f"orphan ผิดปกติ ({orphan:,.2f}) — ข้อมูลไม่สอดคล้อง")

    # (1) FIFO ใหม่ในหน่วยความจำ — มิเรอร์ _payable_settle: ตัดบิล 'วันเดียวกับจ่าย', ยอดตรงก่อน แล้วยกมา แล้ว id
    for b in bills:
        b["new_paid"] = 0.0
    for p in pays:
        p["new_alloc"] = 0.0
        p["dates"] = []
        remaining = float(p["amount"] or 0)
        cands = [b for b in bills
                 if b["doc_date"] == p["doc_date"] and (float(b["amount"] or 0) - b["new_paid"]) > 0.01]
        cands.sort(key=lambda b: (
            0 if abs((float(b["amount"] or 0) - b["new_paid"]) - float(p["amount"] or 0)) < 0.01 else 1,
            0 if b["note"] == _PAYABLE_CARRY_NOTE else 1,
            b["id"]))
        for b in cands:
            if remaining <= 0.01:
                break
            take = min(remaining, float(b["amount"] or 0) - b["new_paid"])
            b["new_paid"]  += take
            p["new_alloc"] += take
            remaining      -= take
            p["dates"].append(_short(b["doc_date"]))

    # (2) ฉีด orphan กลับเข้า allocated ของจ่าย (เรียงตามวัน) เท่าที่ยังมีช่องว่าง (amount − new_alloc)
    if orphan > 0.01:
        remaining = orphan
        for p in pays:
            if remaining <= 0.01:
                break
            spare = float(p["amount"] or 0) - p["new_alloc"]
            if spare <= 0.01:
                continue
            add = min(spare, remaining)
            p["new_alloc"] += add
            remaining      -= add
            if "บิลเก่า" not in p["dates"]:
                p["dates"].append("บิลเก่า")
        if remaining > 0.01:
            raise ValueError(f"ฉีดยอดจ่ายบิลเก่ากลับไม่ครบ (เหลือ {remaining:,.2f})")

    # (3) ตรวจยอดคงเดิมก่อนแตะ DB
    total_after = (opening
                   + sum(float(b["amount"] or 0) - b["new_paid"] for b in bills)
                   - sum(float(p["amount"] or 0) - p["new_alloc"] for p in pays))
    if abs(total_after - total_before) > 0.01:
        raise ValueError(f"ยอดจะเพี้ยน {total_before:,.2f} → {total_after:,.2f}")

    # (4) ผ่านการตรวจ → เขียนกลับใน transaction เดียว (พลาดกลางคัน = rollback อัตโนมัติ)
    with _db() as conn:
        for b in bills:
            conn.execute("UPDATE payable_bills SET paid=? WHERE id=?", (b["new_paid"], b["id"]))
        for p in pays:
            conn.execute("UPDATE payable_payments SET allocated=?, settle_note=? WHERE id=?",
                         (p["new_alloc"], (",".join(p["dates"]) or None), p["id"]))
        conn.commit()
    _payable_cleanup_paid(acct)
    return total_before, total_after


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
    silent = group_id in PAYABLE_MIRROR     # กลุ่ม 1 (mirror primary): ไม่แจ้งเตือนรายตัว — เห็นผลที่ 'สรุปรายวัน' พอ

    def notify(text):
        """แจ้งผลบิล/สลิป — กลุ่ม mirror primary (กลุ่ม 1) เงียบ (ดูสรุปรายวันแทน); กลุ่มอื่นตอบปกติ"""
        if not silent:
            _payable_send(event, group_id, out, text)

    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        info        = extract_payable_doc(image_bytes)
        # อ่านไม่ออก (other) หรืออ่านยอดไม่ได้ → ลองซ้ำด้วย pro (กู้ใบยาก/รูปมืด)
        if (info.get("doc_type") or "other").lower() == "other" or float(info.get("amount") or 0) <= 0:
            try:
                info2 = extract_payable_doc(image_bytes, retry=True)
                if (info2.get("doc_type") or "other").lower() in ("bill", "payment") and float(info2.get("amount") or 0) > 0:
                    info = info2
                    print(f"[payable] pro-retry กู้ได้ group={group_id}", flush=True)
            except Exception as e:
                print(f"[payable] pro-retry พลาด group={group_id}: {e}", flush=True)
    except Exception as e:
        print(f"[payable] อ่านรูปไม่สำเร็จ group={group_id}: {e}", flush=True)
        notify("⚠️ อ่านรูปไม่สำเร็จ กรุณาส่งใหม่อีกครั้ง")
        return

    doc_type = (info.get("doc_type") or "other").lower()
    amount   = float(info.get("amount") or 0)
    doc_date = _sane_doc_date(info.get("doc_date"))   # ใช้วันที่บนเอกสารเฉพาะที่สมเหตุผล (กัน AI อ่านเพี้ยน) ไม่งั้น = วันนี้

    if doc_type == "bill":
        if amount <= 0:
            record_image_miss(acct, "payable_unread")
            notify("🟡 อ่านยอดเงินบนบิลไม่ได้ — โปรดถ่ายให้ชัดแล้วส่งใหม่ หรือพิมพ์ 'บิล <ยอด>' เอง")
            return
        eff_date = doc_date or datetime.now(TZ).date().isoformat()
        if _payable_bill_exists(acct, eff_date, amount):   # กันส่งบิลซ้ำ (วันที่+ยอดเดียวกันมีแล้ว)
            notify(f"🔁 บิลนี้ (วันที่ {eff_date} ยอด {amount:,.2f}) เคยบันทึกแล้ว — ไม่นับซ้ำ")
            return
        # บิลซื้อ: บันทึกเงียบ 'ไม่เด้งสรุป' (จะไปโผล่ในสรุปรอบที่มีการจ่ายถัดไป) — กลุ่ม 1 เงียบอยู่แล้ว
        rid = save_payable_bill(acct, amount, note="รูป", doc_date=doc_date)
        notify(f"📥 บันทึกบิลซื้อ #{rid} — {amount:,.2f} บาท")
        return

    if doc_type == "payment":
        if amount <= 0:
            record_image_miss(acct, "payable_unread")
            notify("🟡 อ่านยอดเงินบนสลิปไม่ได้ — โปรดถ่ายให้ชัดแล้วส่งใหม่")
            return
        ref = info.get("ref_number")
        if _payment_ref_exists(acct, ref):
            notify(f"🔁 สลิปนี้ (อ้างอิง {ref}) เคยบันทึกแล้ว — ไม่นับซ้ำ")
            return
        # ตัดยอดเข้ากับบรรทัดค้าง: ดูวันที่ที่โน้ตบนสลิป → ถ้าไม่มี ลองจับยอดที่ตรงกับบรรทัดเดียว
        pay_for = _sane_doc_date(info.get("pay_for_date"))
        allocated, settled, settle_note = _payable_settle(acct, pay_for, amount)
        save_payable_payment(acct, amount, sender=info.get("sender"),
                             ref_number=ref, slip_dt=info.get("datetime"),
                             doc_date=doc_date, allocated=allocated, settle_note=settle_note)
        # มีการจ่าย → เด้ง 'สรุปหนี้' ทุกครั้ง (กลุ่ม 1 ไม่มียอดรวม / กลุ่ม 2 มียอดรวม) แล้วลบบรรทัดที่จ่ายครบ (รอบหน้าหาย)
        _payable_push_summary(event, group_id, acct)
        _payable_cleanup_paid(acct)
        return

    # other → อ่านไม่ออกว่าเป็นบิล/สลิป — ห้ามทิ้งเงียบ (กันบัญชีคลาดเคลื่อน) ตอบเตือน + นับไว้ให้เห็นในสรุป
    record_image_miss(acct, "payable_unread")
    print(f"[payable] รูปไม่ใช่บิล/สลิป group={group_id}", flush=True)
    notify("🟡 อ่านรูปนี้ไม่ออกว่าเป็นบิลซื้อหรือสลิปจ่าย — ยังไม่ได้บันทึก\n"
           "ถ้าเป็นบิล/สลิปจริง โปรดถ่ายให้ชัดแล้วส่งใหม่ หรือพิมพ์ 'บิล <ยอด>' เอง")


def handle_payable_text(event, text: str, group_id: str) -> bool:
    """คำสั่งบัญชีเจ้าหนี้ในกลุ่ม PAYABLE_GROUPS — คืน True ถ้าจัดการแล้ว
    บัญชีเดียว 2 กลุ่ม (mirror): ทุกคำสั่งทำงานบน acct (บัญชีร่วม) แต่ตอบในกลุ่มที่พิมพ์"""
    low = text.lower().strip()
    acct = _payable_account_key(group_id)   # บัญชีร่วม (primary/mirror ใช้ข้อมูลชุดเดียวกัน)
    out  = _payable_output_group(group_id)  # ผลการ 'บันทึก' เด้งที่ไหน (primary→mirror เงียบในกลุ่มต้นทาง)

    # บล็อก 'ค้าง' = ยอดค้างยกมาแต่ละวัน → ลงเป็น 'บิลรายวัน' (กระจายทุกบรรทัดในรายงาน แล้วเดินยอดต่อ)
    #   ค้าง
    #   6/6=24153 (ค้าง 14153)   ← มีวงเล็บ = จ่ายบางส่วน ใช้เลขในวงเล็บ
    #   8/6=18504                ← ไม่มีวงเล็บ ใช้เลขปกติ
    # วางบล็อกใหม่ = แทนที่ยอดค้างยกมาเดิมทั้งหมด (ไม่บวกเพิ่ม); วันที่ซ้ำ = บวกรวม
    lines = text.splitlines()
    if len(lines) > 1 and lines[0].strip().lower() in ("ค้าง", "ยอดค้าง", "รายการค้าง", "บิลค้าง"):
        entries, errors = [], []
        for ln in lines[1:]:
            ln = ln.strip()
            if not ln or "=" not in ln:
                continue   # ข้ามเส้นคั่น/บรรทัดว่าง (ไม่มี '=')
            dpart, apart = ln.split("=", 1)
            diso = _parse_thai_date(dpart.strip())
            if diso is None:
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
            entries.append((diso, val))
        if errors:
            print(f"[payable-import] ข้าม {len(errors)} บรรทัด: {errors[:5]}", flush=True)
        if not entries:
            _payable_send(event, group_id, out, "❌ อ่านบล็อกค้างไม่ได้ — รูปแบบควรเป็น  วัน/เดือน=ยอด (ค้าง xxxx)")
            return True
        # วางบล็อกใหม่ = ลบยอดค้างยกมาเดิม + ล้าง opening ก้อนเก่า (กันนับเบิ้ล) แล้วลงใหม่เป็นบิลรายวัน
        with _db() as conn:
            conn.execute("DELETE FROM payable_bills WHERE group_id=? AND note=?", (acct, _PAYABLE_CARRY_NOTE))
            conn.commit()
        _set_meta(f"payable_opening:{acct}", "0")
        for diso, val in entries:
            save_payable_bill(acct, val, note=_PAYABLE_CARRY_NOTE, doc_date=diso)
        # ไม่ส่งข้อความยืนยัน — เด้ง 'สรุปหนี้' (ผลลัพธ์จริง) ให้ดูแทน
        _payable_push_summary(event, group_id, acct)
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
                _push(grp, TextSendMessage(text=build_payable_ledger(acct, with_total)))
                sent.append(f"{grp} ({'มียอดรวม' if with_total else 'ไม่มียอดรวม'})")
            except Exception as e:
                errs.append(f"{grp}: {e}")
        msg = f"✅ ส่งสรุปหนี้รายวันแล้ว {len(sent)} กลุ่ม"
        if errs:
            msg += "\n❌ ส่งไม่สำเร็จ:\n" + "\n".join(errs)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return True

    # จัดยอดใหม่ — จับคู่ 'จ่าย'↔'บิล' ใหม่ (แก้ตัดผิดใบ) แบบ 'คงยอดค้างเดิมเป๊ะ' + ตรวจยอดก่อนเขียน
    # ถ้าคำนวณแล้วยอดจะเพี้ยน/ผิดปกติ → ยกเลิกทั้งหมด ไม่แตะข้อมูลเดิม (กันบั๊กเก่าที่เคยทำ 128,206 → −194,437)
    if low in ("จัดยอดใหม่", "คำนวณยอดใหม่", "รีคอนยอด", "จัดยอด", "คำนวณยอด"):
        try:
            before, after = _payable_reconcile(acct)
        except Exception as e:
            print(f"[payable] จัดยอดใหม่ ยกเลิก group={group_id}: {e}", flush=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"🛑 จัดยอดใหม่ไม่สำเร็จ — ยกเลิกแล้ว ไม่แตะข้อมูลเดิมสักรายการ\n"
                     f"เหตุผล: {e}\n(ยอดค้างที่แสดงอยู่ตอนนี้ยังถูกต้องเหมือนเดิม)"))
            return True
        _payable_send(event, group_id, out,
            f"🔄 จัดยอดใหม่เรียบร้อย — จับคู่จ่าย↔บิลใหม่ ยอดค้างคงเดิม\n"
            f"💰 ค้างจ่าย {PAYABLE_VENDOR} สะสม: {after:,.2f} บาท\n"
            "─────────────────\n"
            + build_payable_ledger(acct, with_total=True))
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
            # ตารางกระทบบิล-สลิป (โอนขาด/เกิน) เก่าเกิน SLIP_KEEP_DAYS — ยอดสะสมเก็บใน meta แยก ไม่โดนลบ
            conn.execute("DELETE FROM dining_bills WHERE bill_date < ?", (slip_cutoff,))
            conn.execute("DELETE FROM dining_pairs WHERE pair_date < ?", (slip_cutoff,))
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
    # กลุ่มแผน B (ผูก OA เฉพาะใน OA_ROUTE) = จงใจให้มี OA อยู่ → ไม่ให้มาร์ค 'left' มา suppress การส่งถาวร
    # (กัน trap: เผลอมาร์ค left ตอน setup ยังไม่พร้อม แล้วรายงานเงียบตลอดไป เพราะ push-only ไม่มี event มา self-heal)
    # ถ้าส่งไม่ได้จริงจะ error ทุกครั้ง (เห็นใน log) แต่พอส่งได้ push สำเร็จจะ _clear_group_left ให้เอง
    if gid in _oa_route:
        return False
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


def _is_content_gone(e: Exception) -> bool:
    """โหลดรูปจาก LINE ไม่ได้เพราะ 'รูปหาย' — 410 'content is gone' (ยกเลิกข้อความ/หมดอายุ) หรือ 404
    คนละเรื่องกับ Gemini quota โดยสิ้นเชิง (พังตั้งแต่ขั้นดาวน์โหลด ยังไม่ถึง Gemini)"""
    return isinstance(e, LineBotApiError) and getattr(e, "status_code", None) in (404, 410)


def _looks_like_gemini_issue(e) -> bool:
    """error นี้น่าจะมาจาก Gemini (rate-limit/quota/overload) — ใช้เลือกข้อความเตือนแอดมินให้ตรงเหตุ"""
    s = str(e).lower()
    return any(k in s for k in ("resource_exhausted", "quota", "rate-limit", "rate_limit",
                                "rate limit", "429", "503", "overload", "gemini"))


def _is_gemini_credits_error(e) -> bool:
    """429 เพราะ 'เครดิต Gemini (prepay) หมด' — ต้องเติมเงิน ไม่ใช่รอ (ต่างจาก rate-limit ชั่วคราว)
    → ใช้ trigger circuit breaker: หยุดยิง Gemini ชั่วคราว + เตือนเติมด่วน (แทน re-queue ไล่ยิงจนเปลือง)"""
    s = str(e).lower()
    return ("resource_exhausted" in s or "429" in s) and any(
        k in s for k in ("credit", "depleted", "prepay", "billing", "balance", "insufficient"))


def _is_quota_error(e: Exception) -> bool:
    """push โดนปฏิเสธเพราะเต็มโควต้า push รายเดือน (429 / ข้อความมี monthly/limit/quota)
    ใช้เป็น safety net: ถ้าตัวนับพลาดหรือ OA เต็มก่อนถึง PUSH_FREE_LIMIT → สลับ OA ถัดไปทันที ไม่ให้ตกหล่น"""
    if not isinstance(e, LineBotApiError) or getattr(e, "status_code", None) != 429:
        return False
    msg = (str(getattr(e, "message", "")) + " " + str(getattr(e, "error", "")) + " " + str(e)).lower()
    return ("monthly" in msg) or ("limit" in msg) or ("quota" in msg) or (msg.strip() == " ")  # 429 มักเป็นเต็มโควต้า


def _push_month_key(idx: int) -> str:
    """คีย์นับโควต้า push รายเดือนต่อ OA — reset เองเมื่อขึ้นเดือนใหม่ (คีย์เปลี่ยน)
    OA1='push_count:YYYY-MM', OA2='push_count2:YYYY-MM', ..."""
    mon = datetime.now(TZ).strftime("%Y-%m")
    suffix = "" if idx == 0 else str(idx + 1)
    return f"push_count{suffix}:{mon}"


def _push_count(idx: int) -> int:
    try:
        return int(_get_meta(_push_month_key(idx)) or "0")
    except (TypeError, ValueError):
        return 0

_real_usage_cache = {}   # idx -> (usage, expires_epoch) กันยิง API ถี่
def _line_real_usage(idx: int):
    """ยอด push จริงเดือนนี้จาก LINE (ตรงกับ OA Manager) — บอทนับเองมักต่ำกว่า เพราะ LINE รวมบรอดแคสต์/ข้อความอื่นด้วย"""
    now = time.time()
    hit = _real_usage_cache.get(idx)
    if hit and hit[1] > now:
        return hit[0]
    try:
        val = int(_push_apis[idx].get_message_quota_consumption().total_usage)
    except Exception:
        val = None
    _real_usage_cache[idx] = (val, now + 300)   # cache 5 นาที
    return val


# แคชจำนวนสมาชิกกลุ่ม/ห้อง — LINE นับ 'push เข้ากลุ่ม 1 ครั้ง' = จำนวนสมาชิก (ไม่ใช่ 1)
# จึงต้องรู้จำนวนสมาชิกเพื่อให้ตัวนับโควต้าตรงกับที่ LINE คิดจริง (เดิมนับเป็น 'จำนวน bubble' → ต่ำกว่าจริงมาก)
_member_count_cache = {}     # id -> (count, expires_epoch); กันยิง API ถี่ทุก push
MEMBER_COUNT_TTL = int(os.environ.get("MEMBER_COUNT_TTL", "3600"))

def _recipient_count(to, api=None) -> int:
    """จำนวน 'ผู้รับจริง' ของปลายทาง push = ที่ LINE ใช้คิดโควต้า
    - user (Uxxx) = 1
    - group (Cxxx) / room (Rxxx) = จำนวนสมาชิก (แคช MEMBER_COUNT_TTL วิ กันยิง API ถี่)
    ⚠️ ต้องนับด้วย OA ที่ 'จะส่งจริง' (api) — OA อื่นไม่ได้อยู่ในกลุ่มนั้น get_members_count จะ error → นับเป็น 1 ผิด
    LINE: push เข้ากลุ่ม 1 ครั้ง (กี่ bubble ก็ตาม) นับ = จำนวนสมาชิก | reply ฟรี ไม่นับเลย"""
    if not isinstance(to, str) or not to or to[0] not in ("C", "R"):
        return 1
    now = time.time()
    hit = _member_count_cache.get(to)
    if hit and hit[1] > now:
        return hit[0]
    api = api or line_bot_api
    try:
        cnt = int(api.get_group_members_count(to) if to[0] == "C"
                  else api.get_room_members_count(to))
        cnt = max(1, cnt)
    except Exception as e:
        # นับไม่ได้ (เน็ต/บอทออกจากกลุ่ม) → ใช้ค่าที่แคชไว้ ไม่งั้น 1 (อย่างน้อยไม่พัง/ไม่บล็อกการส่ง)
        print(f"[push] นับสมาชิก {to} ไม่ได้: {e}", flush=True)
        cnt = hit[0] if hit else 1
    _member_count_cache[to] = (cnt, now + MEMBER_COUNT_TTL)
    return cnt


def _push_warn_key(idx: int = 0) -> str:
    """คีย์ 'OA นี้เตือนใกล้เต็มแล้วเดือนนี้' (แยกราย OA) — reset เองขึ้นเดือนใหม่ (คีย์เปลี่ยน)"""
    suffix = "" if idx == 0 else str(idx + 1)
    return f"push_warned{suffix}:{datetime.now(TZ).strftime('%Y-%m')}"


def _notify_push_near_limit(idx: int, cnt: int):
    """เตือนเจ้าของครั้งเดียว/เดือน เมื่อ OA (ที่ไม่มีตัวสำรองต่อ) push ใกล้เต็มโควต้าฟรี
    สัญญาณ 'push ไม่พอแล้ว' → ถึงเวลาลด push หรือแยกกลุ่มไปอีก OA ก่อนข้อความตกหล่น"""
    if not ADMIN_USER_ID:
        return
    try:
        _push(ADMIN_USER_ID, TextSendMessage(
            text=(f"⚠️ push OA{idx+1} เดือนนี้ใช้ไป {cnt}/{PUSH_FREE_LIMIT} ข้อความ (ใกล้เต็มโควต้าฟรี)\n"
                  "OA นี้ใกล้ตันแล้ว — ถ้าเต็ม ข้อความที่บอทเด้งเอง (รายงาน/ตื๊อจอง) ของกลุ่มที่ผูกกับ OA นี้จะตกหล่น\n"
                  "แก้: ลด push ที่ไม่จำเป็น หรือย้าย/แยกกลุ่มไปอีก OA\n"
                  "พิมพ์ 'โควต้า' ในกลุ่มเพื่อดูรายละเอียด")))
    except Exception as e:
        print(f"[push-warn] เตือนเจ้าของไม่สำเร็จ: {e}", flush=True)


def _push(to, messages):
    """ส่ง push โดยเลือก OA ตาม 'กลุ่ม→OA' (OA_ROUTE) — LINE ให้ OA อยู่กลุ่มละตัว จึงไม่มี failover สลับในกลุ่มเดียว
    - ปลายทางที่แมปใน OA_ROUTE → OA ตัวนั้น; ปลายทางอื่น → OA1 (เอด) เสมอ
    - ส่งสำเร็จ → นับ +จำนวนผู้รับจริง (push เข้ากลุ่ม = จำนวนสมาชิก ตามที่ LINE คิดโควต้า) ให้ OA ตัวนั้น
    - error (เช่น 400 not-member / 429) → re-raise ให้ call site เดิมจัดการ (mark left / นับ fail) เหมือนเดิม
    - OA แตะเกณฑ์เตือน → เตือนเจ้าของครั้งเดียว/เดือน/OA (นอก lock กัน deadlock)
    หมายเหตุ: reply_message ยังใช้ OA1 ตรงๆ (ฟรี ไม่ผ่านฟังก์ชันนี้)"""
    n = len(_push_apis)
    # LINE ให้ OA อยู่กลุ่มละตัว → OA2/3/4 ไม่เคยอยู่ร่วมกลุ่มกับเอด → "failover สลับ OA ในกลุ่มเดียว" ทำไม่ได้เลย
    #   - ปลายทางที่แมปใน OA_ROUTE → ส่งผ่าน OA ตัวนั้นตัวเดียว
    #   - ปลายทางอื่นทั้งหมด (DM แอดมิน / กลุ่มที่มีแต่เอด เช่นกลุ่มหนี้) → OA1 (เอด) เสมอ ไม่ cascade
    #     ⚠️ เดิม cascade ไป OA ถัดไปเมื่อเอดครบโควต้า — พอเพิ่ม token OA2 แต่มันไม่ได้อยู่ในกลุ่มนั้น → push ล้ม 'not member'
    route = _oa_route.get(to) if isinstance(to, str) else None
    candidates = [i for i in route if i < n] if route else [0]   # ลิสต์ OA (หลายตัว = สลับอัตโนมัติเมื่อเต็ม)
    if not candidates:
        candidates = [0]
    # LINE นับ push เข้ากลุ่ม = จำนวนสมาชิก — นับด้วย OA ที่จะส่งจริง (candidates[0]) ไม่งั้น OA อื่นนับกลุ่มไม่ได้ = นับผิดเป็น 1
    inc = _recipient_count(to, _push_apis[candidates[0]])
    last = candidates[-1]
    warn_idx = warn_at = None
    with _push_lock:
        last_err = None
        sent = False
        for idx in candidates:
            try:
                _push_apis[idx].push_message(to, messages)
                new_cnt = _push_count(idx) + inc
                _set_meta(_push_month_key(idx), str(new_cnt))
                if isinstance(to, str) and to[:1] in ("C", "R"):
                    _clear_group_left(to)   # push สำเร็จ = ยังอยู่ในกลุ่ม → ปลดมาร์ค left ถ้าเคยถูกมาร์คพลาด (self-heal)
                if idx != candidates[0]:
                    print(f"[push] ส่งผ่าน OA{idx+1} (OA ก่อนหน้าเต็มโควต้า) → {to}", flush=True)
                # เตือนเมื่อ OA 'ตัวท้ายสุดที่ยังลองได้' ใกล้เต็ม (ไม่มีตัวสำรองต่อ) — มาร์คใน lock กันส่งซ้ำ, ส่งจริงนอก lock
                if idx == last and new_cnt >= PUSH_WARN_AT and _get_meta(_push_warn_key(idx)) != "1":
                    _set_meta(_push_warn_key(idx), "1")
                    warn_idx, warn_at = idx, new_cnt
                sent = True
                break
            except Exception as e:
                last_err = e
                # สลับ OA ถัดไปเมื่อ: เต็มโควต้า (429) หรือส่งไม่ได้ (400 เช่น OA ไม่ได้อยู่ในกลุ่ม) — ถ้ายังมีตัวสำรอง
                if idx != last and isinstance(e, LineBotApiError) and getattr(e, "status_code", None) in (400, 429):
                    if _is_quota_error(e):
                        _set_meta(_push_month_key(idx), str(PUSH_FREE_LIMIT))  # มาร์คเต็ม กันเลือกซ้ำเดือนนี้
                    print(f"[push] OA{idx+1} ส่งไม่ได้ ({getattr(e,'status_code','?')}) → ลอง OA ถัดไป", flush=True)
                    continue
                raise
        if not sent and last_err:   # ตัวท้ายสุดก็ยังเจอ quota error → re-raise ให้ call site รู้ว่าส่งไม่ได้
            raise last_err
    if warn_at is not None:   # ส่งเตือนนอก _push_lock (เพราะ _notify → _push วนกลับมา acquire lock อีก)
        _notify_push_near_limit(warn_idx, warn_at)


def _report_off(gid: str) -> bool:
    """กลุ่มนี้สั่ง 'ปิดรายงาน' ไว้ไหม — ยังเช็คสลิปปกติ แต่ไม่ส่งรายงานสรุปประจำวัน (00:30)"""
    return _get_meta(f"noreport:{gid}") == "1"


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
        # ข้ามกลุ่มที่บอทถูกเตะออก (_group_left) / สั่งเมิน (IGNORE_GROUPS) / สั่งปิดรายงาน/ปิดสลิป — กันค้าง retry/สแปม
        targets = [g for g in group_ids
                   if _slip_enabled(g) and not _group_left(g)
                   and g not in IGNORE_GROUPS and g not in PAYABLE_GROUPS
                   and not _report_off(g)]
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
                _push(dest, TextSendMessage(text=build_daily_report(group_id, yesterday)))
                _mark_sent("report", today, group_id)
                print(f"[report] sent OK → {dest}" + (f" (redirect จาก {group_id})" if dest != group_id else ""), flush=True)
            except Exception as e:
                if _is_not_member_error(e):
                    _mark_group_left(dest)   # บอทไม่ได้อยู่ในกลุ่มปลายทางแล้ว → เลิก retry (ไม่นับเป็น fail)
                else:
                    failed += 1
                print(f"[report] push FAILED {dest}: {e}", flush=True)
        # หมายเหตุ: สรุปจองแยกไปที่ maybe_send_resv_summary แล้ว (จองล่วงหน้าถึงวันงาน 11:00 / จองในวัน 16:00)
        # มาร์คว่า 'ส่งครบแล้ว' เฉพาะเมื่อไม่มีอันไหนล้มเหลว — ถ้ามี fail ปล่อยไว้ให้ ping รอบหน้า retry
        if failed == 0:
            _set_meta("last_report_date", today)
            _cleanup_old_data()          # ลบข้อมูลเก่าวันละครั้ง กัน DB บวม
            print(f"[report] done {today}", flush=True)
        else:
            print(f"[report] {failed} กลุ่มส่งไม่สำเร็จ — จะ retry รอบ ping ถัดไป (~5 นาที)", flush=True)


def maybe_send_resv_summary():
    """ส่งรายงานจองอัตโนมัติ 2 รอบ/วัน (idempotent แยกรอบ):
      • รอบเช้า RESV_ADVANCE_SUMMARY_HOUR (ดีฟอลต์ 11:00) → 'จองล่วงหน้าที่ถึงวันงานวันนี้'
        (จองมาก่อนหน้า วันงานคือวันนี้ — โผล่เฉพาะวันงาน ไม่โผล่ทุกวันตั้งแต่จอง)
      • รอบบ่าย RESV_TODAY_SUMMARY_HOUR   (ดีฟอลต์ 16:00) → 'จองในวัน' (จองวันนี้เพื่อวันนี้)
    แต่ละรอบส่งครั้งเดียว/วันในกรอบ [ชม., +3); วันไหนไม่มีจอง = ไม่ส่ง (ประหยัด push)"""
    now = datetime.now(TZ)
    _maybe_send_resv_slot(
        now, RESV_ADVANCE_SUMMARY_HOUR, "resv_adv_summary",
        lambda h: build_resv_summary(None, f"📅 จองล่วงหน้าวันนี้ ({h:02d}:00)", date_mode="advance_arrived", skip_if_empty=True))
    _maybe_send_resv_slot(
        now, RESV_TODAY_SUMMARY_HOUR, "resv_today_summary",
        lambda h: build_resv_summary(None, f"📋 จองในวันวันนี้ ({h:02d}:00)", date_mode="sameday", skip_if_empty=True))


def _maybe_send_resv_slot(now, start_hour, job, build_text):
    """ส่งรายงานจองรอบหนึ่ง(idempotent/วัน) เข้ากลุ่ม staff กลุ่มเดียว ในกรอบ [start_hour, +3 ชม.)
    build_text(start_hour) → ข้อความ (None = ไม่มีจอง ไม่ส่ง); เลยกรอบ = ข้ามวันนี้ (กันส่งผิดเวลา)"""
    hm  = (now.hour, now.minute)
    end_hour = min(start_hour + 3, 24)   # กรอบส่ง 3 ชม. (ไม่ข้ามเที่ยงคืน)
    if hm < (start_hour, 0):
        return
    today = now.date().isoformat()
    meta_key = f"last_{job}_date"
    with _report_lock:
        if _get_meta(meta_key) == today:
            return
        if hm >= (end_hour, 0):
            # เลยกรอบเวลาส่งแล้ว (เช่น บอทเพิ่งตื่น/deploy หลังกรอบ) → มาร์คข้ามวันนี้ ไม่ส่งผิดเวลา
            _set_meta(meta_key, today)
            print(f"[{job}] เลย {end_hour}:00 แล้ว ข้ามวันนี้ (พรุ่งนี้ส่ง {start_hour}:00)", flush=True)
            return
        # สรุปจอง (ทั้งรอบล่วงหน้า-ถึงวันงาน + รอบจองในวัน) ส่งเข้ากลุ่ม staff กลุ่มเดียว
        # ข้ามถ้าบอทถูกเตะออก (_group_left) / สั่งเมิน (IGNORE_GROUPS) — กันค้าง retry/สแปม
        targets = list(dict.fromkeys(
            [t for t in [STAFF_GROUP_ID]
             if t and not _group_left(t) and t not in IGNORE_GROUPS]))
        if not targets:
            _set_meta(meta_key, today)
            return
        text = build_text(start_hour)
        if text is None:
            _set_meta(meta_key, today)
            print(f"[{job}] วันนี้ไม่มีจอง — ไม่ส่ง (ประหยัด)", flush=True)
            return
        failed = 0
        # idempotent รายกลุ่ม: กลุ่มที่ส่งสำเร็จแล้ววันนี้จะไม่ส่งซ้ำ แม้กลุ่มอื่นพลาดแล้วต้อง retry
        for g in targets:
            if _already_sent(job, today, g):
                continue
            # สรุปจอง 'ไม่ใช้ REPORT_REDIRECT' — ส่งไปกลุ่มของมันเอง (สลิป/บาร์น้ำ) ให้จองสรุปอยู่กลุ่มเดิม
            # (ต่างจากรายงานสลิป 00:30 ที่ redirect เข้า management ได้)
            dest = g
            if _group_left(dest) or dest in IGNORE_GROUPS:
                continue
            try:
                _push(dest, TextSendMessage(text=text))
                _mark_sent(job, today, g)
                print(f"[{job}] sent → {dest}", flush=True)
            except Exception as e:
                if _is_not_member_error(e):
                    _mark_group_left(dest)
                else:
                    failed += 1
                print(f"[{job}] FAILED {dest}: {e}", flush=True)
        if failed == 0:
            _set_meta(meta_key, today)
            print(f"[{job}] done {today}", flush=True)
        else:
            print(f"[{job}] {failed} กลุ่มส่งไม่สำเร็จ — retry รอบหน้า", flush=True)


def maybe_send_payable_summary():
    """[ปิดการใช้งาน] เดิมส่งสรุปหนี้รายวันตอนตี1 — เปลี่ยนเป็น 'เด้งสรุปทุกครั้งที่มีการจ่าย' แทน
    (ดู _payable_push_summary ใน payment branch) จึงไม่ส่งอัตโนมัติตามเวลาแล้ว"""
    return
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
                    _push(grp, TextSendMessage(text=build_payable_ledger(acct, with_total)))
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
        if _in_deep_quiet():
            time.sleep(1800)   # เงียบลึก → ไม่มีรายงานตามเวลา หยุดเช็ค ปล่อย Neon หลับ
            continue
        time.sleep(300)
        try:
            _check_memory()           # เฝ้า memory ทุก ~5 นาที → เตือนแอดมินถ้าใกล้ลิมิต
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


def _resv_guide() -> str:
    """คู่มือจองย่อ (วิธีจอง + ตัวอย่าง) — แนบตอนจองข้อมูลไม่ครบ ให้แจ้งใหม่ได้ถูกเลย"""
    return ("📋 วิธีจองที่ถูกต้อง — พิมพ์ให้ครบ 4 อย่าง:\n"
            "1) ชื่อผู้จอง  2) จำนวนคน  3) วันเวลา  4) โซน\n"
            "ตัวอย่าง:\n"
            "• จองโต๊ะคุณเอ 4คน วันนี้ 2ทุ่ม โซนA\n"
            "• จองคุณบี 6คน เสาร์นี้ 1ทุ่ม โซนริมน้ำ\n"
            "(จองล่วงหน้าใส่วันด้วย เช่น พรุ่งนี้/เสาร์นี้/25 มิ.ย.) — พิมพ์ 'คู่มือ' ดูทั้งหมด")


def _reply_with_mention(event, body: str):
    """ตอบกลับพร้อม @mention 'คนที่พิมพ์' (กดได้/เด้งเตือน) ผ่าน LINE v3 (textV2 + mention)
    ถ้าพลาด → fallback เป็นข้อความธรรมดา (ใส่ @ชื่อ นำหน้า) เพื่อให้ข้อความถึงเสมอ"""
    uid = getattr(event.source, "user_id", None)
    if uid:
        try:
            from linebot.v3.messaging import (Configuration, ApiClient, MessagingApi,
                ReplyMessageRequest, TextMessageV2, MentionSubstitutionObject, UserMentionTarget)
            with ApiClient(Configuration(access_token=LINE_TOKEN)) as _api:
                MessagingApi(_api).reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessageV2(
                        text="{u} " + body,
                        substitution={"u": MentionSubstitutionObject(
                            type="mention",
                            mentionee=UserMentionTarget(type="user", user_id=uid))})]))
            return
        except Exception as e:
            print(f"[resv] v3 mention reply พลาด → fallback ข้อความธรรมดา: {e}", flush=True)
    # fallback: ข้อความธรรมดา (อาจไม่ใช่ mention จริง แต่ใส่ชื่อให้เห็นว่าเรียกใคร)
    try:
        name = get_display_name(event.source)
        prefix = f"@{name}\n" if name and name != "ไม่ทราบชื่อ" else ""
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=prefix + body))
    except Exception as e:
        print(f"[resv] fallback reply พลาด: {e}", flush=True)


_TH_WD = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
_TH_MON = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

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
        "is_reservation: true เฉพาะเมื่อเป็นการจองโต๊ะจริง (มีเจตนานัด/จองที่นั่งให้ลูกค้า) — "
        "รวมถึงข้อความที่พนักงานพิมพ์ขึ้นต้นว่า 'รับจอง ...' เพื่อบันทึกจองให้ลูกค้า (ถือเป็นการจองจริง)\n"
        "is_advance: true ถ้าเป็นการจอง 'ล่วงหน้า' (สำหรับวันอื่น/วันข้างหน้า เช่น พรุ่งนี้ เสาร์นี้ วันที่ 25 หรือวันที่ตัวเลขที่ไม่ใช่วันนี้ ฯลฯ), "
        "false ถ้าจองสำหรับ 'วันนี้/คืนนี้/ตอนนี้'\n"
        "customer: ชื่อลูกค้า/ผู้จอง (ถ้ามี)\n"
        "people: จำนวนคน เช่น '4 คน' (ถ้ามี)\n"
        "date: วันที่จองแบบอ่านง่าย เช่น 'วันนี้','พรุ่งนี้','เสาร์นี้','25 มิ.ย.' — จองวันนี้/คืนนี้ใส่ 'วันนี้'; "
        "ถ้าเป็นจองล่วงหน้าแต่ไม่ระบุว่าวันไหน ให้เป็น null\n"
        f"resv_date: วันที่จองเป็นตัวเลข YYYY-MM-DD คำนวณจากวันนี้ ({today_str}) เช่น "
        "จองวันนี้/คืนนี้=วันนี้, พรุ่งนี้=+1วัน, มะรืน=+2วัน, 'เสาร์นี้/ศุกร์นี้ฯลฯ'=วันนั้นที่จะถึงถัดไป, "
        "'วันที่ 25'=วันที่ 25 เดือนนี้ (ถ้าผ่านแล้วเป็นเดือนหน้า); "
        "**รองรับวันที่แบบตัวเลข 'วัน/เดือน' หรือ 'วัน/เดือน/ปี' ด้วย** เช่น '16/7'=16 ก.ค.ปีนี้; "
        "ปีที่เป็น 2 หลักหรือ ≥2500 = พ.ศ. ให้ลบ 543 เป็น ค.ศ. เช่น '16/7/69'→2026-07-16, '16/7/2569'→2026-07-16, "
        "'16/7/2026'→2026-07-16; ถ้าไม่ระบุวันให้ null\n"
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
    response = _gemini_generate(prompt, json_mode=True)
    # parse แบบทนทาน: ถ้าโมเดลตอบไม่เป็น JSON ให้ถือว่า 'ไม่ใช่การจอง' (เงียบ) แทนที่จะ error
    return _parse_gemini_json(response, {"is_reservation": False})


def _resv_detail_lines(r: dict) -> str:
    # รองรับทั้ง dict จาก AI (datetime/table) และแถวจาก DB (resv_datetime/table_no)
    cust = r.get("customer")
    ppl  = r.get("people")
    dt   = _resv_when_label(r)   # คำนวณวันใหม่เทียบวันนี้ (กัน 'พรุ่งนี้' ค้าง)
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
    """จองวันนี้ → การ์ด+ปุ่มไปกลุ่ม staff (STAFF_GROUP_ID) คอนเฟิร์ม (รับจากกลุ่มใน RESV_GROUPS)
    จองล่วงหน้า → ส่งการ์ด+ปุ่มไปกลุ่มบาร์น้ำ (BAR_GROUP_ID) คอนเฟิร์ม จากกลุ่มไหนก็ได้
    กลุ่มต้นทางได้ 'ข้อมูลจอง (ไม่มีปุ่ม)' + เด้งผลคอนเฟิร์มกลับ
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
        body = ("📝 ขอข้อมูลจองเพิ่มครับ ยังขาด:\n" + "\n".join(missing) +
                "\n📖 กรุณาอ่านคู่มือและทำให้ถูกต้องด้วย"
                "\n─────────────────\n" + _resv_guide())
        _reply_with_mention(event, body)
        return True

    # ── เช็คเวลาจองอยู่ในเวลาเปิดร้าน (11:00–00:00) ไหม — นอกเวลาแค่ 'เตือน' ไม่บล็อก ──
    tmin = _parse_hhmm(info.get("time_hhmm"))
    time_warn = "" if _within_open_hours(tmin) else \
        f"\n⚠️ เวลา {info.get('time_hhmm')} อยู่นอกเวลาเปิดร้าน (11:00–00:00) โปรดตรวจสอบ"

    # ปลายทางการ์ด+ปุ่มคอนเฟิร์ม (ใครเป็นคนกดยืนยันจอง):
    #  - จองวันนี้     → กลุ่ม staff (= กลุ่มส่งสลิป) เป็นคนคอนเฟิร์ม
    #  - จองล่วงหน้า   → กลุ่มบาร์น้ำ เป็นคนคอนเฟิร์ม
    # ถ้ากลุ่มต้นทาง = กลุ่มที่ต้องคอนเฟิร์มอยู่แล้ว → การ์ดขึ้นในกลุ่มเดิม (reply)
    # ถ้าไม่ใช่ → กลุ่มต้นทางได้ 'ข้อมูลจอง (ไม่มีปุ่ม)' + การ์ด+ปุ่มไปกลุ่มที่คอนเฟิร์ม แล้วเด้งผลยืนยันกลับ
    dest = (BAR_GROUP_ID if is_advance else STAFF_GROUP_ID) or group_id
    resv_id = save_reservation(group_id, requested_by, info, text, dest)
    detail = _resv_detail_lines(info)
    head = "📅 จองล่วงหน้า" if is_advance else "🔔 จองโต๊ะ"

    detail_msg = TextSendMessage(
        text=f"{head}ใหม่ #{resv_id}\nจากคุณ {requested_by}\n─────────────────\n{detail}{time_warn}")
    confirm_msg = _resv_confirm_card(resv_id, head)

    dest_name = "กลุ่มบาร์น้ำ" if is_advance else "กลุ่ม staff"
    print(f"[resv] จอง #{resv_id} advance={is_advance} → การ์ดที่ {dest} ({dest_name})", flush=True)
    if dest == group_id:
        line_bot_api.reply_message(event.reply_token, [detail_msg, confirm_msg])
    else:
        _push(dest, [detail_msg, confirm_msg])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"📤 ส่งจอง #{resv_id} ให้{dest_name}คอนเฟิร์มแล้ว รอยืนยัน\n─────────────────\n{detail}{time_warn}"))
    # แผน B: เด้งสำเนา 'ข้อมูลจอง' (ไม่มีปุ่ม) ให้บาร์น้ำ/sound — 'เฉพาะจองล่วงหน้า' (จองวันนี้ไม่ต้องเด้ง)
    if is_advance:
        _resv_broadcast_info(resv_id, head, f"จากคุณ {requested_by}\n{detail}{time_warn}", skip_group=dest)
    return True


def _resv_is_advance(resv: dict) -> bool:
    """จองล่วงหน้า = วันงาน (resv_date) เป็นวันหลังวันที่แจ้งจอง (created_at); ไม่มี resv_date/วันเดียวกัน = จองวันนี้"""
    rd = resv.get("resv_date")
    return bool(rd) and rd > (resv.get("created_at") or "")[:10]


def _resv_broadcast_info(resv_id: int, head: str, detail: str, skip_group: str = None):
    """แผน B: เด้ง 'ข้อมูลจอง (ไม่มีปุ่มคอนเฟิร์ม)' ให้กลุ่มใน RESV_INFO_GROUPS (บาร์น้ำ/sound) รับรู้
    push ผ่าน OA ของกลุ่มนั้น (ตาม OA_ROUTE); คอนเฟิร์มยังทำที่กลุ่มรับจองที่เดียว — กลุ่มพวกนี้แค่ดู
    หมายเหตุ: เรียกเฉพาะ 'จองล่วงหน้า' เท่านั้น (จองวันนี้ไม่ broadcast) — เช็คที่ call site"""
    if not RESV_INFO_GROUPS:
        return
    msg = TextSendMessage(text=f"{head} #{resv_id}\n─────────────────\n{detail}")
    for g in RESV_INFO_GROUPS:
        if g == skip_group or _group_left(g) or g in IGNORE_GROUPS:
            continue
        try:
            _push(g, msg)
        except Exception as e:
            print(f"[resv] เด้งข้อมูลจอง → {g} ล้มเหลว: {e}", flush=True)


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
        # จองคอนเฟิร์มไปแล้ว + มีคนกดปุ่มซ้ำ → 'เงียบ' ไม่ตอบเตือน (ตามที่เจ้าของสั่ง กันข้อความรก)
        print(f"[resv] จอง #{resv_id} ถูกคอนเฟิร์มแล้ว มีคนกดซ้ำ — ข้ามเงียบๆ", flush=True)
        return

    mark_reservation_confirmed(resv_id, confirmer)
    # ตอบในกลุ่มที่กดปุ่มคอนเฟิร์ม
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"✅ การจอง #{resv_id} คอนเฟิร์มแล้ว!\nโดย {confirmer}\n─────────────────\n"
             f"ผู้แจ้งจอง: {resv.get('requested_by','-')}\n{detail}"))

    # แจ้งกลับกลุ่มต้นทาง ถ้าต่างจากกลุ่มที่กด
    #  - จองล่วงหน้า: บาร์น้ำกด → แจ้งกลับกลุ่มที่แจ้ง
    #  - จองวันนี้: staff กด → แจ้งกลับกลุ่มที่แจ้ง
    # ถ้าต้นทาง = กลุ่มที่กด (จองในกลุ่มเดียวกัน) → ไม่ต้องแจ้งซ้ำ
    origin = resv.get("origin_group_id")
    if origin and origin != pressed_group:
        by_name = "บาร์น้ำ" if _resv_is_advance(resv) else "staff"
        kind = "จองล่วงหน้า" if _resv_is_advance(resv) else "จอง"
        try:
            _push(origin, TextSendMessage(
                text=f"✅ {kind} #{resv_id} ที่แจ้งไว้ {by_name}คอนเฟิร์มแล้ว!\n"
                     f"โดย {confirmer}\n─────────────────\n{detail}"))
        except Exception as e:
            print(f"[resv] notify origin failed: {e}", flush=True)

    # แผน B: อัปเดตสถานะ 'คอนเฟิร์มแล้ว' ให้บาร์น้ำ/sound — เฉพาะจองล่วงหน้า (ที่เคยเด้งข้อมูลไว้เท่านั้น)
    if _resv_is_advance(resv):
        _resv_broadcast_info(resv_id, "✅ คอนเฟิร์มจองแล้ว", f"{detail}\nโดย {confirmer}", skip_group=pressed_group)


def _touch_reminded(resv_id: int, when: str):
    with _db() as conn:
        conn.execute("UPDATE reservations SET reminded_at=? WHERE id=?", (when, resv_id))
        conn.commit()


def _resv_remind_due(r: dict, now, interval_min: int) -> bool:
    """จองนี้ถึงเวลาย้ำเตือนไหม
    ⏰ ตื๊อทุกจองที่ยัง PENDING (ทั้งจองวันนี้และจองล่วงหน้า) — ตื๊อ 'ตั้งแต่ตอนรับจอง'
    ทุก interval_min นาที ภายใน RESV_NAG_MAX_HOURS ชม.แรกนับจากตอนสร้าง (กันตื๊อไม่จบ/ข้ามวัน)
    → จองล่วงหน้าจึงถูกคอนเฟิร์มตั้งแต่รับจอง แล้วยังโผล่ในสรุปรอบเช้าวันงานอีกครั้ง"""
    notify = r.get("notify_group_id")
    if not notify or notify in IGNORE_GROUPS or _group_left(notify):
        return False
    try:
        last_dt    = datetime.strptime(r.get("reminded_at") or r.get("created_at"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        created_dt = datetime.strptime(r.get("created_at"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except Exception:
        return False
    if RESV_NAG_MAX_HOURS and (now - created_dt).total_seconds() > RESV_NAG_MAX_HOURS * 3600:
        return False                          # เกินกรอบตื๊อจากตอนสร้าง → หยุด (ครอบทั้งจองวันนี้/ล่วงหน้า)
    if (now - last_dt).total_seconds() < interval_min * 60:
        return False
    return True


def _reservation_reminder_loop():
    """แจ้งเตือนการจองที่ยัง PENDING ซ้ำในกลุ่มที่ส่งการ์ดไว้ — ทั้งจองวันนี้และจองล่วงหน้า
    ทุก RESV_NAG_INTERVAL_MIN นาที (ทุกช่วงเวลาเท่ากัน) จนกว่าจะคอนเฟิร์ม/เกิน RESV_NAG_MAX_HOURS จากตอนรับจอง"""
    while True:
        if _in_deep_quiet():
            time.sleep(1800)   # เงียบลึก (ดึก–เช้า) → นอนยาว ไม่ query จอง ปล่อย Neon หลับ; ตื่นมาเช็คนาฬิกาใหม่
            continue
        time.sleep(RESV_LOOP_SEC)
        try:
            now      = datetime.now(TZ)
            interval = RESV_NAG_INTERVAL_MIN      # นาที (เท่ากันทุกช่วงเวลา)
            try:
                with _db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM reservations WHERE status=?", ("PENDING",)).fetchall()]
            except Exception:
                continue   # storage ยังไม่พร้อม → ข้ามรอบนี้
            for r in rows:
                if not _resv_remind_due(r, now, interval):
                    continue
                notify = r.get("notify_group_id")
                try:
                    _push(notify, [
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
threading.Thread(target=_dining_scheduler_loop, daemon=True).start()   # คิวจับคู่บิล-สลิป (1 thread แทน Timer ต่อใบ)


# ══════════════════════════════════════════════════════════════════════════════
# LINE Webhook
# ══════════════════════════════════════════════════════════════════════════════

def _slip_off(gid: str) -> bool:
    """กลุ่มนี้สั่ง 'ปิดสลิป' ไว้ไหม — ไม่ยุ่งกับสลิปเลย (ไม่เช็ค/ไม่เตือน/ไม่รายงาน/ไม่รับคำสั่งสลิป)
    ฟีเจอร์อื่น (จอง ฯลฯ) ยังทำงาน — ต่างจาก IGNORE_GROUPS ที่เมินทั้งกลุ่ม"""
    return _get_meta(f"slipoff:{gid}") == "1"


def _slip_allow():
    """allowlist 'กลุ่มรับสลิป' ที่ตั้งผ่านแชท (meta) — ถ้ามี = ใช้แทน SLIP_GROUPS env (เซ็ตเองในแอปได้ ไม่ต้องแตะ env)
    พิมพ์ 'กลุ่มนี้รับสลิป' ในกลุ่มจริง → กลุ่มนั้นเข้า allowlist, กลุ่มอื่นทั้งหมดเงียบสลิปทันที"""
    raw = _get_meta("slip_allow") or ""
    return [g for g in raw.split(",") if g]


def _slip_enabled(group_id: str) -> bool:
    """เช็คสลิป+เตือน+รายงาน เฉพาะกลุ่มรับสลิป และไม่ได้สั่ง 'ปิดสลิป'
    ลำดับ: ถ้ามี allowlist ตั้งผ่านแชท → ใช้ตัวนั้น; ไม่งั้นใช้ env SLIP_GROUPS (ว่าง=ทุกกลุ่ม)"""
    allow = _slip_allow()
    in_scope = (group_id in allow) if allow else ((not SLIP_GROUPS) or (group_id in SLIP_GROUPS))
    return in_scope and not _slip_off(group_id)


def _channel_secret_for(idx: int):
    """channel secret ของ OA ตัวที่ idx (0=หลัก/เอด, 1=OA2, ...) — ใช้ตรวจ signature ของ webhook ช่องนั้น"""
    return LINE_SECRET if idx == 0 else os.environ.get(f"LINE_CHANNEL_SECRET_{idx+1}")


def _sig_ok(secret: str, body: str, signature: str) -> bool:
    """ตรวจ X-Line-Signature = base64(HMAC-SHA256(channel_secret, body)) แบบ constant-time"""
    if not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("utf-8"), signature or "")


def _handle_secondary_callback(body: str, signature: str) -> bool:
    """webhook ของ OA สำรอง (OA2..) — รองรับเฉพาะคำสั่ง 'groupid' เพื่อใช้ตอนตั้งค่า OA_ROUTE
    OA สำรองปกติเป็น push-only; เปิด webhook มาที่ /callback ชั่วคราว แล้วพิมพ์ 'groupid' ในกลุ่ม
    → OA นั้นตอบ 'group id ของตัวเอง' (ได้ไอดีที่ตรงกับ OA นั้นแม้คนละ provider). คืน True ถ้า signature ตรงช่องใดช่องหนึ่ง"""
    tried = []
    for idx in range(1, len(_push_apis)):
        secret = _channel_secret_for(idx)
        tried.append(f"OA{idx+1}({'มี secret' if secret else 'ไม่ตั้ง secret'})")
        if not _sig_ok(secret, body, signature):
            continue
        print(f"[onboard] webhook signature ตรงกับ OA{idx+1}", flush=True)
        try:
            events = json.loads(body).get("events", [])
        except Exception:
            return True
        for ev in events:
            if ev.get("type") != "message" or (ev.get("message") or {}).get("type") != "text":
                continue
            if (ev["message"].get("text") or "").strip().lower().replace(" ", "") != "groupid":
                continue
            src = ev.get("source") or {}
            gid = src.get("groupId") or src.get("roomId")
            token = ev.get("replyToken")
            if gid and token:
                try:
                    _push_apis[idx].reply_message(token, TextSendMessage(text=f"🆔 group id (OA{idx+1}):\n{gid}"))
                    print(f"[onboard] ตอบ groupid OA{idx+1} = {gid}", flush=True)
                except Exception as e:
                    print(f"[onboard] OA{idx+1} ตอบ groupid ล้มเหลว: {e}", flush=True)
        return True
    # ไม่ตรงช่องไหนเลย — บอกให้ชัดว่าเพราะไม่มีช่องสำรอง หรือ secret ไม่ตรง (ไว้ debug ตอนตั้งค่า)
    print(f"[onboard] ⚠️ webhook signature ไม่ตรงช่องหลัก(เอด)และช่องสำรองใดเลย → ตอบ 400. "
          f"ช่องสำรองที่มี token: {tried or 'ไม่มีเลย (ยังไม่ตั้ง LINE_CHANNEL_ACCESS_TOKEN_2?)'} "
          f"— ถ้ามีช่องแต่ยังไม่ตรง = LINE_CHANNEL_SECRET_N ไม่ตรง secret จริงของ channel นั้น", flush=True)
    return False


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
        return "OK"
    except InvalidSignatureError:
        # ไม่ใช่ช่องหลัก (เอด) → ลองช่องสำรอง (OA2..) เพื่อบริการ 'groupid' ตอนตั้งค่า
        if _handle_secondary_callback(body, signature):
            return "OK"
        abort(400)


_last_admin_error_ts = 0.0
_last_mem_warn_ts = 0.0

def _mem_rss_mb():
    """หน่วยความจำที่ใช้จริง (RSS) เป็น MB จาก /proc (Linux/Render) — อ่านไม่ได้คืน None"""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0   # kB → MB
    except Exception:
        return None
    return None


def _check_memory():
    """เฝ้า memory — เกิน MEM_WARN_MB แล้ว DM เตือนแอดมิน (จำกัด 1 ครั้ง/30 นาที กันสแปม)
    บอทเฝ้าตัวเอง: เตือนล่วงหน้าก่อน Render kill เพราะ OOM (จะได้อัปเกรด/ดูสาเหตุทัน)"""
    global _last_mem_warn_ts
    mb = _mem_rss_mb()
    if mb is None or mb < MEM_WARN_MB:
        return
    if time.time() - _last_mem_warn_ts < 1800:
        return
    _last_mem_warn_ts = time.time()
    print(f"[mem] ⚠️ RSS สูง {mb:.0f} MB (เกินเกณฑ์ {MEM_WARN_MB:.0f} MB)", flush=True)
    if ADMIN_USER_ID:
        try:
            _push(ADMIN_USER_ID, TextSendMessage(
                text=f"⚠️ [SYSTEM] หน่วยความจำบอทสูง: {mb:.0f} MB (ใกล้ลิมิต Render 512MB)\n"
                     "ถ้าเตือนบ่อยช่วงพีค = ควรอัปเกรด RAM (Starter→Standard) กัน restart กลางคัน"))
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
    # เลือกข้อความตามเหตุจริง — กันโทษ Gemini ผิด (เช่น 410 รูปหาย ไม่เกี่ยวกับ Gemini)
    if _is_content_gone(err):
        tip = ("📎 รูปถูกยกเลิก/หมดอายุก่อนบอทโหลดทัน (LINE 410 'content is gone') — "
               "ไม่เกี่ยวกับ Gemini; ให้ส่งรูปสลิปใหม่ หรือใช้ 'เพิ่มสลิป <ยอด>' ลงมือ")
    elif _is_gemini_credits_error(err):
        tip = ("💳 เครดิต Gemini (prepay) หมด! เติมด่วนที่ https://ai.studio/projects — "
               "บอทหยุดอ่านสลิปชั่วคราวกันยิงเปล่า, จะกลับมาอ่านเองใน ~1 นาทีหลังเติม; "
               "แนะนำเปลี่ยนเป็นจ่ายอัตโนมัติกันหมดกลางคัน")
    elif _looks_like_gemini_issue(err):
        tip = ("🤖 น่าจะเป็นโควต้า Gemini ฟรีหมดรายวัน/rate-limit ช่วงพีค → "
               "พิจารณาเปิดบิลลิ่ง Gemini (ถูกมาก) เพื่อไม่ให้ขาดช่วง")
    else:
        tip = ("อ่านสลิปไม่สำเร็จชั่วคราว — ถ้าเกิดถี่ช่วงสลิปเยอะ อาจเป็นโควต้า Gemini "
               "→ พิจารณาเปิดบิลลิ่ง Gemini")
    try:
        _push(ADMIN_USER_ID, TextSendMessage(
            text=f"🛠️ [SYSTEM] บอทอ่านสลิปไม่สำเร็จ (group={group_id})\n"
                 f"สาเหตุ: {str(err)[:250]}\n{tip}"))
    except Exception:
        pass


# จำกัดอ่านรูปพร้อมกันไม่กี่ใบ (คิวที่เหลือรอ) — กันรูปถล่ม 50-70 ใบรัวๆ ทำ memory พุ่งจน worker OOM/รีเซ็ต
# (worker ล่ม = storage รีเซ็ต = จอง/สลิปช่วงนั้นพลาด) — ค่าน้อยปลอดภัยกว่าเพราะ Render Starter แรมจำกัด
_slip_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SLIP_WORKERS", "2")),
                                thread_name_prefix="slip")
# pool 'โหลดรูป' แยกจากอ่าน Gemini — workers เยอะกว่า เพื่อคว้าไฟล์จาก LINE ตอนยังสด (กัน 410 ช่วงพีค)
# โหลดรูปเบา (แค่ดึง bytes) จึงมีหลาย worker ได้ ไม่กิน memory เท่าอ่าน AI ที่ยังจำกัดที่ _slip_pool
_dl_pool   = ThreadPoolExecutor(max_workers=int(os.environ.get("SLIP_DL_WORKERS", "6")),
                                thread_name_prefix="dl")
# จำกัด 'รูปที่ค้างใน RAM พร้อมกัน' (โหลดแล้วรออ่าน) ไม่ให้เกิน N ใบ — กัน burst ทำ memory พุ่งจน OOM/restart
# (โหลดรูปทันทีกัน 410 แลกกับต้อง buffer bytes; semaphore นี้เป็นเบรกกันบวมเกิน) ลดค่าถ้า RAM ตึง
_slip_inflight = threading.BoundedSemaphore(int(os.environ.get("SLIP_MAX_INFLIGHT", "6")))
# re-queue: สลิปที่ Gemini อ่านล้มทั้ง flash+pro (มัก 503/rate-limit ชั่วคราวช่วงพีค) → ลองใหม่แทนดรอปทันที
SLIP_RETRY_MAX   = int(os.environ.get("SLIP_RETRY_MAX", "2"))     # ลองใหม่กี่รอบ (นอกจากรอบแรก)
SLIP_RETRY_DELAY = int(os.environ.get("SLIP_RETRY_DELAY", "45"))  # หน่วงกี่วินาทีก่อนลองใหม่ (รอ burst ซา)
# กันคิว re-queue บวม RAM ตอน Gemini ล่มยาว (ทุกใบล้ม+ลองใหม่พร้อมกัน) — เกินนี้ = ยอมแพ้เลย ไม่เข้าคิว
_slip_requeue_sem = threading.BoundedSemaphore(int(os.environ.get("SLIP_RETRY_QUEUE_MAX", "8")))
# Circuit breaker 'เครดิต Gemini หมด' — เจอ 429 credits depleted → หยุดยิง Gemini ชั่วคราว (กันยิงเปล่าเปลือง/สแปม)
# ระหว่างหยุด: ข้ามการอ่าน (นับตกหล่น) แต่ปล่อย 'probe' 1 ใบทุก GEMINI_PROBE_INTERVAL วิ เช็คว่าเครดิตกลับมายัง
GEMINI_OUT_COOLDOWN   = int(os.environ.get("GEMINI_OUT_COOLDOWN", "600"))   # หยุดยิงกี่วินาทีหลังเจอเครดิตหมด
GEMINI_PROBE_INTERVAL = int(os.environ.get("GEMINI_PROBE_INTERVAL", "60"))  # ระหว่างหยุด ทดสอบทุกกี่วินาที
_gemini_out_lock  = threading.Lock()
_gemini_out_until = 0.0   # epoch; > now = อยู่ในช่วง 'เครดิตหมด' (หยุดยิง Gemini)
_gemini_last_probe = 0.0  # เวลาที่ปล่อย probe ล่าสุด


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    # ส่งเข้า 'pool โหลดรูป' (workers เยอะ) → คว้าไฟล์จาก LINE ตอนยังสด กัน 410 'content is gone' ช่วงสลิปเยอะ
    # แล้วค่อยส่งงานหนัก (อ่าน Gemini) เข้า _slip_pool ที่จำกัด concurrency กัน memory พุ่ง
    _dl_pool.submit(_download_image_event, event)


def _download_image_event(event):
    """ขั้น 'โหลดรูป' ทำทันที (concurrency สูง) — สลิปโหลดที่นี่แล้วส่ง bytes เข้าคิวอ่าน
    กลุ่มเจ้าหนี้/เมิน/ไม่เปิดสลิป → ส่งต่อให้ worker เดิมจัดการ (payable โหลดเองในนั้น)"""
    group_id = getattr(event.source, "group_id", event.source.user_id)
    if group_id in IGNORE_GROUPS or group_id in PAYABLE_GROUPS or not _slip_enabled(group_id):
        _slip_pool.submit(_process_image_event, event)
        return
    # จองสิทธิ์ 'รูปค้าง RAM' ก่อนโหลด — เต็มโควตาจะบล็อกที่นี่ (backpressure กัน memory บวมช่วง burst)
    # worker อ่านสลิป (_process_image_event) จะคืนสิทธิ์ให้เมื่อทำเสร็จ (holds_sem=True)
    _slip_inflight.acquire()
    try:
        content     = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        _slip_pool.submit(_process_image_event, event, image_bytes, None, True)
    except Exception as e:
        print(f"[error] โหลดรูปไม่สำเร็จ (webhook) group={group_id}: {e}", flush=True)
        _slip_pool.submit(_process_image_event, event, None, e, True)


def _process_image_event(event, image_bytes=None, download_err=None, holds_sem=False, attempt=1, requeue_held=False):
    """ห่อ _process_slip_image ด้วย try/finally — คืนสิทธิ์ semaphore เสมอเมื่อทำเสร็จ (ทุก path/return/error)
    holds_sem = ถือสิทธิ์ 'รูปค้าง RAM' (_slip_inflight) / requeue_held = ถือสิทธิ์คิว re-queue (_slip_requeue_sem)"""
    try:
        _process_slip_image(event, image_bytes, download_err, attempt)
    finally:
        if holds_sem:
            _slip_inflight.release()
        if requeue_held:
            _slip_requeue_sem.release()


def _process_slip_image(event, image_bytes=None, download_err=None, attempt=1):
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
    # ใช้รูปที่โหลดมาแล้วจากขั้น webhook (สด กัน 410) — โหลดพลาดตั้งแต่ตอนนั้น = เงียบในกลุ่ม เตือนเฉพาะแอดมิน
    if download_err is not None:
        record_image_miss(group_id, "error")
        notify_admin_error(group_id, download_err)
        return
    if image_bytes is None:
        # fallback: ยังไม่มี bytes (เช่นถูกเรียกตรง ๆ ไม่ผ่านขั้นโหลด) → โหลดเองที่นี่
        try:
            content     = line_bot_api.get_message_content(event.message.id)
            image_bytes = b"".join(chunk for chunk in content.iter_content())
        except Exception as e:
            print(f"[error] โหลดรูปไม่สำเร็จ group={group_id}: {e}", flush=True)
            record_image_miss(group_id, "error")
            notify_admin_error(group_id, e)
            return

    # Circuit breaker 'เครดิต Gemini หมด' — ระหว่างหยุด: ข้ามการอ่าน (กันยิงเปล่า/สแปม) ยกเว้นปล่อย probe ทดสอบเป็นระยะ
    global _gemini_out_until, _gemini_last_probe
    _now = time.time()
    _probe = False
    with _gemini_out_lock:
        _in_outage = _gemini_out_until > _now
        if _in_outage and (_now - _gemini_last_probe >= GEMINI_PROBE_INTERVAL):
            _gemini_last_probe = _now
            _probe = True   # ปล่อยใบนี้ทดสอบว่าเครดิตกลับมายัง
    if _in_outage and not _probe:
        print(f"[slip] ข้าม Gemini: ช่วงเครดิตหมด (หยุดยิงชั่วคราว) group={group_id}", flush=True)
        record_image_miss(group_id, "error")
        notify_admin_error(group_id, "429 RESOURCE_EXHAUSTED credits depleted (บอทหยุดอ่านชั่วคราว รอเติมเครดิต)")
        return

    # อ่านสลิป: รอบแรก flash; ถ้า 'พัง/ไม่ใช่สลิป/ยอด<=0' ลองซ้ำด้วย pro (กู้ทั้งใบอ่านยาก + กรณี Gemini ล้มชั่วคราว)
    # dining=True เฉพาะกลุ่ม DINING_GROUPS → ใส่คำสั่งอ่านบิลใน prompt เฉพาะกลุ่มนั้น (กลุ่มอื่นได้ prompt เดิมเป๊ะ)
    _dining = _dining_enabled(group_id)
    info = None
    last_err = None   # เก็บ error จริงจาก Gemini (ไว้เตือนแอดมินตอนยอมแพ้ ให้รู้เหตุจริง 503/429/timeout)
    try:
        info = extract_slip_info(image_bytes, dining=_dining)
    except Exception as e:
        last_err = e
        print(f"[slip] flash อ่านพลาด group={group_id}: {e}", flush=True)
    # อ่านซ้ำด้วย pro เมื่อรอบแรก 'พัง/ไม่ใช่สลิป/ยอด<=0' — รวมถึงกรณีกลุ่ม dining ที่ flash ตัดสินว่าเป็น 'บิล'
    # (อาจเป็นรูป 'บิล+สลิปคู่กัน' ที่ flash เห็นแต่บิล อ่านสลิปบนจอไม่ออก) → ให้ pro ลองหาสลิปอีกรอบ กันยอดสลิปหาย
    if info is None or not info.get("is_slip", True) or float(info.get("amount") or 0) <= 0:
        try:
            info_retry = extract_slip_info(image_bytes, retry=True, dining=_dining)
            if info is None or info_retry.get("is_slip") or float(info_retry.get("amount") or 0) > 0:
                info = info_retry
                print(f"[slip] retry กู้สลิปได้ group={group_id}", flush=True)
        except Exception as e:
            last_err = e
            print(f"[slip] retry อ่านซ้ำพลาด group={group_id}: {e}", flush=True)

    # อ่านไม่สำเร็จทั้ง flash+pro (มักเพราะ Gemini ล้มชั่วคราวช่วงพีค) → ลองใหม่ (re-queue) แทนดรอปทันที
    if info is None:
        print(f"[slip] อ่านไม่สำเร็จทั้ง flash+pro group={group_id} (รอบ {attempt}): {last_err}", flush=True)
        # เครดิต Gemini หมด → เปิด breaker (หยุดยิงชั่วคราว) + เตือนเติมด่วน; ไม่ re-queue (ยิงไปก็ 429 เปล่า)
        if _is_gemini_credits_error(last_err):
            with _gemini_out_lock:
                _gemini_out_until  = time.time() + GEMINI_OUT_COOLDOWN
                _gemini_last_probe = time.time()   # เริ่มนับ probe ใหม่ตอน arm (กันยิงซ้ำทันที รอ 1 รอบก่อนทดสอบ)
            print(f"[slip] 💳 เครดิต Gemini หมด → หยุดยิง {GEMINI_OUT_COOLDOWN}s group={group_id}", flush=True)
            record_image_miss(group_id, "error")
            notify_admin_error(group_id, last_err)
            return
        # ยังไม่หมดโควตาลองใหม่ + มีรูป + คิว re-queue ไม่เต็ม → เข้าคิวลองใหม่หลังหน่วง (รอ burst/Gemini ซา)
        if attempt <= SLIP_RETRY_MAX and image_bytes is not None and _slip_requeue_sem.acquire(blocking=False):
            print(f"[slip] เข้าคิวลองใหม่ครั้งที่ {attempt+1}/{SLIP_RETRY_MAX+1} ใน {SLIP_RETRY_DELAY}s group={group_id}", flush=True)
            threading.Timer(SLIP_RETRY_DELAY, _slip_pool.submit,
                            args=(_process_image_event, event, image_bytes, None, False, attempt + 1, True)).start()
            return
        # หมดโควตาลองใหม่ (หรือคิวเต็ม) → ยอมแพ้: นับตกหล่น + เตือนแอดมินพร้อม error 'จริง' จาก Gemini
        print(f"[slip] ยอมแพ้หลังลอง {attempt} รอบ group={group_id}", flush=True)
        record_image_miss(group_id, "error")
        notify_admin_error(group_id, last_err or f"extract_slip_info ล้มทั้ง flash+pro ({attempt} รอบ)")
        return

    # อ่านสำเร็จ — ถ้าใบนี้เป็น probe ช่วงเครดิตหมด แปลว่าเครดิตกลับมาแล้ว → ปลด breaker กลับมาอ่านปกติ
    if _probe:
        with _gemini_out_lock:
            _gemini_out_until = 0.0
        print(f"[slip] ✅ เครดิต Gemini กลับมา — เปิดอ่านต่อ group={group_id}", flush=True)

    # ใบรูดบัตรเครดิต/เดบิต (EDC settlement) = จ่ายด้วยบัตร ไม่ใช่สลิปโอน → ไม่นับยอดโอน (เข้า 'ข้ามไม่ใช่รายรับ' ดูที่ 'ดูที่ข้าม')
    if info.get("is_card_settlement"):
        _amt = f"{float(info.get('amount') or 0):,.2f}"
        print(f"[skip] ใบรูดบัตรเครดิต (ไม่นับยอดโอน) group={group_id} amt={_amt}", flush=True)
        record_image_miss(group_id, "notincome", detail=f"💳 บัตรเครดิต {_amt} (รูดบัตร EDC)")
        return

    # บิลร้านล้วน (ใบแจ้งรายการ) ในกลุ่ม dining → เก็บเข้าคิวรอจับคู่กับสลิป (ไม่ใช่สลิป ไม่นับตกหล่น)
    # ประเมินซ้ำจาก info สุดท้าย (เผื่อ pro รอบสองเพิ่งอ่านออกว่าเป็นบิล)
    if (_dining_enabled(group_id) and info.get("is_bill") and not info.get("is_slip", False)
            and float(info.get("bill_total") or 0) > 0):
        try:
            if not _dining_bill_seen(group_id, msg_id):
                _dining_save_bill(group_id, info["bill_total"], info.get("bill_table"), msg_id)
                print(f"[dining] รับบิลโต๊ะ {info.get('bill_table')} = {info.get('bill_total')} group={group_id}", flush=True)
        except Exception as e:
            print(f"[dining] save bill error group={group_id}: {e}", flush=True)
        return

    try:
        # กันพลาดสำคัญ: ถ้า AI อ่าน 'จำนวนเงิน' ได้ ให้ถือเป็นสลิปเสมอ แม้ is_slip จะตอบ false (AI ขัดแย้งในตัว)
        if not info.get("is_slip", True) and float(info.get("amount") or 0) > 0:
            info["is_slip"] = True
            print(f"[slip] is_slip=false แต่มียอด {info.get('amount')} → ถือเป็นสลิป group={group_id}", flush=True)

        # ยังไม่ใช่สลิปหลังอ่านซ้ำ → นับตกหล่น (เงียบ เว้นแต่เปิด SLIP_WARN_UNREAD) — ไม่เด้ง error กวน
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
            _amt = f"{float(info.get('amount') or 0):,.2f}"
            _dest = " / ".join(p for p in (info.get("receiver"), info.get("receiver_account"), info.get("bank")) if p) or "?"
            record_image_miss(group_id, "notincome", detail=f"{_amt} → {_dest}")
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
            dup_type, prev_amount, prev = find_duplicate(group_id, info)
            verdict  = build_verdict(info, promptpay, dup_type, prev_amount, prev)
            info["_slip_id"] = save_slip(group_id, info, verdict["status"], message_id=msg_id)

        # สลิปผ่าน (PASS) → เงียบไว้ ไม่รกแชท (ยังบันทึกไว้ ดูรวมได้ที่ "สรุป")
        # เตือนในกรุ๊ปเฉพาะที่มีปัญหา (WARN/FAIL) — ตอบทันทีด้วย reply
        if verdict["status"] != "PASS":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=verdict["group_msg"]))
        if verdict["admin_msg"] and ADMIN_USER_ID:
            _push(ADMIN_USER_ID, TextSendMessage(text=verdict["admin_msg"]))

        # กระทบบิล-สลิป (เฉพาะ DINING_GROUPS): หน่วง ~DINING_MATCH_DELAY วิ ก่อนจับคู่
        # → รอบิลที่ส่งไล่ๆ กันถูกอ่านเข้าคิวครบ กันจับคู่สลับลำดับ; โอนขาด → push เตือน+ปุ่ม (reply token หมดอายุแล้ว)
        if _dining:
            _dining_schedule(group_id, info)   # เข้าคิวให้ scheduler เดียวจับคู่ (ไม่สร้าง thread ต่อสลิป)
    except Exception as e:
        # ประมวลผล/บันทึกพลาด (หลังอ่านสลิปได้แล้ว) → เงียบในกลุ่ม เตือนเฉพาะแอดมิน (ไม่เด้งกวน)
        print(f"[error] handle_image group={group_id}: {e}", flush=True)
        record_image_miss(group_id, "error")
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


def delete_slip_by_index(event, group_id, n, slip_date: str = None):
    d = slip_date or _today_iso()
    when = "วันนี้" if d == _today_iso() else f"วันที่ {d}"
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, sender, amount FROM slips WHERE group_id=? AND slip_date=? ORDER BY id",
            (group_id, d)
        ).fetchall()
    if n < 1 or n > len(rows):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"❌ ไม่มีรายการที่ {n} ({when}มี {len(rows)} รายการ)\nพิมพ์ 'สรุป {d}' ดูเลขรายการก่อน"))
        return
    row = rows[n - 1]
    with _db() as conn:
        conn.execute("DELETE FROM slips WHERE id=?", (row["id"],))
        conn.commit()
    amt = f"{float(row['amount'] or 0):,.2f}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"🗑️ ลบรายการที่ {n} ({when}) แล้ว: {row['sender'] or '?'} | {amt} บาท\n"
             "─────────────────\n" + build_daily_report(group_id, d)))


def add_manual_slip(event, group_id, amount: float, note: str = None, slip_date: str = None):
    """กรอกสลิปด้วยมือ (เมื่อบอทอ่านรูปไม่ออก เช่น ถ่ายจอเบลอ/มืด/มี noise) เพื่อให้ยอดตรง
    บันทึกเป็นสลิป PASS ป้ายชื่อ '✍️ กรอกมือ' ให้เห็นชัดว่าเป็นการเติมเอง ไม่ใช่ AI อ่าน
    slip_date=None → ลงวันนี้; ระบุ ISO ได้ (เช่นเคลียร์ยอดหลังเที่ยงคืน ให้ลงเป็นเมื่อวาน)"""
    sender = f"✍️ กรอกมือ{(' ' + note) if note else ''}"
    info = {"sender": sender, "amount": amount}
    save_slip(group_id, info, "PASS", slip_date=slip_date)
    d = slip_date or _today_iso()
    date_note = "" if slip_date is None else f" [ลงวันที่ {slip_date}]"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"✍️ เพิ่มสลิปด้วยมือแล้ว: {amount:,.2f} บาท{date_note}"
             f"{(' (' + note + ')') if note else ''}\n"
             "─────────────────\n" + build_daily_report(group_id, d)))


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
            "  (เขียนวันที่กำกับบนสลิป เช่น '6/6' → บอทตัดยอดค้างวันนั้นให้: ครบ=ลบบรรทัด, บางส่วน=ลดยอด)",
            "• บิล 3500 → บันทึกบิลด้วยตัวเลข (กรณีไม่มีรูป)",
            "• บิล 6/6 24153 → ลงบิลย้อนวันที่ (วัน/เดือน ยอด)",
            "• ตั้งยอดยกมา 12000 → ตั้งยอดค้างเก่า (ครั้งเดียวตอนเริ่ม)",
            "• สรุปหนี้ → ดูยอดค้างวันนี้",
            "• สรุปหนี้ 2026-06-09 → ดูย้อนหลัง (ตามวันที่)",
            "• รายการบิล → ดูบิลทั้งหมด + ที่มา (เช็กว่ามาจากไหน)",
            "• จัดยอดใหม่ → จับคู่จ่าย↔บิลใหม่ (ยอดค้างคงเดิม, เพี้ยนเมื่อไหร่ยกเลิกเอง)",
            "• ลบบิลล่าสุด / ลบจ่ายล่าสุด → แก้กรณีบันทึกผิด",
            "• ลบวันที่ 6/6 → ลบทุกบิล/จ่ายของวันนั้น",
            "• ล้างบัญชีหนี้ → ล้างทั้งหมด เริ่มนับใหม่ (มีปุ่มยืนยัน)",
            "• ทดสอบรายงานหนี้ → ส่งสรุปหนี้เดี๋ยวนี้ (เช็คการส่ง/redirect)",
            "⏰ บอทเด้งสรุปหนี้ให้เองทุกครั้งที่มี 'การจ่าย' (บิลซื้อไม่เด้ง)",
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
            "• เพิ่มสลิป 114 → กรอกยอดเองเมื่อบอทอ่านรูปไม่ออก",
            "  (ย้อนวัน: เพิ่มสลิปเมื่อวาน 114 / เพิ่มสลิป 6/6 114)",
            "• ลบล่าสุด / ลบ 3 → ลบสลิป (เลขดูจาก 'สรุป')",
            "• ลบ 29/6 3 / ลบเมื่อวาน 3 → ลบสลิปย้อนวัน (เลขดูจาก 'สรุป 2026-06-29')",
            "• ล้างวันนี้ → ล้างสลิปวันนี้ทั้งหมด (มีปุ่มยืนยัน)",
            "",
        ]
    if _dining_enabled(group_id):
        lines += [
            "🧾 กระทบบิล-สลิป (โอนขาด/เกิน)",
            "• ส่งรูปบิลร้าน + สลิปโอน → บอทเทียบยอดให้",
            "  (รูปเดียวกัน=แม่นสุด / แยกรูปก็ได้ จับคู่ตามเวลา)",
            "• โอนขาด = เตือน + ปุ่มเก็บส่วนต่าง | โอนเกิน = เตือน + ปุ่มยืนยัน",
            "  (โอนเกิน เลือก: ✅ เกินจริง→เก็บทริป / 🔧 บอทอ่านผิด→ใช้ยอดบิล)",
            "• เก็บส่วนต่างครบ → กดปุ่ม ✅ เก็บครบแล้ว ใต้ข้อความเตือน (ตัดยอดโต๊ะนั้น)",
            "• ยอดสะสม → ดูยอด + โต๊ะที่ยังไม่เคลียร์ (มีปุ่มปิดยอดทริป)",
            "• เก็บครบแล้ว → ปิดยอดทั้งทริป รีเซ็ตเป็น 0 (ใช้ตอนสิ้นวัน)",
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
        "• ปิดสลิป / เปิดสลิป → ปิด/เปิดระบบสลิปทั้งกลุ่ม (ปิดรายงาน = หยุดแค่รายงานสรุป)",
        "• กลุ่มนี้รับสลิป → ตั้งให้เฉพาะกลุ่มนี้รับสลิป กลุ่มอื่นเงียบหมด (ล้างกลุ่มสลิป = ยกเลิก)",
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
            "• เขียนวันที่กำกับบนสลิป (เช่น '6/6') → บอทตัดยอดค้างของวันนั้นออก (จ่ายครบ=ลบบรรทัด, บางส่วน=ลดยอด); ไม่เขียนก็ได้ บอทลองจับจากยอดที่ตรง\n"
            "• บอทตอบยอดค้างสะสมล่าสุดให้ทุกครั้ง\n\n"
            "🚀 เริ่มใช้ครั้งแรก\n"
            "• พิมพ์ 'ตั้งยอดยกมา 12000' ใส่ยอดค้างเก่าก้อนเดียว\n"
            "• หรือวางบล็อก 'ค้าง' (ยอดค้างยกมารายวัน) → บอทลงเป็นบิลรายวัน กระจายทุกบรรทัดในรายงาน:\n"
            "   ค้าง / 6/6=24153 (ค้าง 14153) / 13/6=8074\n"
            "   (มีวงเล็บใช้เลขในวงเล็บ; วางบล็อกใหม่ = แทนที่ของเดิม)\n\n"
            "📊 ดูยอด\n"
            "• สรุปหนี้ → ยอดค้างวันนี้ (ยกเข้า + บิล − จ่าย = ค้างสะสม)\n"
            "• สรุปหนี้ 2026-06-09 → ดูย้อนหลังตามวันที่\n\n"
            "🧹 แก้ที่ผิด\n"
            "• ลบบิลล่าสุด / ลบจ่ายล่าสุด\n"
            "• ล้างบัญชีหนี้ → ล้างทั้งหมด เริ่มนับใหม่ (มีปุ่มยืนยัน)\n\n"
            "⏰ บอทเด้งสรุปหนี้ให้เองทุกครั้งที่มี 'การจ่าย' (บิลซื้อไม่เด้ง; บรรทัดจ่ายครบโชว์รอบนั้นแล้วรอบถัดไปหาย)\n"
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
            "• เพิ่มสลิป 114 → กรอกยอดเอง (ใช้ตอนบอทอ่านรูปไม่ออก เช่น ถ่ายจอเบลอ/มืด) ใส่โน๊ตได้: เพิ่มสลิป 114 โต๊ะ5\n"
            "   ย้อนวัน (กรณีเคลียร์ยอดหลังเที่ยงคืน): เพิ่มสลิปเมื่อวาน 114 หรือ เพิ่มสลิป 6/6 114\n"
            "• ลบล่าสุด / ลบ 3 → ลบสลิป (เลขดูจาก 'สรุป')\n"
            "• ลบ 29/6 3 / ลบเมื่อวาน 3 → ลบสลิปย้อนวัน\n"
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
            f"⏰ บอทส่งรายงานจองเอง 2 รอบ: จองล่วงหน้า {RESV_ADVANCE_SUMMARY_HOUR:02d}:00 | จองวันนี้ {RESV_TODAY_SUMMARY_HOUR:02d}:00"
        )
    blocks.append("\nℹ️ help → เมนูคำสั่ง | groupid → ดูข้อมูลกลุ่ม")
    return "\n".join(blocks)


@handler.add(LeaveEvent)
def handle_leave(event):
    """บอทถูกเตะออก/ออกจากกลุ่ม → มาร์คเลิกส่งรายงาน-สรุปกลุ่มนั้น (กันค้าง retry/สแปม)"""
    gid = getattr(event.source, "group_id", None) or getattr(event.source, "room_id", None)
    if gid:
        _mark_group_left(gid)


@handler.add(UnsendEvent)
def handle_unsend(event):
    """มีคน 'ยกเลิกข้อความ (unsend)' รูปในกลุ่ม → ถอดสลิป/บิลที่บันทึกจากรูปนั้นออก
    กันเงินผี: ผู้ส่งเรียกคืนสลิปหลังบอทนับไปแล้ว (รูปหายจากแชท แต่ยอดยังค้าง)"""
    gid = getattr(event.source, "group_id", None) or getattr(event.source, "room_id", None)
    mid = getattr(getattr(event, "unsend", None), "message_id", None)
    if not gid or not mid:
        return
    removed_amt = None
    try:
        with _db() as conn:
            srow = conn.execute("SELECT id, amount FROM slips WHERE group_id=? AND message_id=?",
                                (gid, mid)).fetchone()
            if srow:
                conn.execute("DELETE FROM slips WHERE id=?", (srow["id"],))
                removed_amt = float(srow["amount"] or 0)
            conn.execute("DELETE FROM dining_bills WHERE group_id=? AND message_id=?", (gid, mid))
            conn.commit()
    except Exception as e:
        print(f"[unsend] cleanup error group={gid} msg={mid}: {e}", flush=True)
        return
    if removed_amt is not None:
        print(f"[unsend] ถอดสลิปที่ถูกยกเลิก {removed_amt:,.2f} group={gid}", flush=True)
        try:
            _push(gid, TextSendMessage(
                text=f"↩️ มีการยกเลิกรูปสลิป — ถอดยอด {removed_amt:,.2f} บาท ออกจากรายรับแล้ว (กันนับเงินผี)"))
        except Exception:
            pass


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
            "🧾 กระทบบิล-สลิป (โอนขาด/เกิน): " + ("✅ เปิด" if _dining_enabled(group_id) else "❌ ปิด (ต้องเพิ่มใน DINING_GROUPS)"),
        ]
        if group_id in REPORT_REDIRECT:
            roles.append(f"📤 รายงานส่งต่อไปกลุ่ม: {REPORT_REDIRECT[group_id]}")
        if group_id in REPORT_REDIRECT.values():
            roles.append("📥 กลุ่มนี้เป็นปลายทางรับรายงานจากกลุ่มอื่น")
        if group_id in PAYABLE_MIRROR:
            roles.append(f"🪞 บัญชีหนี้แบบ mirror: กลุ่มนี้=กลุ่มทำงาน (ส่งบิล/สลิป + ยืนยันรายตัวที่นี่); สรุปรายวันไปที่ {PAYABLE_MIRROR[group_id]}")
        if group_id in _PAYABLE_PRIMARY_OF:
            roles.append("🪞 บัญชีหนี้แบบ mirror: กลุ่มนี้=กลุ่มดูสรุป (รับเฉพาะสรุปรายวันของบัญชีร่วม ไม่เด้งทุกบิล/สลิป)")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"🆔 Group ID:\n{group_id}\n─────────────────\n" + "\n".join(roles)))
        return
    # เช็คโควต้า push ที่ใช้ไปเดือนนี้ (แยกราย OA) — ไว้ดูว่าใกล้เต็ม/สลับ OA แล้วหรือยัง
    if text.lower() in ("โควต้า", "quota", "โควตา", "push"):
        mon = datetime.now(TZ).strftime("%Y-%m")
        lines = [f"📈 โควต้า push เดือน {mon} (ลิมิต {PUSH_FREE_LIMIT}/OA)"]
        total = 0
        for i in range(len(_push_apis)):
            used = _push_count(i)                 # บอทนับ (เฉพาะที่บอท push)
            real = _line_real_usage(i)            # LINE นับจริง (รวมบรอดแคสต์/ข้อความอื่น)
            shown = real if real is not None else used
            total += shown
            bar = "🔴 เต็ม" if shown >= PUSH_FREE_LIMIT else ("🟡" if shown >= PUSH_FREE_LIMIT * 0.9 else "🟢")
            if real is not None and real != used:
                lines.append(f"{bar} OA{i+1}: {real}/{PUSH_FREE_LIMIT}  (LINE จริง · บอทนับ {used})")
            else:
                lines.append(f"{bar} OA{i+1}: {shown}/{PUSH_FREE_LIMIT}")
        lines.append(f"─────────────────\nรวมทั้งหมด {total} ข้อความ (ยอดจริงจาก LINE)")
        if _oa_route:
            # แผน B: แต่ละกลุ่มผูก OA ของตัวเอง (ไม่มี 'OA ที่ใช้อยู่' ตัวเดียวแบบ failover เก่า)
            here = _oa_route.get(group_id)
            if here:
                _seq = "→".join("OA" + str(i + 1) for i in here)
                lines.append(f"📍 กลุ่มนี้ push ผ่าน {_seq}" + (" (สลับอัตโนมัติเมื่อเต็ม)" if len(here) > 1 else ""))
            else:
                lines.append("📍 กลุ่มนี้ push ผ่าน OA1")
            lines.append("โหมด: แยกกลุ่มคนละ OA (แผน B)" + (" + สลับ OA อัตโนมัติเมื่อเต็ม" if here and len(here) > 1 else ""))
        else:
            active = next((i for i in range(len(_push_apis)) if _push_count(i) < PUSH_FREE_LIMIT), len(_push_apis) - 1)
            lines.append(f"กำลังใช้: OA{active+1}")
        lines.append("ℹ️ นับแบบ LINE: push เข้ากลุ่ม = จำนวนสมาชิก (reply ฟรี ไม่นับ) — เทียบเลขจริงที่ LINE OA Manager")
        if len(_push_apis) == 1:
            lines.append("⚠️ ยังมี OA เดียว — ตั้ง env LINE_CHANNEL_ACCESS_TOKEN_2 เพื่อเปิดตัวสำรอง")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines)))
        return
    # ทดสอบ/ส่งซ้ำรายงานสลิปเดี๋ยวนี้ (ไม่ต้องรอ 00:30) — ยิงไปปลายทางจริง (REPORT_REDIRECT + OA_ROUTE)
    #   'ทดสอบรายงาน' = วันนี้; 'ทดสอบรายงาน 2026-07-10' = ส่งซ้ำของวันที่ระบุ (กู้รายงานที่หายรอบ 00:30)
    # ⚠️ ต้อง match แบบเป๊ะ + เว้นกลุ่มหนี้ — ไม่งั้น 'ทดสอบรายงานหนี้' (คำสั่งบัญชีหนี้) จะโดนดักผิด
    _low_tr = text.lower().strip()
    if (group_id not in PAYABLE_GROUPS
            and (_low_tr in ("ทดสอบรายงาน", "ทดสอบรายงานสลิป", "force report")
                 or _low_tr.startswith("ทดสอบรายงาน "))):
        d = datetime.now(TZ).date().isoformat()
        for p in text.split()[1:]:
            try:
                datetime.strptime(p, "%Y-%m-%d"); d = p; break
            except ValueError:
                continue
        dest = _report_dest(group_id)
        try:
            _push(dest, TextSendMessage(text=build_daily_report(group_id, d)))
            note = f" (redirect จากกลุ่มนี้ไป {dest})" if dest != group_id else ""
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ ยิงรายงานสลิป ({d}) เข้าปลายทางแล้ว{note}\nไปเช็คว่าเข้ากลุ่มปลายทางไหม"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"❌ ส่งรายงานไม่สำเร็จ: {e}\n(ปลายทาง {dest} — เช็คว่า OA ของกลุ่มนั้นอยู่ในกลุ่ม + token ถูก)"))
        return
    # กลุ่มเจ้าหนี้การค้า → จัดการบัญชีหนี้ (บิล/จ่าย/สรุป) แล้วจบ ไม่ทำระบบสลิป/จองปกติ
    if group_id in PAYABLE_GROUPS:
        handle_payable_text(event, text, group_id)
        return
    # สรุปการจอง (วันนี้ + ล่วงหน้า) ของกลุ่มนี้ — ใช้ได้ทุกกลุ่ม
    if text.lower() in ("สรุปจอง", "สรุปการจอง", "รายการจอง", "จองวันนี้"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=build_resv_summary(group_id, "📋 สรุปการจอง (วันนี้ + ล่วงหน้า)", upcoming=True, match_origin=True)))
        return
    # ล้างข้อมูลทั้งหมดของกลุ่มนี้ (สลิป+จอง ทุกวัน) — เตรียมเลิกใช้/รีเซ็ตกลุ่ม (มีปุ่มยืนยัน)
    if text.lower() in ("ล้างทั้งหมด", "ล้างกลุ่มนี้", "ล้างข้อมูลทั้งหมด", "รีเซ็ตทั้งหมด"):
        send_reset_all_confirm(event, group_id)
        return
    # เปิด/ปิด 'ระบบสลิปทั้งหมด' ของกลุ่มนี้ (ไม่เช็ค/ไม่เตือน/ไม่รายงาน) — วางนอกบล็อก _slip_enabled เพื่อให้ 'เปิดสลิป' ได้แม้ปิดอยู่
    if text in ("ปิดสลิป", "ปิดเช็คสลิป", "ไม่เช็คสลิป", "ปิดระบบสลิป"):
        _set_meta(f"slipoff:{group_id}", "1")
        _del_meta(f"noreport:{group_id}")   # ปิดสลิปครอบคลุมรายงานอยู่แล้ว
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="🛑 ปิดระบบสลิปของกลุ่มนี้แล้ว — บอทจะไม่เช็ค/ไม่เตือน/ไม่รายงานสลิปในกลุ่มนี้\n"
                 "(ฟีเจอร์อื่น เช่น การจอง ยังใช้ได้ปกติ)\n"
                 "• เปิดใหม่พิมพ์ 'เปิดสลิป'"))
        return
    if text in ("เปิดสลิป", "เปิดเช็คสลิป", "เปิดระบบสลิป"):
        _del_meta(f"slipoff:{group_id}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="✅ เปิดระบบสลิปของกลุ่มนี้แล้ว — กลับมาเช็ค/เตือน/รายงานสลิปตามปกติ"))
        return
    # ตั้ง 'เฉพาะกลุ่มนี้' เป็นกลุ่มรับสลิป → กลุ่มอื่นทั้งหมดเงียบสลิปทันที (พิมพ์ครั้งเดียวในกลุ่มจริง)
    if text in ("กลุ่มนี้รับสลิป", "ตั้งกลุ่มสลิป", "เปิดเฉพาะกลุ่มนี้", "เฉพาะกลุ่มนี้รับสลิป", "ตั้งกลุ่มนี้เป็นกลุ่มสลิป"):
        allow = _slip_allow()
        if group_id not in allow:
            allow.append(group_id)
        _set_meta("slip_allow", ",".join(allow))
        _del_meta(f"slipoff:{group_id}")   # กลุ่มนี้ต้องเปิดสลิปแน่ๆ
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"✅ ตั้งกลุ่มนี้เป็น 'กลุ่มรับสลิป' แล้ว\n"
                 f"กลุ่มรับสลิปตอนนี้: {len(allow)} กลุ่ม\n"
                 "🔕 ทุกกลุ่มอื่นที่ไม่ได้ตั้ง = ไม่เช็ก/ไม่รายงานสลิปทั้งหมด (รวมกลุ่มใหม่ในอนาคต)\n"
                 "• ถ้าตั้งผิด พิมพ์ 'ล้างกลุ่มสลิป' เพื่อกลับค่าเริ่มต้น"))
        return
    if text in ("ล้างกลุ่มสลิป", "รีเซ็ตกลุ่มสลิป", "ยกเลิกกลุ่มสลิป"):
        _del_meta("slip_allow")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="↩️ ล้างรายการ 'กลุ่มรับสลิป' แล้ว — กลับไปใช้ค่าตั้งต้น (ตาม env SLIP_GROUPS)"))
        return
    # คำสั่งเกี่ยวกับสลิป (สรุป/ลบ/ล้าง) → เฉพาะกลุ่มที่เปิดเช็คสลิปเท่านั้น
    if _slip_enabled(group_id):
        # กระทบบิล-สลิป (เฉพาะ DINING_GROUPS): ยืนยันเก็บครบ=รีเซ็ตยอด / ดูยอดสะสม
        if _dining_enabled(group_id):
            if text in ("เก็บครบแล้ว", "เก็บเงินครบ", "เก็บเงินครบแล้ว", "ปิดยอด", "เคลียร์ยอด", "เคลียยอด", "รีเซ็ตยอด"):
                _dining_reset(event, group_id)
                return
            if text in ("ยอดสะสม", "ยอดทริป", "สรุปบิล", "ยอดบิล", "บิลสลิป", "กระทบยอด"):
                # สรุปยอด + การ์ดปุ่ม 'เก็บครบแล้ว' (พนักงานดูยอดแล้วกดปิดได้เลย)
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=build_dining_summary(group_id)),
                    _dining_confirm_card(),
                ])
                return
        if text.lower() in ("สรุป", "รายงาน", "report", "summary"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_daily_report(group_id)))
            return

        # เปิด/ปิด 'รายงานสรุปประจำวัน' ของกลุ่มนี้ (ยังเช็คสลิป + ใช้คำสั่ง 'สรุป' ได้ปกติ)
        if text in ("ปิดรายงาน", "ปิดรายงานสลิป", "งดรายงาน", "ไม่เอารายงาน", "ปิดสรุป"):
            _set_meta(f"noreport:{group_id}", "1")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔕 ปิดรายงานสรุปประจำวัน (00:30) ของกลุ่มนี้แล้ว\n"
                     "• ยังเช็คสลิป/เตือนปกติ + พิมพ์ 'สรุป' ดูเองได้\n"
                     "• เปิดใหม่พิมพ์ 'เปิดรายงาน'"))
            return
        if text in ("เปิดรายงาน", "เปิดรายงานสลิป", "เอารายงาน", "เปิดสรุป"):
            _del_meta(f"noreport:{group_id}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔔 เปิดรายงานสรุปประจำวัน (00:30) ของกลุ่มนี้แล้ว"))
            return

        # ดูใบที่ถูกข้าม 'ไม่ใช่รายรับ' (โชว์ยอด+ปลายทาง) — เช็กว่าโดนกรองทิ้งเป็นเงินร้านจริงไหม
        if text.lower() in ("ดูที่ข้าม", "รายการข้าม", "ใบที่ข้าม", "สรุปข้าม", "ที่ข้าม"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_skipped_list(group_id)))
            return
        if text.startswith("ดูที่ข้าม ") or text.startswith("รายการข้าม "):
            arg = text.split(" ", 1)[1].strip()
            try:
                datetime.strptime(arg, "%Y-%m-%d")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_skipped_list(group_id, arg)))
                return
            except ValueError:
                pass

        # สรุปย้อนหลังรายวัน เช่น "สรุป 2026-06-03" (เอาไว้ตรวจเทียบยอด)
        if text.startswith("สรุป ") or text.startswith("รายงาน "):
            arg = text.split(" ", 1)[1].strip()
            try:
                datetime.strptime(arg, "%Y-%m-%d")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=build_daily_report(group_id, arg)))
                return
            except ValueError:
                pass

        # push 'รายงานเมื่อวาน' เข้า 'กลุ่มนี้' เดี๋ยวนี้ (ต่างจาก 'ทดสอบรายงาน' ที่ยิงไปปลายทาง redirect)
        # ใช้เช็คว่า push เข้ากลุ่มนี้ได้ไหม + ดูรายงานเมื่อวานซ้ำ
        if text.lower() in ("รายงานเมื่อวาน", "test report"):
            yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
            try:
                _push(group_id, TextSendMessage(text=build_daily_report(group_id, yesterday)))
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="✅ push รายงานเมื่อวานสำเร็จ — ระบบ push ใช้งานได้ปกติ"))
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"🚨 push ล้มเหลว: {e}\n(นี่คือสาเหตุที่รายงาน 00:30 ไม่เด้ง)"))
            return

        # กรอกสลิปด้วยมือ — ใช้เมื่อบอทอ่านรูปไม่ออก (ถ่ายจอเบลอ/มืด/มี noise) ให้ยอดตรง
        # รูปแบบ: "เพิ่มสลิป 114" (วันนี้) | "เพิ่มสลิปเมื่อวาน 114" | "เพิ่มสลิป 6/6 114" (ย้อนวัน) | +โน๊ตท้ายได้
        _help_add = ("✍️ พิมพ์: เพิ่มสลิป <ยอด> [โน๊ต]\n"
                     "เช่น  เพิ่มสลิป 114  |  เพิ่มสลิป 114 โต๊ะ5\n"
                     "ย้อนวัน:  เพิ่มสลิปเมื่อวาน 114  |  เพิ่มสลิป 6/6 114")
        for _kw in ("เพิ่มสลิป", "กรอกสลิป", "เติมสลิป", "บวกสลิป"):
            if text.startswith(_kw):
                rest = text[len(_kw):].strip()
                target_date = None   # None = วันนี้ (save_slip ใส่วันนี้ให้เอง)
                if rest.startswith("เมื่อวาน"):
                    target_date = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
                    rest = rest[len("เมื่อวาน"):].strip()
                else:
                    dm = re.match(r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(.*)$", rest)
                    if dm:
                        target_date = _parse_thai_date(dm.group(1))
                        if target_date is None:
                            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                                text="❌ วันที่ไม่ถูก เช่น: เพิ่มสลิป 6/6 114 (วัน/เดือน ยอด)"))
                            return
                        rest = dm.group(2).strip()
                m = re.match(r"^([\d,]+(?:\.\d+)?)\s*(.*)$", rest)
                if not m:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=_help_add))
                    return
                amt = float(m.group(1).replace(",", ""))
                if amt <= 0:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ยอดต้องมากกว่า 0"))
                    return
                add_manual_slip(event, group_id, amt, (m.group(2).strip() or None), slip_date=target_date)
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
            # ลบย้อนวัน: "ลบ 29/6 26" (วัน/เดือน เลขรายการ) | "ลบเมื่อวาน 26"
            if arg.startswith("เมื่อวาน"):
                rest = arg[len("เมื่อวาน"):].strip()
                if rest.isdigit():
                    y = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
                    delete_slip_by_index(event, group_id, int(rest), slip_date=y)
                    return
            dm = re.match(r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(\d+)$", arg)
            if dm:
                d = _parse_thai_date(dm.group(1))
                if d is None:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text="❌ วันที่ไม่ถูก เช่น: ลบ 29/6 26 (วัน/เดือน เลขรายการ)"))
                    return
                delete_slip_by_index(event, group_id, int(dm.group(2)), slip_date=d)
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
            print(f"[postback] จอง #{resv_id} กดซ้ำ — ข้ามเงียบๆ (ไม่ตอบเตือน)", flush=True)
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
    elif action == "dining_clear":
        try:
            pair_id = int(parts[1])
        except (IndexError, ValueError):
            return
        if not _postback_once(f"pb:{data}"):
            _already("⚠️ โต๊ะนี้เคลียร์ไปแล้ว ไม่ต้องกดซ้ำครับ"); return
        try:
            g = getattr(event.source, "group_id", event.source.user_id)
            if _dining_enabled(g):
                _dining_clear_pair(event, g, pair_id)   # เก็บส่วนต่างครบ → เคลียร์เฉพาะโต๊ะนั้น
        except Exception as e:
            print(f"[dining] clear button error: {e}", flush=True)
    elif action in ("dining_over_keep", "dining_over_fix"):
        try:
            pair_id = int(parts[1])
        except (IndexError, ValueError):
            return
        if not _postback_once(f"pb:{data}"):
            _already("⚠️ รายการนี้ยืนยันไปแล้ว ไม่ต้องกดซ้ำครับ"); return
        try:
            g = getattr(event.source, "group_id", event.source.user_id)
            if _dining_enabled(g):
                if action == "dining_over_keep":
                    _dining_over_keep(event, g, pair_id)   # โอนเกินจริง → เก็บเข้าทริป
                else:
                    _dining_over_fix(event, g, pair_id)    # บอทอ่านผิด → ใช้ยอดบิล
        except Exception as e:
            print(f"[dining] over button error: {e}", flush=True)
    elif action == "dining_confirm":
        if not _postback_once(f"pb:{data}"):
            _already(); return
        try:
            g = getattr(event.source, "group_id", event.source.user_id)
            if _dining_enabled(g):
                _dining_reset(event, g)   # ยืนยันเก็บเงินครบทั้งทริป → รีเซ็ตยอด
        except Exception as e:
            print(f"[dining] confirm button error: {e}", flush=True)


@app.route("/health", methods=["GET"])
def health():
    # ช่วงเงียบลึก (ดึก–เช้า) → ตอบเบาๆ ไม่แตะ DB/รายงาน เพื่อปล่อยให้ Neon หลับ ประหยัด compute
    if _in_deep_quiet():
        return {"status": "ok", "quiet": True}
    # ทุกครั้งที่ถูก ping (UptimeRobot ทุก 5 นาที) เช็คว่าถึงเวลาส่งรายงาน/สรุปจองไหม + เฝ้า memory
    try:
        _check_memory()
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
        _mb = _mem_rss_mb()
        return {"status": "ok", "slips_today": today_count, "active_groups": group_count,
                "storage": STORAGE_DESC, "memory_mb": round(_mb, 1) if _mb else None}
    except Exception as e:
        # DB ล่ม/เชื่อมไม่ได้ชั่วคราว — ตอบ 200 อยู่ (กัน UptimeRobot รีสตาร์ท) แต่บอกสถานะ
        return {"status": "db_error", "storage": f"{STORAGE_DESC} — {e}"}


@app.route("/api/slip_daily", methods=["GET"])
def api_slip_daily():
    """อ่านอย่างเดียว: คืนยอดสลิปรวมรายวัน (ไว้ให้ dashboard เทียบกับเงินโอนจาก POS)
    ความปลอดภัย (fail-closed): ต้องตั้ง env รหัส (SLIP_API_TOKEN หรือ DASHBOARD_PASSWORD) และส่ง ?token=... ให้ตรง
      — ไม่ตั้งรหัสเลย = ล็อก (403) กันยอดขายหลุดสาธารณะ (repo เป็น public)
    ขอบเขต: นับเฉพาะกลุ่มที่บอทเปิดรับสลิปจริง (allowlist ผ่านแชท หรือ SLIP_GROUPS) — ไม่ปนกลุ่มทดสอบ
    นับ verdict != 'FAIL' (PASS ผ่าน + WARN ผ่านแต่เตือน) ตัด FAIL (ปลอม/ซ้ำ) ออก
    คืน: {"ok":true,"daily":{"2026-07-01":{"amount":109000.0,"count":42}, ...}}
    """
    need = os.environ.get("SLIP_API_TOKEN") or os.environ.get("DASHBOARD_PASSWORD") or ""
    if not need:
        return {"ok": False, "error": "locked: ตั้ง env SLIP_API_TOKEN หรือ DASHBOARD_PASSWORD ก่อน"}, 403
    if request.args.get("token", "") != need:
        return {"ok": False, "error": "unauthorized"}, 401
    try:
        allowed = _slip_allow() or SLIP_GROUPS   # กลุ่มที่บอทเช็คสลิปจริง (เว้นว่าง = ทุกกลุ่ม)
        where, params = "slip_date IS NOT NULL", []
        if allowed:
            where += " AND group_id IN (%s)" % ",".join("?" * len(allowed))
            params = list(allowed)
        with _db() as conn:
            rows = conn.execute(
                "SELECT slip_date, "
                "SUM(CASE WHEN verdict!='FAIL' THEN amount ELSE 0 END) amt, "
                "COUNT(CASE WHEN verdict!='FAIL' THEN 1 END) cnt "
                f"FROM slips WHERE {where} "
                "GROUP BY slip_date ORDER BY slip_date DESC LIMIT 120", params
            ).fetchall()
        daily = {r["slip_date"]: {"amount": round(float(r["amt"] or 0), 2), "count": int(r["cnt"] or 0)}
                 for r in rows}
        return {"ok": True, "daily": daily}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# ── ย้าย DB (Neon → Supabase ฯลฯ): dump/load ทุกตารางผ่าน endpoint ตัวบอทเอง ──
# ไม่ต้องใช้ psql/pg_dump: 1) ยังชี้ DB เดิม → GET /api/db_export เซฟ JSON  2) เปลี่ยน DATABASE_URL เป็น DB ใหม่ (redeploy)
# 3) POST /api/db_import?confirm=yes แนบ JSON → ข้อมูลเข้าครบ (สคีมาสร้างอัตโนมัติตอน init)
_MIGRATE_TABLES = ["meta", "groups", "slips", "reservations",
                   "payable_bills", "payable_payments",
                   "dining_bills", "dining_pairs", "image_misses"]
# ตารางที่มีคอลัมน์ id (SERIAL) → ต้องรีเซ็ต sequence หลัง import กัน insert ใหม่ชน id เดิม
_MIGRATE_SEQ_TABLES = ["slips", "reservations", "payable_bills", "payable_payments",
                       "dining_bills", "dining_pairs", "image_misses"]

def _migrate_auth() -> bool:
    need = os.environ.get("SLIP_API_TOKEN") or os.environ.get("DASHBOARD_PASSWORD") or ""
    return bool(need) and request.args.get("token", "") == need


@app.route("/api/db_export", methods=["GET"])
def api_db_export():
    """ดัมพ์ข้อมูลทุกตารางเป็น JSON (อ่านอย่างเดียว, ต้องมี ?token=) — ใช้ตอนยังชี้ DB เดิมเพื่อเซฟไว้ย้าย"""
    if not _migrate_auth():
        return {"ok": False, "error": "unauthorized"}, 401
    try:
        dump, counts = {}, {}
        with _db() as conn:
            for t in _MIGRATE_TABLES:
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                dump[t] = [dict(r) for r in rows]
                counts[t] = len(rows)
        body = json.dumps({"ok": True, "ts": datetime.now(TZ).isoformat(),
                           "counts": counts, "tables": dump}, ensure_ascii=False, default=str)
        return app.response_class(body, mimetype="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}, 500


@app.route("/api/db_import", methods=["POST"])
def api_db_import():
    """โหลด JSON จาก /api/db_export เข้า DB ปัจจุบัน (ต้องมี ?token= + ?confirm=yes)
    ค่าเริ่มต้น mode=merge (ON CONFLICT DO NOTHING ไม่ทับของเดิม) · ?mode=replace = ล้างตารางก่อนโหลด (ระวัง!)"""
    if not _migrate_auth():
        return {"ok": False, "error": "unauthorized"}, 401
    if request.args.get("confirm") != "yes":
        return {"ok": False, "error": "ต้องส่ง ?confirm=yes เพื่อยืนยัน"}, 400
    mode = request.args.get("mode", "merge")
    try:
        body = json.loads(request.get_data(as_text=True) or "{}")
        tables = body.get("tables") or {}
        _ensure_init()   # สร้างสคีมา/คอลัมน์ให้ครบก่อน (เผื่อ DB ใหม่ยังว่าง)
        loaded = {}
        with _db() as conn:
            for t in _MIGRATE_TABLES:
                rows = tables.get(t) or []
                if mode == "replace":
                    conn.execute(f"DELETE FROM {t}")
                n = 0
                for row in rows:
                    cols = list(row.keys())
                    if not cols:
                        continue
                    ph = ",".join(["?"] * len(cols))
                    sql = f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
                    conn.execute(sql, [row[c] for c in cols])
                    n += 1
                loaded[t] = n
            if USE_PG:   # เดิน sequence ของ id ต่อจาก max(id) กัน insert ใหม่ชน
                for t in _MIGRATE_SEQ_TABLES:
                    conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                        f"GREATEST((SELECT COALESCE(MAX(id),1) FROM {t}),1))")
            conn.commit()
        return {"ok": True, "mode": mode, "loaded": loaded}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}, 500


@app.route("/api/db_migrate", methods=["GET", "POST"])
def api_db_migrate():
    """คัดลอกข้อมูลจาก DB เก่า → DB ปัจจุบัน ในคำสั่งเดียว (ไม่ต้องดาวน์โหลด JSON)
    วิธีใช้: ตั้ง DATABASE_URL = DB ใหม่ (Supabase) + MIGRATE_SOURCE_URL = DB เก่า (Neon) บน Render แล้ว redeploy
    จากนั้นเรียก GET /api/db_migrate?token=...&confirm=yes → บอทเชื่อม DB เก่า อ่านทุกตาราง แล้วเขียนเข้า DB ใหม่
    ปลอดภัย: token + confirm=yes · mode=merge (ไม่ทับ) ค่าเริ่มต้น / mode=replace = ล้างก่อน · รันซ้ำได้ (idempotent)"""
    if not _migrate_auth():
        return {"ok": False, "error": "unauthorized"}, 401
    if request.args.get("confirm") != "yes":
        return {"ok": False, "error": "ต้องส่ง ?confirm=yes เพื่อยืนยัน"}, 400
    src_url = os.environ.get("MIGRATE_SOURCE_URL", "")
    if not src_url:
        return {"ok": False, "error": "ยังไม่ตั้ง env MIGRATE_SOURCE_URL (= DATABASE_URL ของ DB เก่า)"}, 400
    if not USE_PG:
        return {"ok": False, "error": "DB ปลายทางต้องเป็น Postgres (ตั้ง DATABASE_URL ให้เป็น DB ใหม่ก่อน)"}, 400
    mode = request.args.get("mode", "merge")
    try:
        src = psycopg2.connect(src_url, connect_timeout=15)
        data = {}
        with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as scur:
            for t in _MIGRATE_TABLES:
                scur.execute(f"SELECT * FROM {t}")
                data[t] = [dict(r) for r in scur.fetchall()]
        src.close()
        _ensure_init()
        copied = {}
        # เขียน 'ทีละตาราง' (คนละ transaction) — กัน lock ค้างยาว + ถ้าตารางใหญ่ timeout ตารางอื่นยังผ่าน
        # ตั้ง statement/lock timeout เอง (Supabase default อาจสั้น) · ON CONFLICT DO NOTHING = รันซ้ำได้
        for t in _MIGRATE_TABLES:
            rows = data.get(t) or []
            with _db() as conn:
                conn.execute("SET statement_timeout = '600s'")
                conn.execute("SET lock_timeout = '25s'")
                if mode == "replace":
                    conn.execute(f"DELETE FROM {t}")
                n = 0
                for row in rows:
                    cols = list(row.keys())
                    if not cols:
                        continue
                    ph = ",".join(["?"] * len(cols))
                    conn.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                                 [row[c] for c in cols])
                    n += 1
                if t in _MIGRATE_SEQ_TABLES:
                    conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                        f"GREATEST((SELECT COALESCE(MAX(id),1) FROM {t}),1))")
                conn.commit()
            copied[t] = n
        return {"ok": True, "mode": mode, "copied": copied}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}, 500


@app.route("/api/push_owner", methods=["POST"])
def api_push_owner():
    """ให้ Apps Script (dashboard การเงิน) ส่งข้อความแจ้งเตือนเข้า LINE เจ้าของ (ADMIN_USER_ID)
    ความปลอดภัย: ต้องมี token ตรง (SLIP_API_TOKEN หรือ DASHBOARD_PASSWORD)
    body: {"message":"..."} — ใช้ LINE token เดิมของบอท (ไม่ต้องส่ง token LINE ออกไป)"""
    need = os.environ.get("SLIP_API_TOKEN") or os.environ.get("DASHBOARD_PASSWORD") or ""
    if not need or request.args.get("token", "") != need:
        return {"ok": False, "error": "unauthorized"}, 401
    if not ADMIN_USER_IDS:
        return {"ok": False, "error": "ยังไม่ตั้ง env LINE_ADMIN_USER_ID"}, 400
    try:
        body = json.loads(request.get_data(as_text=True) or "{}")
        msg = (body.get("message") or "").strip()
        if not msg:
            return {"ok": False, "error": "empty message"}, 400
        # ส่งให้เจ้าของทุกคน ผ่าน _push() → เลือก OA ตาม OA_ROUTE อัตโนมัติ (กลุ่ม management → OA2)
        # + นับโควตา/สลับ OA ตามระบบบอท · ไม่ต้องพึ่ง LINE_ADMIN_PUSH_TOKEN แล้ว
        sent, errors = 0, []
        for uid in ADMIN_USER_IDS:
            try:
                _push(uid, TextSendMessage(text=msg[:4900]))
                sent += 1
            except Exception as e:
                errors.append(f"{uid[:8]}…: {str(e)[:80]}")
        if sent == 0:
            return {"ok": False, "error": "; ".join(errors)[:180]}
        return {"ok": True, "sent": sent, "total": len(ADMIN_USER_IDS), "errors": errors}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
