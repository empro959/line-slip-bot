"""เทสต์: 'ใบแจ้งรายการ' (บิลกระดาษของร้าน) ต้องไม่ถูกนับเป็นรายรับ

ทำไมต้องมี — เคสจริง 30/08/26 (เจอตอนไล่ของค้างข้อ ⚪ #1 ใน HANDOFF §1):
  บิลร้านมีชื่อร้านอยู่บนหัวใบ → AI ใส่มาเป็น receiver → ตรง PAYEE_ACCOUNTS
  → กฎ 'มียอด = ถือเป็นสลิป' ทับคำตอบ is_slip=false ของ AI ทิ้ง → บันทึกเป็นรายรับ
  → นับซ้ำกับสลิปที่ลูกค้าโอนจริงของโต๊ะเดียวกัน 'เงียบสนิท'
  (find_duplicate ต้องมีทั้ง amount และ slip_datetime ตรงกัน — บิลไม่มีเวลาโอน จึงไม่เคยถูกจับซ้ำ)
  ตอนนั้นมี 3 ใน 4 กลุ่มรับสลิปที่ไม่ได้เปิด DINING_GROUPS = ไม่มีด่านจับบิลเลย

⚖️ เทสต์ไฟล์นี้คุม 'สองทิศ' เสมอ — ทิศที่แพงกว่าคือทิศหลัง:
  1. บิลร้าน ต้องไม่กลายเป็นรายรับ (นับเกิน)
  2. สลิปจริง ต้องไม่ถูกตัดทิ้ง (นับขาด — 19/08 เคยตัดเงินลูกค้าทิ้ง 1,017 บาทมาแล้ว)

วิธีรัน:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

# ── ต้องตั้ง env ก่อน import app (app.py อ่าน env ตอน import) ──
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy-token")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy-secret")
os.environ.setdefault("GEMINI_API_KEY", "dummy-key")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

_tmpdir = tempfile.TemporaryDirectory(prefix="line_slip_bill_test_")
_prev_cwd = os.getcwd()
os.chdir(_tmpdir.name)

import app  # noqa: E402


class _WithPayeeConfig(unittest.TestCase):
    """ตั้งบัญชีร้านที่ 'ตัวโมดูล' ไม่ใช่ที่ env

    app.py อ่าน env ตอน import และ Python import โมดูลครั้งเดียว —
    ไฟล์เทสต์อื่นที่รันก่อนจะ import app ไปแล้วด้วย env ของตัวเอง
    ตั้งผ่าน os.environ ที่นี่จึงไม่มีผล และข้อจะแดงเฉพาะตอนรันทั้งชุด (ผ่านตอนรันไฟล์เดียว)
    ค่าข้างล่างเป็นค่าสมมติ ไม่ใช่ชื่อ/เลขบัญชีจริง (repo เป็น public)"""

    def setUp(self):
        self._bak = (app.PAYEE_KEYWORDS, app.PAYEE_ACCOUNTS)
        app.PAYEE_KEYWORDS = ["ไส้ย่างซอย4", "sai yang soi 4"]
        app.PAYEE_ACCOUNTS = [("ไส้ย่างซอย4", ["ไส้ย่าง", "2481"])]

    def tearDown(self):
        app.PAYEE_KEYWORDS, app.PAYEE_ACCOUNTS = self._bak


class StoreBillNotIncome(_WithPayeeConfig):
    """ตัวชี้ขาด: _is_store_bill — ต้นฉบับเดียวที่ทั้งเส้นรับรูปและเส้นกู้สลิปใช้ร่วมกัน"""

    def test_บิลร้านล้วน_ต้องถูกจับได้(self):
        bill = {"is_slip": False, "is_bill": True, "bill_total": 1450.0,
                "bill_table": "B16", "receiver": "ไส้ย่างซอย4", "amount": 1450.0}
        self.assertTrue(app._is_store_bill(bill))

    def test_บิลร้าน_ห้ามรอดไปเป็นรายรับผ่านกฎ_มียอดถือเป็นสลิป(self):
        """กฎ 'มียอด = ถือเป็นสลิป' ต้องไม่มีโอกาสได้ทำงานกับใบที่เป็นบิล
        (ด่านบิลถูกวางไว้ 'ก่อน' กฎนั้นใน _process_image_event)"""
        bill = {"is_slip": False, "is_bill": True, "bill_total": 1450.0,
                "receiver": "ไส้ย่างซอย4", "amount": 1450.0}
        self.assertTrue(app._is_store_bill(bill), "ต้องถูกด่านบิลจับก่อน")
        # ยืนยันว่า 'ถ้าไม่มีด่านนี้' ใบนี้จะกลายเป็นรายรับจริงๆ — คือเหตุผลที่เทสต์นี้มีอยู่
        self.assertTrue(app._is_income(bill), "ถ้าหลุดด่านบิลไปได้ ใบนี้จะถูกนับเป็นรายรับ")

    # ── ทิศตรงข้าม: ห้ามตัดสลิปจริงทิ้ง ──
    def test_สลิปโอนปกติ_ต้องไม่โดนด่านบิล(self):
        slip = {"is_slip": True, "amount": 500.0, "sender": "สมชาย",
                "receiver": "ไส้ย่างซอย4", "ref_number": "0123456789"}
        self.assertFalse(app._is_store_bill(slip))
        self.assertTrue(app._is_income(slip))

    def test_รูปบิลคู่กับสลิป_ยังเป็นรายรับ(self):
        """ถ่ายบิล+สลิปมาในรูปเดียว — AI ตอบ is_slip=true, is_bill=false
        ต้องเดินทางเดิม (ระบบ D เอา bill_total ไปเทียบต่อ) ห้ามโดนตัด"""
        both = {"is_slip": True, "is_bill": False, "amount": 1450.0,
                "bill_total": 1500.0, "bill_table": "A3", "receiver": "ไส้ย่างซอย4"}
        self.assertFalse(app._is_store_bill(both))
        self.assertTrue(app._is_income(both))

    def test_สลิปที่อ่านได้ไม่ครบ_ห้ามถูกเดาว่าเป็นบิล(self):
        """ไม่มีเลขอ้างอิง/ไม่มีชื่อผู้โอน = สลิปที่ OCR อ่านไม่ครบ ไม่ใช่บิล
        เดาตรงนี้เมื่อไหร่ = ตัดเงินลูกค้าทิ้ง (บทเรียน 19/08)"""
        partial = {"is_slip": True, "amount": 897.0, "receiver": "ไส้ย่างซอย4",
                   "ref_number": None, "sender": None}
        self.assertFalse(app._is_store_bill(partial))

    def test_ไม่มีฟิลด์_is_bill_เลย_ต้องไม่พัง(self):
        """ผลลัพธ์เก่า/กลุ่มที่ prompt ยังไม่มีฟิลด์นี้ → ต้องถือว่าไม่ใช่บิล ไม่ใช่ error"""
        self.assertFalse(app._is_store_bill({"is_slip": True, "amount": 100.0}))
        self.assertFalse(app._is_store_bill({}))


class BillPromptAsksEveryGroup(unittest.TestCase):
    """prompt ต้องถาม is_bill 'ทุกกลุ่ม' — ไม่ใช่เฉพาะ DINING_GROUPS
    (ต้นเหตุเดิม: 3 ใน 4 กลุ่มรับสลิปไม่เคยถูกถามว่ารูปนี้เป็นบิลไหม)"""

    def _capture_prompt(self, dining):
        seen = {}

        def _fake(parts, model=None, json_mode=False):
            seen["prompt"] = parts[0]
            raise RuntimeError("stop-after-prompt")   # ไม่ต้องยิงเน็ตจริง

        _orig = app._gemini_generate
        app._gemini_generate = _fake
        try:
            app.extract_slip_info(b"fake-image-bytes", dining=dining)
        except Exception:
            pass
        finally:
            app._gemini_generate = _orig
        return seen.get("prompt", "")

    def test_กลุ่มที่ไม่ได้เปิด_dining_ก็ต้องถาม_is_bill(self):
        p = self._capture_prompt(dining=False)
        self.assertIn('"is_bill"', p, "กลุ่มธรรมดาต้องถาม is_bill ด้วย")
        self.assertIn('"bill_total"', p)

    def test_กลุ่ม_dining_ยังถามเลขโต๊ะเหมือนเดิม(self):
        p = self._capture_prompt(dining=True)
        self.assertIn('"is_bill"', p)
        self.assertIn('"bill_table"', p, "กลุ่ม dining ต้องได้เลขโต๊ะไว้จับคู่")

    def test_กลุ่มธรรมดาไม่ต้องถามเลขโต๊ะ(self):
        """เลขโต๊ะใช้เฉพาะตอนจับคู่ — ถามในกลุ่มที่ไม่มีระบบจับคู่ = เพิ่มโอกาสอ่านเพี้ยนเปล่าๆ"""
        self.assertNotIn('"bill_table"', self._capture_prompt(dining=False))


class RecoveryMustNotResurrectBills(unittest.TestCase):
    """ตัวกู้สลิปต้องไม่ดึงบิลที่เคยข้ามถูกแล้ว กลับเข้ามาเป็นรายรับ
    (กู้อัตโนมัติรันเองก่อนส่งรายงานประจำวัน — พลาดตรงนี้ = ยอดเกินทุกวันโดยไม่มีใครสั่ง)"""

    def test_ป้ายบิลต้องไม่ปนกับป้ายโอนระหว่างบัญชีร้าน(self):
        """สองป้ายนี้แก้คนละวิธี ห้ามใช้ข้อความเดียวกัน"""
        self.assertNotEqual(app._BILL_MARK, app._INTERNAL_MARK)
        self.assertFalse(app._BILL_MARK.startswith(app._INTERNAL_MARK))
        self.assertFalse(app._INTERNAL_MARK.startswith(app._BILL_MARK))

    def test_detail_ของบิลต้องขึ้นต้นด้วยป้ายบิล(self):
        """ตัวกู้สลิปตัดสินจาก 'ขึ้นต้นด้วยป้าย' — รูปแบบ detail ต้องตรงกับที่ตัวกู้มองหา"""
        detail = f"{app._BILL_MARK} 1,450.00 · โต๊ะ B16 (บิลของร้าน ไม่ใช่หลักฐานว่ามีเงินเข้า)"
        self.assertTrue(detail.startswith(app._BILL_MARK))
        # และต้อง 'ไม่' เข้ารูปแบบทางลัด 'ยอด → ปลายทาง' ที่ตัวกู้ใช้ตัดสินใหม่เป็นรายรับ
        import re
        self.assertIsNone(re.match(r"\s*([\d,]+(?:\.\d+)?)\s*→\s*(.+)$", detail),
                          "detail ของบิลต้องไม่ถูกทางลัด 'ตัดสินใหม่' หยิบไปทำเป็นรายรับ")


if __name__ == "__main__":
    unittest.main()
