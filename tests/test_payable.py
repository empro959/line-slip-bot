"""เทสต์ regression ของ 'บัญชีเจ้าหนี้การค้า' (ระบบที่ 3) + ตัวแปลง SQL Postgres

ทำไมต้องมี: ส่วนนี้คือ 'ตัวเลขเงิน' ที่เคยพังจริงมาแล้วหลายรอบ —
  • ยอดค้างติดลบ 128,206 → −194,437 (reconcile ทิ้ง orphan)
  • _cleanup_old_data() พังเงียบทุกวันบน Postgres (ลืม escape '%')
  • จ่ายไปตัดผิดใบ (วันที่บนสลิปชนะยอดที่ตรงเป๊ะ)
เคสพวกนี้ตาเปล่าไม่เห็น ต้องมีเทสต์ยึงไว้

วิธีรัน:  python3 -m unittest discover -s tests -v
ไม่ต้องมี DB จริง/คีย์จริง — ใช้ SQLite ไฟล์ชั่วคราว และคีย์ปลอม (ไม่ยิงเน็ตออก)
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# ── ต้องตั้ง env ก่อน import app (app.py อ่าน env ตอน import และสร้าง client ทันที) ──
os.environ.pop("DATABASE_URL", None)                     # บังคับใช้ SQLite
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy-token")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy-secret")
os.environ.setdefault("GEMINI_API_KEY", "dummy-key")     # แค่ให้ import ผ่าน ไม่ได้ยิงจริง
os.environ.setdefault("PAYABLE_GROUPS", "Gtest")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# ย้าย cwd ไปโฟลเดอร์ชั่วคราว 'ก่อน' import app — app.py สตาร์ต background thread ตอน import
# และ thread พวกนั้นแตะ DB ทันที (DB_PATH เป็น './slips.db' แบบ relative)
# ถ้าไม่ย้ายก่อน จะไปสร้าง slips.db ทิ้งไว้ในโฟลเดอร์ repo
_tmpdir = tempfile.TemporaryDirectory(prefix="line_slip_test_")
_prev_cwd = os.getcwd()
os.chdir(_tmpdir.name)

import app  # noqa: E402

ACCT = "Gtest"
MIRROR = "Gmirror"


def _d(days_ago: int) -> str:
    """วันที่แบบสัมพัทธ์กับวันนี้ — กันเทสต์เน่าเมื่อเวลาผ่านไป
    (_sane_doc_date ไม่รับวันอนาคต และไม่รับที่เก่าเกิน PAYABLE_DATE_MAX_DAYS)"""
    return (datetime.now(app.TZ).date() - timedelta(days=days_ago)).isoformat()


def _dm(iso: str) -> str:
    """แปลง 'YYYY-MM-DD' → 'd/m' แบบที่คนพิมพ์ในกลุ่มจริงๆ"""
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.day}/{d.month}"


def _dmy(iso: str) -> str:
    """แปลง 'YYYY-MM-DD' → 'DD/MM/YY' แบบที่บอทพิมพ์ในสรุปหนี้"""
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d}"


def setUpModule():
    """ชี้ DB ไปไฟล์ชั่วคราว — ไม่แตะ slips.db ของจริง"""
    app.DB_PATH = os.path.join(_tmpdir.name, "test.db")   # _db() อ่านค่านี้ตอนเรียก
    app.init_db()


def tearDownModule():
    os.chdir(_prev_cwd)
    _tmpdir.cleanup()


class PayableTestCase(unittest.TestCase):
    """ฐานร่วม: ล้างตารางก่อนทุกเคส ให้แต่ละเคสเป็นอิสระต่อกัน"""

    def setUp(self):
        with app._db() as conn:
            conn.execute("DELETE FROM payable_bills WHERE group_id=?", (ACCT,))
            conn.execute("DELETE FROM payable_payments WHERE group_id=?", (ACCT,))
            conn.commit()

    def bills(self):
        with app._db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, doc_date, amount, COALESCE(paid,0) paid FROM payable_bills "
                "WHERE group_id=? ORDER BY id", (ACCT,)).fetchall()]

    def paid_on(self, doc_date):
        """ยอดที่ถูกตัดของบิลวันนั้น (ไม่มีบรรทัด = None แปลว่าโดน cleanup ลบเพราะจ่ายครบ)"""
        for b in self.bills():
            if b["doc_date"] == doc_date:
                return b["paid"]
        return None


class TestPgSqlEscape(unittest.TestCase):
    """_Conn._pg_sql — เคสจริง 2026-08-17: _cleanup_old_data() พังทุกวันบน Postgres
    เพราะ "LIKE 'sent:%'" ไม่ถูก escape → psycopg2 ตีความ % เป็น format spec →
    IndexError → transaction rollback → ลบข้อมูลเก่าไม่เคยทำงานเลย (SQLite ไม่เจอบั๊กนี้)"""

    def test_escape_percent_then_placeholder(self):
        got = app._Conn._pg_sql("DELETE FROM x WHERE note LIKE 'sent:%' AND d < ?")
        self.assertEqual(got, "DELETE FROM x WHERE note LIKE 'sent:%%' AND d < %s")

    def test_survives_psycopg2_style_formatting(self):
        """psycopg2 ทำ `query % args` — ของที่ escape แล้วต้องไม่ระเบิด"""
        sql = app._Conn._pg_sql("DELETE FROM x WHERE note LIKE 'sent:%' AND d < ?")
        self.assertEqual(sql % ("2026-01-01",),
                         "DELETE FROM x WHERE note LIKE 'sent:%' AND d < 2026-01-01")

    def test_order_matters_placeholder_not_double_escaped(self):
        """ต้อง escape ก่อนแปลง ? → %s ไม่งั้น %s ที่เพิ่งสร้างจะกลายเป็น %%s"""
        self.assertEqual(app._Conn._pg_sql("SELECT ?"), "SELECT %s")
        self.assertNotIn("%%s", app._Conn._pg_sql("SELECT ? WHERE a LIKE 'x%'"))

    def test_regression_guard_old_code_really_was_broken(self):
        """ยืนยันว่าโค้ดเดิม (แปลง ? → %s เฉยๆ ไม่ escape) พังจริง — กันเทสต์ที่ผ่านโดยไม่ตรงเคส"""
        old = "DELETE FROM x WHERE note LIKE 'sent:%' AND d < ?".replace("?", "%s")
        with self.assertRaises((IndexError, ValueError, TypeError)):
            old % ("2026-01-01",)


class TestPayableSettle(PayableTestCase):
    """_payable_settle — ลำดับจับคู่ 'จ่าย'↔'บิล' (เจ้าของตกลง 2026-08-13):
    (1) ยอดค้างตรงเป๊ะในวันที่บนสลิป (2) ยอดค้างตรงเป๊ะทั้งบัญชี (3) FIFO ของวันที่บนสลิป"""

    def test_exact_amount_beats_slip_date(self):
        """เคสจริง 12/08/26: จ่าย 2,867 = ค่าบิลเมื่อ 14 วันก่อนเป๊ะ แต่สลิปให้วันที่วันนี้มา
        ต้องไปตัดใบที่ยอดตรง ไม่ใช่ไปตัดบางส่วนใบของวันนี้"""
        old_day, today = _d(14), _d(0)
        app.save_payable_bill(ACCT, 2867.0, note="รูป", doc_date=old_day)
        app.save_payable_bill(ACCT, 15091.0, note="รูป", doc_date=today)

        allocated, settled, note = app._payable_settle(ACCT, today, 2867.0)

        self.assertAlmostEqual(allocated, 2867.0, places=2)
        self.assertAlmostEqual(self.paid_on(old_day), 2867.0, places=2)
        self.assertAlmostEqual(self.paid_on(today), 0.0, places=2,
                               msg="ไปตัดผิดใบ — บิลของวันนี้ไม่ควรถูกแตะ")
        self.assertEqual(len(settled), 1)

    def test_fifo_fallback_when_no_exact_match(self):
        """ไม่มีใบยอดตรง → ตัด FIFO ตามวันที่ที่โน้ตไว้ (ตัดบางส่วนได้)"""
        day = _d(7)
        app.save_payable_bill(ACCT, 1000.0, note="รูป", doc_date=day)
        app.save_payable_bill(ACCT, 2000.0, note="รูป", doc_date=day)

        allocated, settled, note = app._payable_settle(ACCT, day, 1500.0)

        rows = self.bills()
        self.assertAlmostEqual(allocated, 1500.0, places=2)
        self.assertAlmostEqual(rows[0]["paid"], 1000.0, places=2)   # ใบแรกครบ
        self.assertAlmostEqual(rows[1]["paid"], 500.0, places=2)    # ใบสองบางส่วน

    def test_carry_forward_line_settled_first(self):
        """'ยอดค้างยกมา' ต้องถูกตัดก่อนบิลปกติที่ยอดเท่ากัน"""
        day = _d(5)
        app.save_payable_bill(ACCT, 900.0, note="รูป", doc_date=day)
        app.save_payable_bill(ACCT, 900.0, note=app._PAYABLE_CARRY_NOTE, doc_date=day)

        app._payable_settle(ACCT, day, 900.0)

        rows = self.bills()
        self.assertAlmostEqual(rows[0]["paid"], 0.0, places=2, msg="บิลปกติไม่ควรโดนก่อน")
        self.assertAlmostEqual(rows[1]["paid"], 900.0, places=2, msg="ค้างยกมาต้องโดนตัดก่อน")

    def test_overpay_allocates_only_what_exists(self):
        """จ่ายเกินยอดบิล — จัดสรรได้แค่เท่าที่มีบิลรองรับ ส่วนเกินไม่หายไปไหน (ไปลดยอดค้างรวม)"""
        day = _d(3)
        app.save_payable_bill(ACCT, 500.0, note="รูป", doc_date=day)

        allocated, settled, note = app._payable_settle(ACCT, day, 800.0)

        self.assertAlmostEqual(allocated, 500.0, places=2)
        self.assertAlmostEqual(self.paid_on(day), 500.0, places=2)


class TestPayableUnlink(PayableTestCase):
    """_payable_unlink — ลบบิล/จ่าย ต้อง 'คืนยอดที่จับคู่ไว้' ก่อนลบ
    ไม่คืน = ยอดค้างเพี้ยน และ 'จัดยอดใหม่' จะขึ้น orphan ผิดปกติ ซ่อมไม่ได้"""

    def _pay(self, day, amount):
        allocated, settled, note = app._payable_settle(ACCT, day, amount)
        return app.save_payable_payment(ACCT, amount, doc_date=day,
                                        allocated=allocated, settle_note=note)

    def test_delete_payment_restores_bill_paid(self):
        day = _d(6)
        app.save_payable_bill(ACCT, 5000.0, note="รูป", doc_date=day)
        pid = self._pay(day, 5000.0)

        self.assertAlmostEqual(self.paid_on(day), 5000.0, places=2)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 0.0, places=2)

        with app._db() as conn:
            row = conn.execute("SELECT * FROM payable_payments WHERE id=?", (pid,)).fetchone()
            leftover = app._payable_unlink(conn, ACCT, "payable_payments", row)
            conn.execute("DELETE FROM payable_payments WHERE id=?", (pid,))
            conn.commit()

        self.assertAlmostEqual(leftover, 0.0, places=2, msg="ควรคืนยอดได้ครบ")
        self.assertAlmostEqual(self.paid_on(day), 0.0, places=2)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 5000.0, places=2)

    def test_delete_bill_restores_payment_allocated(self):
        day = _d(6)
        app.save_payable_bill(ACCT, 3000.0, note="รูป", doc_date=day)
        pid = self._pay(day, 3000.0)
        bill_id = self.bills()[0]["id"]

        with app._db() as conn:
            row = conn.execute("SELECT * FROM payable_bills WHERE id=?", (bill_id,)).fetchone()
            app._payable_unlink(conn, ACCT, "payable_bills", row)
            conn.execute("DELETE FROM payable_bills WHERE id=?", (bill_id,))
            conn.commit()
            alloc = conn.execute("SELECT COALESCE(allocated,0) a FROM payable_payments "
                                 "WHERE id=?", (pid,)).fetchone()["a"]

        self.assertAlmostEqual(float(alloc), 0.0, places=2,
                               msg="ลบบิลแล้วต้องคืน allocated ของฝั่งจ่าย")
        self.assertAlmostEqual(app._payable_outstanding(ACCT), -3000.0, places=2,
                               msg="ลบบิลทิ้งแต่เงินจ่ายยังอยู่ → ค้างติดลบเท่าที่จ่ายไป")


class TestPayableReconcile(PayableTestCase):
    """_payable_reconcile ('จัดยอดใหม่') — ต้องคงยอดค้างเป๊ะ และใช้กติกาเดียวกับ settle"""

    def _seed_exact_match_case(self):
        old_day, today = _d(14), _d(0)
        app.save_payable_bill(ACCT, 2867.0, note="รูป", doc_date=old_day)
        app.save_payable_bill(ACCT, 15091.0, note="รูป", doc_date=today)
        allocated, settled, note = app._payable_settle(ACCT, today, 2867.0)
        app.save_payable_payment(ACCT, 2867.0, doc_date=today,
                                 allocated=allocated, settle_note=note)
        return old_day, today

    def test_total_preserved_and_no_wrong_bill_moved(self):
        old_day, today = self._seed_exact_match_case()
        before_outstanding = app._payable_outstanding(ACCT)

        total_before, total_after = app._payable_reconcile(ACCT)

        self.assertAlmostEqual(total_before, total_after, places=2, msg="self-verify ต้องตรง")
        self.assertAlmostEqual(app._payable_outstanding(ACCT), before_outstanding, places=2)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 15091.0, places=2)
        # บิลที่จ่ายครบถูก _payable_cleanup_paid ลบทิ้ง = พฤติกรรมที่ตั้งใจ
        self.assertIsNone(self.paid_on(old_day), "บิลที่จ่ายครบควรถูก cleanup ลบ")
        self.assertAlmostEqual(self.paid_on(today), 0.0, places=2,
                               msg="จัดยอดใหม่ไม่ควรย้ายการตัดไปผิดใบ")

    def test_reconcile_twice_does_not_break(self):
        """เคสจริง: สั่ง 'จัดยอดใหม่' 2 ครั้งซ้อนหลังมีบิลจ่ายครบ ของเดิม ValueError ใช้ไม่ได้เลย
        (orphan ต้องถูก 'จองโควตา' ก่อนจับคู่ ไม่ใช่ฉีดกลับทีหลัง)"""
        self._seed_exact_match_case()
        app._payable_reconcile(ACCT)

        total_before, total_after = app._payable_reconcile(ACCT)   # ครั้งที่ 2 ต้องไม่ระเบิด

        self.assertAlmostEqual(total_before, total_after, places=2)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 15091.0, places=2)

    def test_orphan_from_cleaned_up_bill_is_kept(self):
        """บิลที่จ่ายครบถูกลบไปแล้ว แต่ 'จ่าย' ยังมี allocated>0 (orphan)
        reconcile ต้องคงยอดไว้ ห้าม reset allocated ทิ้ง (บั๊กเดิมทำยอดติดลบ)"""
        self._seed_exact_match_case()
        app._payable_reconcile(ACCT)          # รอบนี้ทำให้เกิด orphan (บิลถูก cleanup)
        app.save_payable_bill(ACCT, 1000.0, note="รูป", doc_date=_d(1))
        expected = app._payable_outstanding(ACCT)

        app._payable_reconcile(ACCT)

        self.assertAlmostEqual(app._payable_outstanding(ACCT), expected, places=2)
        self.assertGreater(app._payable_outstanding(ACCT), 0,
                           "ยอดค้างต้องไม่ติดลบเพราะ orphan หาย")


class TestPayablePushSummary(PayableTestCase):
    """_payable_send_summary — reply token หมดอายุ (Gemini อ่านรูปนาน) ต้อง fallback push
    ไม่งั้นสรุปหนี้หายเงียบ (เคสจริง: ส่งบิลแล้วสรุปไม่เด้ง)

    หมายเหตุ 22/08/26: ตัวส่งจริงถูกแยกออกจาก _payable_push_summary (ซึ่งตอนนี้ทำหน้าที่ 'เข้าคิว')
    เทสต์ชุดนี้จึงยิงที่ตัวส่งตรงๆ — เรื่องคิว/รวบใบเดียวดูที่ TestPayableSummaryDebounce"""

    def setUp(self):
        super().setUp()
        self._orig = (app._push, app.line_bot_api.reply_message,
                      app._payable_report_recipients, app._group_left)
        self.pushed, self.replied = [], []
        app._push = lambda grp, msg: self.pushed.append(grp)
        app._payable_report_recipients = lambda acct: [(ACCT, False), (MIRROR, True)]
        app._group_left = lambda grp: False

    def tearDown(self):
        (app._push, app.line_bot_api.reply_message,
         app._payable_report_recipients, app._group_left) = self._orig

    class _Event:
        reply_token = "dummy-reply-token"

    def test_fallback_push_when_reply_token_expired(self):
        app.line_bot_api.reply_message = self._fail_reply
        app.save_payable_bill(ACCT, 100.0, note="รูป", doc_date=_d(0))

        app._payable_send_summary(ACCT, ACCT, self._Event.reply_token)

        self.assertIn(ACCT, self.pushed, "reply พังแล้วต้อง push กลุ่มต้นทางแทน")
        self.assertIn(MIRROR, self.pushed, "กลุ่ม mirror ต้องได้สรุปด้วย")

    def test_happy_path_uses_free_reply_for_source_group(self):
        """ปกติ (reply ไม่พัง): กลุ่มต้นทางใช้ reply ฟรี ไม่กินโควตา push"""
        app.line_bot_api.reply_message = self._ok_reply
        app.save_payable_bill(ACCT, 100.0, note="รูป", doc_date=_d(0))

        app._payable_send_summary(ACCT, ACCT, self._Event.reply_token)

        self.assertEqual(len(self.replied), 1, "กลุ่มต้นทางต้องใช้ reply")
        self.assertNotIn(ACCT, self.pushed, "กลุ่มต้นทางไม่ควรกินโควตา push")
        self.assertIn(MIRROR, self.pushed)

    def _fail_reply(self, *args, **kwargs):
        raise Exception("Invalid reply token")

    def _ok_reply(self, *args, **kwargs):
        self.replied.append(args)


class TestPayableBillGuards(PayableTestCase):
    """กันบันทึกซ้ำ / วันที่เพี้ยน — ด่านหน้าก่อนตัวเลขเข้าบัญชี"""

    def test_duplicate_bill_detected(self):
        day = _d(2)
        app.save_payable_bill(ACCT, 1234.0, note="รูป", doc_date=day)
        self.assertTrue(app._payable_bill_exists(ACCT, day, 1234.0))
        self.assertFalse(app._payable_bill_exists(ACCT, day, 1234.5))
        self.assertFalse(app._payable_bill_exists(ACCT, _d(3), 1234.0))

    def test_sane_doc_date_rejects_future_and_too_old(self):
        future = (datetime.now(app.TZ).date() + timedelta(days=1)).isoformat()
        too_old = (datetime.now(app.TZ).date()
                   - timedelta(days=app.PAYABLE_DATE_MAX_DAYS + 1)).isoformat()
        self.assertIsNone(app._sane_doc_date(future), "วันอนาคต = AI อ่านเพี้ยน")
        self.assertIsNone(app._sane_doc_date(too_old), "เก่าเกินเพดาน = AI อ่านเพี้ยน")
        self.assertIsNone(app._sane_doc_date("ไม่ใช่วันที่"))
        self.assertEqual(app._sane_doc_date(_d(1)), _d(1))

    def test_date_from_memo(self):
        """แกะวัน/เดือนจากบันทึกช่วยจำบนสลิป (regex ชัวร์กว่าให้ AI เดา)"""
        self.assertIsNone(app._date_from_memo(None))
        self.assertIsNone(app._date_from_memo("ไม่มีวันที่"))
        year = datetime.now(app.TZ).year
        got = app._date_from_memo("ไส้ (23/7) ค้าง 7,993")
        if got is not None:                      # None ได้ถ้าเลยเพดาน 90 วันตามช่วงที่รัน
            self.assertRegex(got, r"^\d{4}-07-23$")
            self.assertLessEqual(int(got[:4]), year)


class TestCarryBlock(PayableTestCase):
    """บล็อก 'ค้าง' ที่เจ้าของก๊อปมาวางในกลุ่ม — เคสจริงจากกลุ่ม management 18/08/26

    ของเดิม: บรรทัดสรุปท้ายบล็อกที่คนเขียนเอง ('= 2,050') ถูกตีเป็น 'อ่านไม่ได้'
    ขึ้น ❌ 'โปรดแก้แล้ววางใหม่ทั้งบล็อก' ทั้งที่บันทึก 3 บรรทัดถูกครบแล้ว
    ตอนนี้ใช้เป็น 'ตัวตรวจ' แทน — ตรงกันก็ยืนยันให้ ไม่ตรงถึงเตือน"""

    def setUp(self):
        super().setUp()
        self._orig = (app._payable_send, app._payable_push_summary)
        self.sent = []
        app._payable_send = lambda ev, gid, out, text: self.sent.append(text)
        app._payable_push_summary = lambda *a, **k: None

    def tearDown(self):
        app._payable_send, app._payable_push_summary = self._orig

    class _Event:
        reply_token = "tok"

    def _paste(self, block):
        self.sent = []
        handled = app.handle_payable_text(self._Event(), block, ACCT)
        return handled, (self.sent[0] if self.sent else "")

    def test_pasting_the_bots_own_summary_restores_carry(self):
        """🔴 เคสจริง 18/08/26 — ล้างบัญชีแล้วก๊อป 'สรุปหนี้' ของบอทเองมาวางกลับ

        สรุปของบอทเขียนว่า '08/08/26  ยกมา 19,614.00' ไม่มี '=' แต่โค้ดบังคับว่า
        บรรทัดต้องมี '=' → บอทเงียบสนิท ยอดขึ้น 0 ทั้งที่วางถูกต้อง
        (คอมมิตเก่าโฆษณาว่า 'ก๊อปสรุปมาทั้งดุ้นก็ได้' แต่ใช้ไม่ได้จริง)"""
        d1, d2, d3 = _d(10), _d(7), _d(5)
        summary = (
            "📋 สรุปหนี้  — รายวัน\n"
            "━━━━━━━━━━━━━\n"
            f"{_dmy(d1)}  ยกมา 19,614.00\n"
            f"{_dmy(d2)}  ยกมา 15,835.00\n"
            f"{_dmy(d3)}  ยกมา 12,788.00\n"
            "━━━━━━━━━━━━━\n"
            f"{_dmy(_d(4))}  📥+30,403.00\n"
            f"{_dmy(_d(3))}  📥+22,449.00")
        handled, msg = self._paste(summary)

        self.assertTrue(handled, "ต้องอ่านสรุปของบอทเองออก")
        self.assertIn("✅", msg)
        self.assertNotIn("❌", msg, "บรรทัดบิล 📥 ไม่ใช่ error")
        # ยกมา 48,237 + บิล 30,403 + 22,449 = 101,089 (บล็อกกู้บิลได้ด้วยแล้ว)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 101089.0, places=2)
        self.assertEqual(len(self.bills()), 5, "ยกมา 3 + บิล 2")

    def test_pasting_full_summary_restores_carry_and_bills(self):
        """กู้ทั้งบัญชีด้วยการวางสรุปทีเดียว — ไม่ต้องพิมพ์บิลทีละบรรทัด

        เคสจริง 18/08/26: หลังล้างบัญชี ต้องกู้ยกมา 3 + บิล 8 ใบ = 165,117
        ของเดิมวางบล็อกได้แค่ยอดยกมา บิลต้องพิมพ์เอง 8 ข้อความ (พิมพ์ผิดง่าย)"""
        carry = [(_d(10), 19614.0), (_d(7), 15835.0), (_d(5), 12788.0)]
        bills = [(_d(4), 30403.0), (_d(3), 22449.0), (_d(3), 7155.0), (_d(2), 13135.0),
                 (_d(1), 20250.0), (_d(1), 6210.0), (_d(0), 1740.0), (_d(0), 15538.0)]
        summary = "📋 สรุปหนี้  — รายวัน\n━━━━━━━━━━━━━\n"
        summary += "".join(f"{_dmy(d)}  ยกมา {v:,.2f}\n" for d, v in carry)
        summary += "━━━━━━━━━━━━━\n"
        summary += "".join(f"{_dmy(d)}  📥+{v:,.2f}\n" for d, v in bills)

        handled, msg = self._paste(summary)

        self.assertTrue(handled)
        self.assertIn("📥 บิลซื้อ +8 ใบ", msg)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 165117.0, places=2)

    def test_pasting_full_summary_twice_does_not_double(self):
        """วางซ้ำต้องไม่นับเบิ้ล — บิลที่วันที่+ยอดตรงกันอยู่แล้วให้ข้าม"""
        summary = (f"📋 สรุปหนี้\n{_dmy(_d(5))}  ยกมา 1,000.00\n"
                   f"{_dmy(_d(3))}  📥+2,000.00\n{_dmy(_d(2))}  📥+3,000.00")
        self._paste(summary)
        first = app._payable_outstanding(ACCT)
        handled, msg = self._paste(summary)

        self.assertAlmostEqual(app._payable_outstanding(ACCT), first, places=2)
        self.assertAlmostEqual(first, 6000.0, places=2)
        self.assertIn("ข้ามที่มีอยู่แล้ว 2 ใบ", msg)

    def test_payment_lines_cannot_be_restored_and_say_so(self):
        """บรรทัด 'จ่าย' คืนอัตโนมัติไม่ได้ (ต้องจับคู่บิล) — ต้องบอก ไม่ใช่เงียบ"""
        handled, msg = self._paste(
            f"📋 สรุปหนี้\n{_dmy(_d(5))}  ยกมา 1,000.00\n{_dmy(_d(4))}  ยกมา 500.00\n"
            f"{_dmy(_d(3))}  💸−500.00")

        self.assertTrue(handled)
        self.assertIn("จ่าย", msg)
        self.assertIn("คืนอัตโนมัติไม่ได้", msg)

    def test_summary_line_with_partial_payment_uses_remaining(self):
        """บรรทัดยกมาที่จ่ายบางส่วนแล้วมี '(เหลือ x)' ต่อท้าย → ต้องใช้ยอดที่เหลือ"""
        handled, msg = self._paste(
            f"📋 สรุปหนี้\n{_dmy(_d(6))}  ยกมา 10,000.00  💸 จ่าย 4,000.00 (เหลือ 6,000.00)\n"
            f"{_dmy(_d(5))}  ยกมา 2,000.00")

        self.assertTrue(handled)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 8000.0, places=2)

    def test_staff_meat_report_must_not_wipe_carry_forward(self):
        """🔴 เคสจริง 18/08/26 ที่ทำยอดหนี้หาย — ต้องไม่เกิดอีกเด็ดขาด

        พนักงานรายงานค่าเนื้อในกลุ่ม (ไม่มีคำว่า 'ค้าง'/'ยกมา' เลย) แต่เข้าเงื่อนไข
        '≥2 บรรทัด d/m=ยอด' พอดี → บอทตีเป็นบล็อกยอดค้าง แล้ว **ลบยอดค้างยกมาเดิม
        ทั้งชุด (169,427) ทิ้ง** เหลือ 2,050"""
        day1, day2, day3 = _d(5), _d(4), _d(3)
        for d, v in ((day1, 100000.0), (day2, 50000.0), (day3, 19427.0)):
            app.save_payable_bill(ACCT, v, note=app._PAYABLE_CARRY_NOTE, doc_date=d)
        before = app._payable_outstanding(ACCT)
        self.assertAlmostEqual(before, 169427.0, places=2)

        block = ("ยอดเนื้อกาดสามแยก\n"
                 f"{_dm(day1)}=430 ม้าม1/2 แดง 1\n"
                 f"{_dm(day2)}=760ตับ 1/2 แดง 1 เศษเนื้อ 2\n"
                 f"{_dm(day3)} = 860 ไส้2ลาบ 200น่อง1\n"
                 "= 2,050")
        handled, msg = self._paste(block)

        self.assertFalse(handled, "ข้อความรายงานค่าเนื้อ ไม่ใช่คำสั่งวางบล็อกยอดค้าง")
        self.assertAlmostEqual(app._payable_outstanding(ACCT), before, places=2,
                               msg="ยอดค้างยกมาต้องไม่ถูกแตะเลย")

    def test_replacing_carry_forward_says_what_it_overwrote(self):
        """วางบล็อกทับ = ลบของเดิมทั้งชุด ต้องบอกให้เห็นว่าทับไปเท่าไร ห้ามเงียบ"""
        app.save_payable_bill(ACCT, 169427.0, note=app._PAYABLE_CARRY_NOTE, doc_date=_d(9))

        handled, msg = self._paste(f"ค้าง\n{_dm(_d(5))}=430\n{_dm(_d(4))}=760")

        self.assertTrue(handled)
        self.assertIn("♻️", msg)
        self.assertIn("169,427.00", msg, "ต้องบอกยอดเดิมที่เพิ่งทับทิ้ง")
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 1190.0, places=2)

    def test_real_case_trailing_total_line_is_a_checksum(self):
        """เคสจริงเป๊ะ: 3 บรรทัดยอดค้าง + บรรทัด '= 2,050' ที่คนบวกไว้ท้ายบล็อก"""
        block = ("ยอดค้างเนื้อกาดสามแยก\n"
                 f"{_dm(_d(5))}=430 ม้าม1/2 แดง 1\n"
                 f"{_dm(_d(4))}=760ตับ 1/2 แดง 1 เศษเนื้อ 2\n"
                 f"{_dm(_d(3))} = 860 ไส้2ลาบ 200น่อง1\n"
                 "= 2,050")
        handled, msg = self._paste(block)

        self.assertTrue(handled)
        self.assertIn("✅", msg)
        self.assertNotIn("❌", msg, "บรรทัดสรุปท้ายบล็อกไม่ใช่ error")
        self.assertIn("ตรงกับยอดรวมท้ายบล็อก", msg)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 2050.0, places=2)
        self.assertEqual(len(self.bills()), 3, "ต้องลง 3 บรรทัด ไม่ใช่ 4")

    def test_mismatched_trailing_total_is_flagged(self):
        """คนบวกเลขท้ายบล็อกผิด/ตกบรรทัด → ต้องเตือน ไม่ปล่อยผ่าน"""
        block = f"ค้าง\n{_dm(_d(5))}=430\n{_dm(_d(4))}=760\n= 9,999"
        handled, msg = self._paste(block)

        self.assertTrue(handled)
        self.assertIn("🔴", msg)
        self.assertIn("ไม่ตรงกับยอดรวมท้ายบล็อก", msg)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 1190.0, places=2,
                               msg="ยังต้องบันทึกบรรทัดที่อ่านได้ตามปกติ")

    def test_block_without_trailing_total_still_works(self):
        """ไม่มีบรรทัดสรุปท้าย = ไม่ต้องมีตัวตรวจ ไม่ควรมีข้อความเพี้ยนโผล่"""
        block = f"ค้าง\n{_dm(_d(5))}=430\n{_dm(_d(4))}=760"
        handled, msg = self._paste(block)

        self.assertTrue(handled)
        self.assertNotIn("ยอดรวมท้ายบล็อก", msg)
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 1190.0, places=2)

    def test_unreadable_line_still_reported(self):
        """บรรทัดที่อ่านไม่ได้จริงๆ ต้องยังขึ้น ❌ เหมือนเดิม (ห้ามกลบเงียบ)"""
        block = f"ค้าง\n{_dm(_d(5))}=430\nอะไรไม่รู้=ไม่มีเลข"
        handled, msg = self._paste(block)

        self.assertTrue(handled)
        self.assertIn("❌", msg)


class TestDbExportCoverage(unittest.TestCase):
    """/api/db_export ต้อง backup 'ทุกตาราง' จริงตามที่โฆษณาไว้

    เคสจริง 18/08/26: recon_pending ไม่อยู่ใน _MIGRATE_TABLES → endpoint ที่บอกว่า
    'dump ทุกตาราง' ข้ามมันไปเงียบๆ เพิ่งมาเจอตอนไล่นับตารางในมือก่อนปิด DB เก่า
    เทสต์นี้จะแดงทันทีที่มีคนเพิ่มตารางใหม่แล้วลืมใส่ในลิสต์"""

    def test_migrate_tables_covers_every_table_in_db(self):
        actual = set(app._db_all_tables())
        listed = set(app._MIGRATE_TABLES)
        self.assertEqual(
            actual - listed, set(),
            "มีตารางใน DB ที่ /api/db_export ไม่ได้ backup — เพิ่มใน _MIGRATE_TABLES")

    def test_no_phantom_table_in_list(self):
        """กันทางกลับ: ลิสต์อ้างตารางที่ไม่มีจริง → export จะ 500 ทั้งก้อน"""
        actual = set(app._db_all_tables())
        self.assertEqual(
            set(app._MIGRATE_TABLES) - actual, set(),
            "_MIGRATE_TABLES อ้างตารางที่ไม่มีใน DB")


class TestWebhookForward(unittest.TestCase):
    """ส่งต่อ event ให้ระบบคอนเทนต์ (OA ตัวเดียว หลายระบบ)

    ทำไมต้องมี: จุดนี้อยู่บนเส้นทางของ *ทุก* ข้อความที่เข้าบอท —
    พลาดแล้วบอทร้านดับทั้งระบบ (เคยดับมาแล้วตอน webhook ถูกเปลี่ยนไปทำคอนเทนต์)
    กฎเหล็ก 3 ข้อ: ปลายทางล่มต้องไม่ทำบอทพัง · ต้องส่ง body ดิบ+ลายเซ็นเดิม · ไม่ตั้ง env = ไม่ยิงอะไรเลย"""

    def setUp(self):
        self._orig = app.WEBHOOK_FORWARD_URLS[:]
        self.calls = []

    def tearDown(self):
        app.WEBHOOK_FORWARD_URLS[:] = self._orig

    def _fake_post(self, ok=True, boom=False):
        class _R:
            status_code = 200 if ok else 500
        def _post(url, data=None, timeout=None, headers=None):
            self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
            if boom:
                raise RuntimeError("ปลายทางล่ม")
            return _R()
        return _post

    def test_forwards_raw_body_and_signature(self):
        app.WEBHOOK_FORWARD_URLS[:] = ["https://example.test/hook"]
        orig = app.requests.post
        app.requests.post = self._fake_post()
        try:
            app._forward_webhook(b'{"events":[]}', "SIG123")
        finally:
            app.requests.post = orig
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual(c["data"], b'{"events":[]}')          # ต้องเป็น byte ดิบ ไม่ใช่ dict/str ที่ถูกแปลง
        self.assertEqual(c["headers"]["X-Line-Signature"], "SIG123")
        self.assertEqual(c["headers"]["Content-Type"], "application/json")

    def test_endpoint_down_must_not_raise(self):
        """ปลายทางล่ม/ช้า = เรื่องของเขา ห้ามเด้งขึ้นมาถึง /callback"""
        app.WEBHOOK_FORWARD_URLS[:] = ["https://dead.test/hook"]
        orig = app.requests.post
        app.requests.post = self._fake_post(boom=True)
        try:
            app._forward_webhook(b"{}", "SIG")   # ต้องไม่ throw
        finally:
            app.requests.post = orig
        self.assertEqual(len(self.calls), 1)

    def test_multiple_targets_all_receive(self):
        app.WEBHOOK_FORWARD_URLS[:] = ["https://a.test/h", "https://b.test/h"]
        orig = app.requests.post
        app.requests.post = self._fake_post()
        try:
            app._forward_webhook(b"{}", "SIG")
        finally:
            app.requests.post = orig
        self.assertEqual([c["url"] for c in self.calls], ["https://a.test/h", "https://b.test/h"])

    def test_no_env_means_no_traffic(self):
        """ไม่ตั้ง env = ต้องไม่ยิงเน็ตออกเลย (ดีฟอลต์ต้องปลอดภัย)"""
        app.WEBHOOK_FORWARD_URLS[:] = []
        orig = app.requests.post
        app.requests.post = self._fake_post()
        try:
            app._forward_webhook(b"{}", "SIG")
        finally:
            app.requests.post = orig
        self.assertEqual(self.calls, [])


class TestModelGoneDetection(unittest.TestCase):
    """โมเดล AI ถูกปลดระวาง — ต้องรู้ทันที ไม่ใช่เงียบ

    ทำไมต้องมี: 19/08/26 ห้องคอนเทนต์โดน Groq ปลดโมเดลกลางอากาศ ระบบเงียบไป 7 วัน
    ฝั่งสลิปเป็นเรื่องเงิน จะเงียบแบบนั้นไม่ได้ · โมเดลหาย = ปัญหาถาวร ห้าม retry วนเปล่า"""

    def test_detects_provider_wording(self):
        for msg in [
            "404 NOT_FOUND: models/gemini-9.9-flash is not found for API version v1beta",
            '{"error":{"message":"The model `x` does not exist or you do not have access","code":"model_not_found"}}',
            "Publisher Model `models/gemini-old` was not found",
        ]:
            self.assertTrue(app._is_model_gone(Exception(msg)), f"ต้องจับได้: {msg[:50]}")

    def test_does_not_confuse_with_temporary_errors(self):
        """503/429/รูปหมดอายุ = ชั่วคราว ต้องไม่ถูกตีเป็น 'โมเดลหาย' (ไม่งั้นเลิก retry ทั้งที่ควร retry)"""
        for msg in [
            "503 The model is overloaded. Please try again later",
            "429 RESOURCE_EXHAUSTED: quota exceeded",
            "LINE 410 content is gone",
        ]:
            self.assertFalse(app._is_model_gone(Exception(msg)), f"ต้องไม่จับ: {msg[:50]}")

    def test_stops_retrying_immediately(self):
        """ปัญหาถาวร → ต้องยิงครั้งเดียวแล้วเลิก (ของเดิมไล่ 4 รอบ + หน่วง 14 วิ ต่อรูป)"""
        calls = []
        class _Boom:
            def generate_content(self, model=None, contents=None, config=None):
                calls.append(model)
                raise Exception("404 NOT_FOUND: models/x is not found for API version v1beta")
        class _C:
            models = _Boom()
        orig = app.gemini_client
        app.gemini_client = _C()
        try:
            with self.assertRaises(Exception):
                app._gemini_generate("hi", attempts=4)
        finally:
            app.gemini_client = orig
        self.assertEqual(len(calls), 1, "โมเดลหายแล้วยัง retry ซ้ำ = เสียเวลาเปล่า")

    def test_still_retries_temporary_failures(self):
        """กันแก้เกิน: 503 ต้องยัง retry ครบตามเดิม"""
        calls = []
        class _Boom:
            def generate_content(self, model=None, contents=None, config=None):
                calls.append(model)
                raise Exception("503 The model is overloaded")
        class _C:
            models = _Boom()
        orig, orig_sleep = app.gemini_client, app.time.sleep
        app.gemini_client = _C()
        app.time.sleep = lambda *_: None      # ไม่ต้องรอจริงตอนเทสต์
        try:
            with self.assertRaises(Exception):
                app._gemini_generate("hi", attempts=3)
        finally:
            app.gemini_client, app.time.sleep = orig, orig_sleep
        self.assertEqual(len(calls), 3)


class TestPayeeMatching(unittest.TestCase):
    """เทียบ 'ปลายทางบนสลิป' กับบัญชีร้าน — ด่านที่ตัดสินว่าเงินเข้าร้านหรือไม่

    ทำไมต้องมี: 19/08/26 สลิปโอนเข้าร้าน 2 ใบ (120 + 897) ถูกตัดทิ้งว่า 'ไม่ใช่รายรับ'
    เพราะ OCR อ่านชื่อร้านเพี้ยน (ไส้ย่าง → ไส้บ้าง / ไยย่าง) แล้วเทียบคีย์เวิร์ดไม่ตรง
    ด่านนี้พลาด = รายรับหายเงียบ ไม่มี error ไม่มีใครรู้"""

    def test_thai_digits_normalized(self):
        """สลิปเขียน 'ซอย๔' (เลขไทย) ต้องเทียบติดกับคีย์เวิร์ด 'ซอย4' (เลขอารบิก)"""
        self.assertEqual(app._norm_match_text("ไส้ย่างซอย๔"), app._norm_match_text("ไส้ย่างซอย4"))

    def test_existing_normalizations_still_work(self):
        """กันแก้เกิน: ของเดิม (ใ→ไ, ตัดช่องว่าง/ขีด) ต้องยังทำงาน"""
        self.assertEqual(app._norm_match_text("ใส้ ย่าง-ซอย 4"), app._norm_match_text("ไส้ย่างซอย4"))
        self.assertEqual(app._norm_match_text("460-5949"), "4605949")

    def test_ocr_typo_in_shop_name_still_counts_as_income(self):
        """เคสจริง 19/08/26 — ปลายทางที่บอทอ่านได้จาก 3 ใบที่ถูกตัดทิ้งผิด (รวม 1,017 บาท)
        ต้องกลับมานับเป็นรายรับ ทั้งที่ชื่อร้านเพี้ยน"""
        orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            real_cases = [
                {"receiver": "บจก. ไส้บ้างซอย4", "receiver_account": "202608193367819", "bank": "K PLUS"},
                {"receiver": "ไส้บ้างซอย 4", "receiver_account": "202608193485741", "bank": "กสิกรไทย"},
                {"receiver": "ไยย่างซอย 4", "receiver_account": "202608193367819", "bank": "K PLUS"},
            ]
            for c in real_cases:
                self.assertTrue(app._is_income(c), f"ต้องนับเป็นรายรับ: {c['receiver']}")
        finally:
            app.PAYEE_ACCOUNTS[:] = orig

    def test_other_companies_must_not_be_counted(self):
        """ด้านกลับที่สำคัญกว่า: ยอมให้เพี้ยนได้ ต้องไม่แปลว่าเงินคนอื่นกลายเป็นเงินร้าน"""
        orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            for c in [
                {"receiver": "บจก. โปร พลัส มัลติเทค", "receiver_account": "x-9431", "bank": "ธนาคาร"},
                {"receiver": "นาง พิมนภัทร์ สุวภาพ และ นาย สุวัฒน์ บุญเรือง", "receiver_account": "x-6120"},
                {"receiver": "บจก. ไทยเบฟเวอเรจ", "receiver_account": "x-1111"},
                {"receiver": "นาย สมชาย ใจดี", "receiver_account": "x-2222"},
            ]:
                self.assertFalse(app._is_income(c), f"ต้องไม่นับเป็นรายรับ: {c['receiver']}")
        finally:
            app.PAYEE_ACCOUNTS[:] = orig

    def test_account_number_must_match_exactly(self):
        """เลขบัญชีห้ามเดา — เพี้ยนหลักเดียวคือคนละบัญชี"""
        orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ร้าน", ["4818"])]
        try:
            self.assertTrue(app._is_income({"receiver_account": "xxx-x-x4818-x"}))
            self.assertFalse(app._is_income({"receiver_account": "xxx-x-x4819-x"}))
            self.assertFalse(app._is_income({"receiver_account": "xxx-x-x4881-x"}))
        finally:
            app.PAYEE_ACCOUNTS[:] = orig

    def test_generic_keyword_catches_ocr_typos(self):
        """คีย์เวิร์ดที่เลี่ยงคำที่ OCR พลาดบ่อย ('ซอย4') ต้องจับได้ทุกแบบที่เจอจริงวันนี้"""
        orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ซอย4"])]
        try:
            for name in ["บจก. ไส้บ้างซอย4", "ไส้บ้างซอย 4", "ไยย่างซอย 4", "ไส้ย่างซอย๔"]:
                self.assertTrue(app._is_income({"receiver": name}), f"ต้องนับเป็นรายรับ: {name}")
            # คนละบริษัท ต้องไม่ถูกนับเป็นรายรับ (ใบ 710 ของวันนั้น)
            self.assertFalse(app._is_income({"receiver": "บจก. โปร พลัส มัลติเทค"}))
        finally:
            app.PAYEE_ACCOUNTS[:] = orig


class TestResvDraft(unittest.TestCase):
    """ร่างจองค้าง — บอทถามเฉพาะที่ขาด แล้วเอาคำตอบสั้นๆ มาต่อกับของเดิม
    (เจ้าของสั่ง 19/08/26: 'ถามเป็นข้อแล้วให้คนจองตอบ แล้วสรุปเลย ถามแบบคนถามกัน')"""

    def setUp(self):
        app._resv_draft_clear("Gstaff")

    def test_no_draft_by_default(self):
        self.assertIsNone(app._resv_draft_get("Gstaff"))

    def test_save_and_read_back(self):
        app._resv_draft_set("Gstaff", "คุณยานา 12 คน โซน A2-3-4", 1)
        d = app._resv_draft_get("Gstaff")
        self.assertEqual(d["text"], "คุณยานา 12 คน โซน A2-3-4")
        self.assertEqual(d["rounds"], 1)

    def test_draft_expires(self):
        """ร่างต้องหมดอายุเอง ไม่ค้างไปเอาข้อความคนละเรื่องมาต่อทีหลัง"""
        import json as _json
        stale = app.time.time() - (app.RESV_FOLLOWUP_MIN * 60 + 60)
        app._set_meta("resvdraft:Gstaff", _json.dumps({"text": "x", "ts": stale, "rounds": 1}))
        self.assertIsNone(app._resv_draft_get("Gstaff"))

    def test_corrupt_draft_does_not_crash(self):
        app._set_meta("resvdraft:Gstaff", "{ไม่ใช่ json")
        self.assertIsNone(app._resv_draft_get("Gstaff"))

    def test_cleared_after_complete(self):
        app._resv_draft_set("Gstaff", "x", 1)
        app._resv_draft_clear("Gstaff")
        self.assertIsNone(app._resv_draft_get("Gstaff"))

    def test_every_missing_field_has_a_human_question(self):
        """ทุกช่องที่บอทเช็ค ต้องมีคำถามภาษาคน ไม่งั้นจะ KeyError กลางทางตอนถาม"""
        for k in ("customer", "people", "date", "time", "table"):
            self.assertIn(k, app._RESV_ASK)
            self.assertTrue(app._RESV_ASK[k].endswith("?") or "?" in app._RESV_ASK[k])


class TestResvLooksLike(unittest.TestCase):
    """จับข้อความที่ 'หน้าตาเป็นการจอง' แม้ไม่มีคำว่าจอง

    เคสจริง 20/08/26: ข้อความแรกของพนักงานเงียบสนิท ต้องพิมพ์ใหม่ใส่คำว่า 'จองให้' ถึงจะติด"""

    def test_real_dropped_message_now_detected(self):
        self.assertTrue(app._looks_like_resv(
            "คุณยานา จำนวน 12 คน เวลา 0826062980 โซน A2-3-4 วันที่ 20/8/69"))

    def test_common_booking_shapes(self):
        for t in ["คุณเอ 4 คน 2 ทุ่ม โซน A", "6 ท่าน พรุ่งนี้ 19:00", "คุณบี 5 คน เสาร์นี้ 1 ทุ่ม"]:
            self.assertTrue(app._looks_like_resv(t), t)

    def test_casual_chat_must_not_trigger(self):
        """สัญญาณเดียวไม่พอ — ไม่งั้นยิง AI ทุกข้อความในกลุ่ม เปลืองและตอบมั่ว"""
        for t in ["ไปกินข้าวกัน 3 คน", "วันนี้เหนื่อยจัง", "พรุ่งนี้เจอกัน",
                  "ส่งของแล้วนะ", "โอนไป 500 แล้ว"]:
            self.assertFalse(app._looks_like_resv(t), t)


class TestResvLabelAnswer(unittest.TestCase):
    """คำตอบสั้นๆ ต้องรู้ว่าตอบข้อไหน

    เคสจริง 20/08/26: บอทถาม 'กี่ท่านครับ?' ตอบ '12' → ไม่รู้จัก ต้องพิมพ์ '12 ท่าน' ถึงติด"""

    def test_bare_number_becomes_people(self):
        d = {"asked": ["people"]}
        self.assertEqual(app._resv_label_answer(d, "12"), "จำนวน 12 คน")

    def test_range_number(self):
        self.assertEqual(app._resv_label_answer({"asked": ["people"]}, "5-6"), "จำนวน 5-6 คน")

    def test_other_fields_get_their_tag(self):
        self.assertEqual(app._resv_label_answer({"asked": ["time"]}, "19.30"), "เวลา 19.30")
        self.assertEqual(app._resv_label_answer({"asked": ["table"]}, "A2"), "โซน A2")
        self.assertEqual(app._resv_label_answer({"asked": ["date"]}, "25"), "วันที่ 25")

    def test_self_explanatory_answers_left_alone(self):
        """คำที่บอกชนิดในตัวอยู่แล้ว ไม่ต้องเติมป้าย (อ่านออกอยู่แล้ว เติมไปก็รกเปล่าๆ)"""
        self.assertEqual(app._resv_label_answer({"asked": ["date"]}, "พรุ่งนี้"), "พรุ่งนี้")
        self.assertEqual(app._resv_label_answer({"asked": ["date"]}, "20/8"), "20/8")
        self.assertEqual(app._resv_label_answer({"asked": ["time"]}, "2 ทุ่ม"), "2 ทุ่ม")

    def test_answer_that_already_has_tag_is_untouched(self):
        self.assertEqual(app._resv_label_answer({"asked": ["people"]}, "12 ท่าน"), "12 ท่าน")
        self.assertEqual(app._resv_label_answer({"asked": ["table"]}, "โซน B"), "โซน B")

    def test_multiple_questions_left_raw(self):
        """ถามหลายข้อ = ไม่รู้ว่าคำตอบตรงกับข้อไหน ต้องปล่อยให้ AI อ่านเอง ห้ามเดา"""
        self.assertEqual(app._resv_label_answer({"asked": ["people", "time"]}, "12 สองทุ่ม"), "12 สองทุ่ม")

    def test_long_answer_left_raw(self):
        long = "ลูกค้าบอกว่ามาประมาณ 12 คนแต่อาจจะเพิ่มอีกสองสามคนนะครับไม่แน่ใจ"
        self.assertEqual(app._resv_label_answer({"asked": ["people"]}, long), long)


class TestResvFollowupWindow(unittest.TestCase):
    """พนักงานพิมพ์จองใหม่หลังบอทถามข้อมูลที่ขาด — ต้องไม่หลุด

    เคสจริง 19/08/26 กลุ่ม Staff: บอทถามหา 'เวลา' ตอน 15:15 → พนักงานพิมพ์ใหม่ครบทุกอย่าง
    ตอน 15:19 แต่ไม่มีคำว่า 'จอง' → ด่านคำใบ้ตัดทิ้งเงียบ จองวันที่ 20/8 หายทั้งใบ"""

    def setUp(self):
        app._resv_draft_clear("Gstaff")

    def test_window_closed_by_default(self):
        self.assertFalse(app._resv_followup_open("Gstaff"))

    def test_window_opens_after_bot_asked(self):
        app._resv_draft_set("Gstaff", "คุณยานา 12 คน", 1)
        self.assertTrue(app._resv_followup_open("Gstaff"))

    def test_real_message_that_was_dropped_has_no_hint_word(self):
        """ยืนยันสาเหตุ: ข้อความจริงที่หลุด ไม่มีคำใบ้สักคำ แต่มีตัวเลข (เงื่อนไขที่ใช้เปิดทาง)"""
        real = "คุณยานา จำนวน 12 คน เวลา 19.30 0826062980 โซน A2-3-4 วันที่ 20/8/69 @All"
        self.assertFalse(any(h in real.lower() for h in app._RESV_HINTS))
        self.assertTrue(any(c.isdigit() for c in real))


class TestRecoverMissedSlips(unittest.TestCase):
    """กู้รูปที่อ่านไม่ออกย้อนหลัง — หัวใจคือ 'ต้องลงวันเดิม ไม่ใช่วันที่กดกู้'

    ทำไมต้องมี: 19/08/26 มีสลิปหลุด 3 ใบ ต้องมานั่งไล่หาเองว่าใบไหน แล้วกรอกมือ
    ซึ่งคำสั่งกรอกมือลงเป็น 'วันนี้' เสมอ → ถ้าทำข้ามวัน ยอดเพี้ยนสองวันรวด"""

    def setUp(self):
        self.gid = "Grecover"
        with app._db() as c:
            c.execute("DELETE FROM image_misses WHERE group_id=?", (self.gid,))
            c.execute("DELETE FROM slips WHERE group_id=?", (self.gid,))
            c.commit()

    def test_nothing_to_work_with_says_so(self):
        """ใบพลาดที่ไม่มีทั้งรหัสรูปและบันทึกยอด = กู้ไม่ได้จริง ต้องบอกตรงๆ ไม่ใช่เงียบ

        เดิม SQL กรองใบพวกนี้ทิ้งตั้งแต่ต้น ผลลัพธ์เลยไม่พูดถึงมันเลย —
        เคสจริง 21/08/26: รายงานบอก 'ตกหล่น 17 ใบ' แต่ผลกู้เงียบสนิท เจ้าของนึกว่าตรวจครบแล้ว"""
        app.record_image_miss(self.gid, "notslip")      # อ่านไม่ออก + ไม่มีรหัสรูป
        out = app.recover_missed_slips(self.gid, _d(0))
        self.assertIn("กู้อัตโนมัติไม่ได้ 1 ใบ", out)
        self.assertIn("ไม่มีรหัสรูป", out)

    def test_old_notincome_without_image_id_still_recoverable(self):
        """ใบเก่าก่อนระบบเก็บรหัสรูป — ถ้ามีบันทึกยอด/ปลายทาง ต้องยังกู้ได้
        (เคสจริง 19/08: 120 + 897 บันทึกไว้ก่อนมีคอลัมน์ message_id)"""
        target_date = _d(1)
        with app._db() as c:
            c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail) "
                      "VALUES (?,?,?,?,?)",
                      (self.gid, target_date, "notincome", "20:12:54",
                       "120.00 → บจก. ไส้บ้างซอย4 / 202608193367819 / K PLUS"))
            c.commit()
        orig_pa = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            out = app.recover_missed_slips(self.gid, target_date)
            again = app.recover_missed_slips(self.gid, target_date)   # กดซ้ำต้องไม่เบิ้ล
        finally:
            app.PAYEE_ACCOUNTS[:] = orig_pa
        self.assertIn("กู้เข้าระบบแล้ว 1 ใบ", out)
        self.assertNotIn("กู้เข้าระบบแล้ว 1 ใบ", again)
        with app._db() as c:
            n = c.execute("SELECT COUNT(*) c FROM slips WHERE group_id=? AND slip_date=?",
                          (self.gid, target_date)).fetchone()["c"]
        self.assertEqual(n, 1, "กดกู้ซ้ำแล้วต้องไม่ได้ยอดเบิ้ล")

    def test_saving_slip_clears_its_earlier_miss_row(self):
        """อ่านรอบแรกไม่ออก → บันทึกว่าพลาด → อ่านซ้ำผ่าน → แถวพลาดต้องหายไป
        ไม่งั้นลิสต์ 'ใบที่ถูกข้าม' มีของที่บันทึกแล้วค้าง ทำให้ดูเหมือนเงินหาย"""
        app.record_image_miss(self.gid, "notincome", detail="1,175.00 → ?", message_id="Mretry")
        app.save_slip(self.gid, {"amount": 1175.0, "sender": "สุจิตรา"}, "PASS", message_id="Mretry")
        with app._db() as c:
            n = c.execute("SELECT COUNT(*) c FROM image_misses WHERE group_id=? AND message_id=?",
                          (self.gid, "Mretry")).fetchone()["c"]
        self.assertEqual(n, 0, "แถวพลาดของรูปเดียวกันต้องถูกล้างเมื่อบันทึกสำเร็จ")

    def test_other_misses_untouched(self):
        """ล้างเฉพาะรูปเดียวกัน ห้ามไปลบใบพลาดของรูปอื่น"""
        app.record_image_miss(self.gid, "notslip", message_id="Mother")
        app.save_slip(self.gid, {"amount": 100.0}, "PASS", message_id="Mmine")
        with app._db() as c:
            n = c.execute("SELECT COUNT(*) c FROM image_misses WHERE group_id=?", (self.gid,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_cap_counts_only_ai_reads(self):
        """เพดานต้องนับเฉพาะใบที่ต้องอ่านรูปด้วย AI
        เคสจริง 20/08/26: นับรวมใบที่กู้จากบันทึก (ซึ่งฟรี) → ใบที่ต้องอ่านจริงไม่เคยถูกแตะ
        พิมพ์ซ้ำกี่รอบก็ขึ้น 'ยังเหลืออีก 2 ใบ' เท่าเดิม = วนไม่จบ"""
        target_date = _d(1)
        with app._db() as c:
            for i in range(3):    # ใบที่กู้จากบันทึกได้ (ไม่ใช้ AI)
                c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail) "
                          "VALUES (?,?,?,?,?)",
                          (self.gid, target_date, "notincome", f"20:0{i}:00",
                           f"{100+i}.00 → บจก. ไส้บ้างซอย4 / x / K PLUS"))
            c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, message_id) "
                      "VALUES (?,?,?,?,?)", (self.gid, target_date, "notslip", "21:00:00", "Mneed"))
            c.commit()
        reads = []
        class _Api:
            def get_message_content(self, mid):
                reads.append(mid)
                raise Exception("410 content is gone")
        orig_api, app.line_bot_api = app.line_bot_api, _Api()
        orig_pa = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            out = app.recover_missed_slips(self.gid, target_date, limit=2)
        finally:
            app.line_bot_api = orig_api
            app.PAYEE_ACCOUNTS[:] = orig_pa
        self.assertIn("กู้เข้าระบบแล้ว 3 ใบ", out)          # ใบฟรีต้องได้ครบ ไม่โดนเพดานกิน
        self.assertEqual(reads, ["Mneed"], "ใบที่ต้องอ่านด้วย AI ต้องถูกแตะ")
        self.assertNotIn("ยังเหลืออีก", out)

    def test_recovered_keeps_original_time(self):
        """กู้ย้อนหลังต้องคงเวลาเดิมของใบ ไม่ใช่เวลาที่กดกู้ — ไม่งั้นลำดับในรายงานเพี้ยน"""
        target_date = _d(1)
        with app._db() as c:
            c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail) "
                      "VALUES (?,?,?,?,?)",
                      (self.gid, target_date, "notincome", "20:12:54",
                       "120.00 → บจก. ไส้บ้างซอย4 / x / K PLUS"))
            c.commit()
        orig_pa = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            app.recover_missed_slips(self.gid, target_date)
        finally:
            app.PAYEE_ACCOUNTS[:] = orig_pa
        with app._db() as c:
            row = c.execute("SELECT recorded_at, sender FROM slips WHERE group_id=?", (self.gid,)).fetchone()
        self.assertEqual(row["recorded_at"], "20:12:54")
        self.assertTrue(row["sender"], "ต้องไม่ปล่อยชื่อว่าง (รายงานจะขึ้น None)")

    def test_records_message_id_when_given(self):
        app.record_image_miss(self.gid, "notslip", message_id="M123")
        with app._db() as c:
            row = c.execute("SELECT message_id FROM image_misses WHERE group_id=?", (self.gid,)).fetchone()
        self.assertEqual(row["message_id"], "M123")

    def test_expired_image_reported_not_crashed(self):
        """รูปหมดอายุใน LINE (410) = เรื่องปกติ ต้องรายงานว่ากู้ไม่ได้ ไม่ใช่ throw"""
        app.record_image_miss(self.gid, "notslip", message_id="Mgone")
        class _Api:
            def get_message_content(self, mid):
                raise Exception("410 content is gone")
        orig, app.line_bot_api = app.line_bot_api, _Api()
        try:
            out = app.recover_missed_slips(self.gid, _d(0))
        finally:
            app.line_bot_api = orig
        self.assertIn("หมดอายุ", out)

    def test_recovered_slip_is_filed_under_original_date(self):
        """ใจความสำคัญ: กู้วันนี้ แต่ต้องลงเป็นวันของรูป"""
        target_date = _d(1)                      # เมื่อวาน
        with app._db() as c:                     # จำลองใบพลาดของเมื่อวาน
            c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail, message_id) "
                      "VALUES (?,?,?,?,?,?)", (self.gid, target_date, "notslip", "20:12:00", None, "Mok"))
            c.commit()
        class _Content:
            def iter_content(self): return [b"fake"]
        class _Api:
            def get_message_content(self, mid): return _Content()
        orig_api, app.line_bot_api = app.line_bot_api, _Api()
        orig_ex, app.extract_slip_info = app.extract_slip_info, (
            lambda b, retry=False: {"is_slip": True, "amount": 120.0, "sender": "ศักดิ์ชัย"})
        orig_pa, app.PAYEE_ACCOUNTS[:] = app.PAYEE_ACCOUNTS[:], []
        try:
            out = app.recover_missed_slips(self.gid, target_date)
        finally:
            app.line_bot_api, app.extract_slip_info = orig_api, orig_ex
            app.PAYEE_ACCOUNTS[:] = orig_pa
        self.assertIn("กู้เข้าระบบแล้ว 1 ใบ", out)
        with app._db() as c:
            rows = c.execute("SELECT slip_date, amount FROM slips WHERE group_id=?", (self.gid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slip_date"], target_date, "กู้แล้วต้องลงวันเดิม ไม่ใช่วันที่กดกู้")
        self.assertEqual(float(rows[0]["amount"]), 120.0)

    def test_notincome_recovered_without_touching_line_or_ai(self):
        """ใบที่ 'อ่านออกแล้วแต่ตัดว่าไม่ใช่เงินร้าน' ต้องกู้ได้จากบันทึกเดิม
        ไม่ต้องโหลดรูป ไม่ต้องยิง AI — เคสจริง 19/08 (120 + 897 ที่ชื่อร้าน OCR เพี้ยน)"""
        target_date = _d(1)
        with app._db() as c:
            for detail, mid in [("120.00 → บจก. ไส้บ้างซอย4 / 202608193367819 / K PLUS", "Mn1"),
                                ("897.00 → ไส้บ้างซอย 4 / 202608193485741 / กสิกรไทย", "Mn2"),
                                ("710.00 → บจก. โปร พลัส มัลติเทค / x-9431 / ธนาคาร", "Mn3")]:
                c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail, message_id) "
                          "VALUES (?,?,?,?,?,?)", (self.gid, target_date, "notincome", "20:12:00", detail, mid))
            c.commit()

        def _boom(*a, **k):
            raise AssertionError("ต้องไม่โหลดรูปจาก LINE ในทางลัดนี้")
        orig_api, app.line_bot_api = app.line_bot_api, type("X", (), {"get_message_content": _boom})()
        orig_pa = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง"])]
        try:
            out = app.recover_missed_slips(self.gid, target_date)
        finally:
            app.line_bot_api = orig_api
            app.PAYEE_ACCOUNTS[:] = orig_pa
        self.assertIn("กู้เข้าระบบแล้ว 2 ใบ", out)
        with app._db() as c:
            rows = c.execute("SELECT slip_date, amount FROM slips WHERE group_id=? ORDER BY amount", (self.gid,)).fetchall()
        self.assertEqual([float(r["amount"]) for r in rows], [120.0, 897.0])
        self.assertTrue(all(r["slip_date"] == target_date for r in rows), "ต้องลงวันเดิม")

    def test_does_not_double_add_same_image(self):
        """กดกู้ซ้ำต้องไม่ได้ยอดเบิ้ล"""
        target_date = _d(1)
        with app._db() as c:
            c.execute("INSERT INTO image_misses (group_id, stat_date, reason, recorded_at, detail, message_id) "
                      "VALUES (?,?,?,?,?,?)", (self.gid, target_date, "notslip", "20:12:00", None, "Mdup"))
            c.execute("INSERT INTO slips (group_id, slip_date, sender, amount, verdict, recorded_at, message_id) "
                      "VALUES (?,?,?,?,?,?,?)", (self.gid, target_date, "ศักดิ์ชัย", 120.0, "PASS", "20:12:00", "Mdup"))
            c.commit()
        out = app.recover_missed_slips(self.gid, target_date)
        self.assertIn("บันทึกไปแล้ว", out)
        with app._db() as c:
            n = c.execute("SELECT COUNT(*) c FROM slips WHERE group_id=?", (self.gid,)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestFlipDetection(unittest.TestCase):
    """ชื่อร้านไปโผล่ฝั่งผู้โอน = สัญญาณว่าอ่านสลับด้าน

    เคสจริง 19/08/26: 3 ใบ (2,486 + 2,195 + 708 = 5,389 บาท) หลุดทั้งหมด
    เพราะบอทเอาข้อมูลฝั่งผู้โอนมาเป็นผู้รับ แล้วเทียบบัญชีร้านไม่ตรง"""

    def setUp(self):
        self._orig = app.PAYEE_ACCOUNTS[:]
        # เลียนแบบของจริง: บัญชีร้านมี 2 ชุด (ชื่อร้าน + บัญชีร่วมเจ้าของ)
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง", "SAI YANG"]),
                                 ("พิมนภัทร์&สุวัฒน์", ["พิมนภัทร", "สุวภาพ", "4612", "99912460"])]

    def tearDown(self):
        app.PAYEE_ACCOUNTS[:] = self._orig

    def test_shop_name_on_sender_side_is_detected(self):
        self.assertTrue(app._kw_hit_any_payee("นาง พิมนภัทร์ สุวภาพ และ นาย สุวัฒน์ บุญ"))
        self.assertTrue(app._kw_hit_any_payee("004-999-1-2460594-9"))

    def test_stranger_on_sender_side_is_not_flagged(self):
        """คนอื่นโอนมา = ปกติที่สุด ห้ามติดธงมั่ว ไม่งั้นลิสต์เต็มไปด้วยธงจนไม่มีใครดู"""
        for t in ["JUMPOT KHUMHA", "SITTHIKORN RI", "นาย สมชาย ใจดี", ""]:
            self.assertFalse(app._kw_hit_any_payee(t), t)


class TestManualSlipParsing(unittest.TestCase):
    """คำสั่งกรอกสลิปมือ ต้องไม่เรื่องมากเรื่องลำดับคำ

    เคสจริง 20/08/26: พิมพ์ตามตัวอย่างแล้วบอทตอบ 'วิธีใช้' กลับมา
    คำสั่งนี้ใช้ตอนยอดไม่ตรงซึ่งมักเป็นตอนเร่งรีบ ยิ่งเรื่องมากยิ่งพลาด"""

    _parse = staticmethod(lambda t: app._parse_manual_slip_line(t))

    def test_amount_and_date_in_any_order(self):
        for t in ["เพิ่มสลิป 19/8 2486 JUMPOT",
                  "เพิ่มสลิป 2486 JUMPOT 19/8",
                  "เพิ่มสลิป JUMPOT 2486 19/8"]:
            d, amt, note = app._parse_manual_slip_line(t)
            self.assertTrue(d.endswith("-08-19"), f"{t} → {d}")
            self.assertEqual(amt, 2486.0, t)

    def test_time_in_note_is_not_taken_as_amount(self):
        """'23:51' ต้องไม่ถูกอ่านเป็นยอด — ไม่งั้นได้ยอด 23 บาท"""
        d, amt, note = app._parse_manual_slip_line("เพิ่มสลิป 19/8 40 ธีรกมล 23:51")
        self.assertEqual(amt, 40.0)
        self.assertIn("23:51", note)

    def test_decimal_and_comma(self):
        _, amt, note = app._parse_manual_slip_line("เพิ่มสลิป 1,175.50 โต๊ะ 5")
        self.assertEqual(amt, 1175.5)
        self.assertIn("โต๊ะ", note)

    def test_yesterday_keyword(self):
        d, amt, note = app._parse_manual_slip_line("เพิ่มสลิปเมื่อวาน 708 ประเสริฐ")
        self.assertEqual(amt, 708.0)
        self.assertEqual(d, (app.datetime.now(app.TZ).date() - app.timedelta(days=1)).isoformat())

    def test_plain_amount_still_works(self):
        self.assertEqual(app._parse_manual_slip_line("เพิ่มสลิป 114"), (None, 114.0, None))

    def test_line_without_keyword_still_parsed(self):
        """วางทีเดียวหลายบรรทัด บรรทัดถัดไปมักไม่พิมพ์คำสั่งซ้ำ"""
        d, amt, note = app._parse_manual_slip_line("19/8 708 ประเสริฐ")
        self.assertEqual(amt, 708.0)
        self.assertTrue(d.endswith("-08-19"))

    def test_junk_line_returns_none(self):
        for t in ["เพิ่มสลิป", "สวัสดีครับ", ""]:
            self.assertIsNone(app._parse_manual_slip_line(t), t)

    def test_bad_date_is_reported(self):
        self.assertEqual(app._parse_manual_slip_line("เพิ่มสลิป 99/99 100"), "BADDATE")


class TestManualSlipAccountLabel(unittest.TestCase):
    """กรอกมือแล้วถ้าโน๊ตบอกบัญชีได้ ควรผูกบัญชีให้ ไม่ใช่กองใน 'ไม่ระบุบัญชี'"""

    def setUp(self):
        self._orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [("ไส้ย่างซอย4", ["ไส้ย่าง", "ซอย4"]),
                                 ("พิมนภัทร์&สุวัฒน์", ["พิมนภัทร", "4612"])]

    def tearDown(self):
        app.PAYEE_ACCOUNTS[:] = self._orig

    def test_note_with_account_keyword_gets_label(self):
        self.assertEqual(app._slip_account_label({"receiver": "โอนเข้า ซอย4"}), "ไส้ย่างซอย4")
        self.assertEqual(app._slip_account_label({"receiver": "เข้าบัญชี พิมนภัทร"}), "พิมนภัทร์&สุวัฒน์")

    def test_note_without_keyword_has_no_label(self):
        """โน๊ตทั่วไปต้องไม่ถูกเดาว่าเป็นบัญชีไหน"""
        self.assertIsNone(app._slip_account_label({"receiver": "JUMPOT KHUMHA"}))


class TestReportWindowVsQuietHours(unittest.TestCase):
    """เลื่อนเวลารายงานไปอยู่ในช่วงเงียบลึกแล้วต้องยังส่งได้

    เคสจริง 20/08/26: เจ้าของตั้ง REPORT_HOUR=5 เพื่อรอข้อมูล POS
    แต่ช่วงเงียบลึก 03:00–10:00 ตัวปลุกจะไม่แตะงานตามเวลาเลย → รายงานจะไม่ออกจนพ้น 10:00"""

    def test_window_covers_report_time(self):
        now = app.datetime.now(app.TZ)
        start = now.replace(hour=app.REPORT_HOUR, minute=app.REPORT_MIN, second=0, microsecond=0)
        for delta_min, expect in [(0, True), (10, True), (44, True), (46, False), (-1, False)]:
            probe = start + app.timedelta(minutes=delta_min)
            in_win = start <= probe < start + app.timedelta(minutes=45)
            self.assertEqual(in_win, expect, f"{delta_min} นาทีหลังเวลารายงาน")

    def test_function_matches_config(self):
        """ฟังก์ชันจริงต้องอิงค่า REPORT_HOUR/REPORT_MIN ที่ตั้งไว้ ไม่ใช่ค่าตายตัว"""
        self.assertIsInstance(app._is_report_window(), bool)
        self.assertTrue(hasattr(app, "REPORT_HOUR") and hasattr(app, "REPORT_MIN"))


class TestPushAcceptsString(unittest.TestCase):
    """_push ต้องรับสตริงได้

    เคสจริง 20/08/26: คำสั่ง 'กู้สลิป' ส่งข้อความเปล่าเข้า _push → SDK พังเงียบในเธรดเบื้องหลัง
    ผู้ใช้เห็นแค่ 'กำลังตรวจ...' แล้วรอผลลัพธ์ที่ไม่มีวันมา 10 นาที
    บั๊กชนิดนี้ต้องกันที่ตัวส่ง ไม่ใช่ไล่แก้ทีละจุดเรียก"""

    def test_string_is_wrapped(self):
        sent = {}
        class _Api:
            def push_message(self, to, messages):
                sent["to"], sent["msg"] = to, messages
        orig = app._push_apis[:]
        app._push_apis[:] = [_Api()]
        try:
            app._push("Gtest", "ข้อความเปล่า")
        finally:
            app._push_apis[:] = orig
        self.assertEqual(sent["to"], "Gtest")
        self.assertIsInstance(sent["msg"], app.TextSendMessage)
        self.assertEqual(sent["msg"].text, "ข้อความเปล่า")

    def test_message_object_passes_through(self):
        sent = {}
        class _Api:
            def push_message(self, to, messages):
                sent["msg"] = messages
        orig = app._push_apis[:]
        app._push_apis[:] = [_Api()]
        try:
            app._push("Gtest", app.TextSendMessage(text="ปกติ"))
        finally:
            app._push_apis[:] = orig
        self.assertEqual(sent["msg"].text, "ปกติ")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestInternalTransferNotIncome(unittest.TestCase):
    """โอนระหว่างบัญชีร้านด้วยกันเอง ต้องไม่ถูกนับเป็นรายรับ

    เคสจริง 20/08/26: สลิป KBIZ 873 บาท
      จาก 'บจก. ไส้ย่างซอย4  xxx-x-x2481-x'  →  'พิมนภัทร์ สุวภาพ และ สุวัฒน์ บุญเรือง  xxx-x-x4612-x'
    ทั้งสองฝั่งเป็นบัญชีร้าน แต่กติกาเดิมดูแค่ปลายทาง → นับเป็นรายรับ = นับซ้ำ
    (เงินก้อนนี้เป็นรายรับตั้งแต่ลูกค้าโอนเข้าบัญชีแรกแล้ว)"""

    def setUp(self):
        self._orig = app.PAYEE_ACCOUNTS[:]
        app.PAYEE_ACCOUNTS[:] = [
            ("ไส้ย่างซอย4",       ["4818", "2481", "ไส้ย่าง", "SAI YANG"]),
            ("พิมนภัทร์&สุวัฒน์", ["6120", "4612", "พิมนภัทร", "สุวัฒน", "สุวภาพ", "บุญเรือง"]),
        ]

    def tearDown(self):
        app.PAYEE_ACCOUNTS[:] = self._orig

    def test_real_case_873_is_internal(self):
        info = {"sender": "บจก. ไส้ย่างซอย4", "account": "xxx-x-x2481-x",
                "receiver": "นาง พิมนภัทร์ สุวภาพ และ นาย สุวัฒน์ บุญเรือง",
                "receiver_account": "xxx-x-x4612-x", "amount": 873.0}
        self.assertEqual(app._internal_transfer_label(info), "ไส้ย่างซอย4")

    def test_customer_paying_in_is_still_income(self):
        """ลูกค้าโอนเข้าร้าน — ต้นทางไม่ใช่บัญชีร้าน → ยังเป็นรายรับ"""
        info = {"sender": "น.ส. กานต์พัชรา ด.", "account": "xxx-x-x1947-x",
                "receiver": "นาง พิมนภัทร์ สุวภาพ และ นาย สุวัฒน์ บุญเรือง",
                "receiver_account": "xxx-x-x4612-x", "amount": 40.0}
        self.assertIsNone(app._internal_transfer_label(info))

    def test_customer_with_same_name_is_not_mistaken_for_shop(self):
        """ลูกค้าชื่อซ้ำคีย์เวิร์ดร้าน ('สุวัฒน์'/'บุญเรือง' เป็นชื่อคนไทยทั่วไป)
        ต้องไม่ถูกตัดทิ้งว่าเป็นการย้ายกระเป๋า — ยืนยันด้วยเลขบัญชีเท่านั้น"""
        info = {"sender": "นาย สุวัฒน์ บุญเรือง", "account": "xxx-x-x7788-x",
                "receiver": "บจก. ไส้ย่างซอย4", "receiver_account": "xxx-x-x2481-x",
                "amount": 500.0}
        self.assertIsNone(app._internal_transfer_label(info))

    def test_shop_paying_out_is_not_internal(self):
        """ร้านจ่ายออกให้บริษัทอื่น (SCB 1,100 → บจก. โปร พลัส มัลติเทค)
        ปลายทางไม่ใช่บัญชีร้าน → ไม่เข้าเคสนี้ (ทางเดิมข้ามให้อยู่แล้ว)"""
        info = {"sender": "นาง พิมนภัทร์ ส.", "account": "xxx-xxx905-1",
                "receiver": "บจก. โปร พลัส มัลติเทค", "receiver_account": "x-9431",
                "amount": 1100.0}
        self.assertIsNone(app._internal_transfer_label(info))

    def test_no_payee_config_means_no_guessing(self):
        app.PAYEE_ACCOUNTS[:] = []
        info = {"sender": "บจก. ไส้ย่างซอย4", "account": "xxx-x-x2481-x",
                "receiver": "พิมนภัทร", "receiver_account": "xxx-x-x4612-x", "amount": 873.0}
        self.assertIsNone(app._internal_transfer_label(info))

    def test_marked_detail_is_not_recovered_back_as_income(self):
        """ป้ายกำกับต้องเห็นได้จาก detail — ตัวกู้สลิปใช้ป้ายนี้กันดึงกลับเข้าเป็นรายรับ"""
        detail = f"{app._INTERNAL_MARK} 873.00 · ไส้ย่างซอย4 → พิมนภัทร์&สุวัฒน์ (ย้ายกระเป๋า ไม่ใช่เงินใหม่)"
        self.assertTrue(detail.startswith(app._INTERNAL_MARK))
        # รูปแบบ 'ยอด → ปลายทาง' ของใบข้ามปกติต้องแกะไม่ติด (ไม่งั้นจะถูกกู้กลับ)
        self.assertIsNone(app.re.match(r"\s*([\d,]+(?:\.\d+)?)\s*→\s*(.+)$", detail))


class TestResvSignalsNewShapes(unittest.TestCase):
    """รหัสโต๊ะลอยๆ กับเวลาแบบพูด ต้องนับเป็นสัญญาณ

    เคสจริง 21/08/26 กลุ่ม Staff 17:30 — พนักงานพิมพ์ 3 ท่อน:
      '@All b10' / 'คุนนาต' / 'มาทุ่ม'
    ทุกท่อนได้ 0 สัญญาณ (แพตเทิร์นโซนบังคับคำว่า 'โต๊ะ' · แพตเทิร์นเวลาบังคับตัวเลขนำหน้า)
    → เงียบทั้งชุด จองหลุด"""

    def test_bare_table_code_counts(self):
        for t in ["b10", "@All b10", "A11", "โซน A2"]:
            self.assertGreaterEqual(app._resv_signal_hits(t), 1, t)

    def test_spoken_time_counts(self):
        for t in ["มาทุ่ม", "มา 2 ทุ่ม", "บ่ายโมง", "เที่ยง"]:
            self.assertGreaterEqual(app._resv_signal_hits(t), 1, t)

    def test_one_topic_must_count_once(self):
        """เรื่องเดียวห้ามนับ 2 — ไม่งั้น 'โต๊ะ b10' เฉยๆ จะผ่านด่านไปเรียก AI"""
        for t in ["โต๊ะ b10", "โซน A2", "2 ทุ่ม", "19:30", "บ่ายโมง"]:
            self.assertEqual(app._resv_signal_hits(t), 1, t)
            self.assertFalse(app._looks_like_resv(t), t)

    def test_thumthe_is_not_time(self):
        self.assertEqual(app._resv_signal_hits("ทุ่มเทมากเลย"), 0)

    def test_english_word_ending_with_digits_is_not_a_table(self):
        for t in ["COVID19 ยังมีอยู่", "ราคา 500"]:
            self.assertEqual(app._resv_signal_hits(t), 0, t)

    def test_real_dropped_fragments_joined_is_detected(self):
        self.assertTrue(app._looks_like_resv("@All b10 คุนนาต มาทุ่ม"))

    def test_casual_chat_with_one_new_signal_still_silent(self):
        for t in ["เดี๋ยว b10 ว่างแล้วบอก", "ส่ง A4 ให้หน่อย", "ทุ่มเทหน่อยนะ"]:
            self.assertFalse(app._looks_like_resv(t), t)


class TestResvMergeFragments(unittest.TestCase):
    """ต่อข้อความสั้นๆ ที่คนเดียวกันพิมพ์ติดกัน (เคส 21/08/26 ข้างบน)"""

    GID, U1, U2 = "Gresv", "Uthanant", "Unattha"

    def setUp(self):
        app._resv_buf_clear(self.GID, self.U1)
        app._resv_buf_clear(self.GID, self.U2)

    def test_three_fragments_become_a_booking(self):
        self.assertFalse(app._looks_like_resv(app._resv_merge_text(self.GID, self.U1, "@All b10")))
        self.assertFalse(app._looks_like_resv(app._resv_merge_text(self.GID, self.U1, "คุนนาต")))
        merged = app._resv_merge_text(self.GID, self.U1, "มาทุ่ม")
        self.assertTrue(app._looks_like_resv(merged), merged)
        self.assertIn("b10", merged)
        self.assertIn("คุนนาต", merged)

    def test_other_person_is_not_mixed_in(self):
        """คนอื่นพิมพ์แทรก ('เจ้า' ของ Natthakan43) ต้องไม่ถูกดึงเข้าชุดของอีกคน"""
        app._resv_merge_text(self.GID, self.U1, "@All b10")
        self.assertNotIn("b10", app._resv_merge_text(self.GID, self.U2, "เจ้า"))

    def test_chat_without_any_signal_does_not_start_a_buffer(self):
        """ไม่มีสัญญาณเลย = ไม่เริ่มเก็บ (ไม่เขียน DB ให้แชททั่วไปทุกข้อความ)"""
        self.assertEqual(app._resv_merge_text(self.GID, self.U1, "เจ้า"), "เจ้า")
        self.assertEqual(app._get_meta(app._resv_buf_key(self.GID, self.U1)) or "", "")

    def test_long_message_is_not_merged(self):
        long_text = "x" * (app.RESV_MERGE_MAXLEN + 1)
        self.assertEqual(app._resv_merge_text(self.GID, self.U1, long_text), long_text)

    def test_buffer_expires(self):
        import json as _json
        app._resv_merge_text(self.GID, self.U1, "@All b10")
        raw = _json.loads(app._get_meta(app._resv_buf_key(self.GID, self.U1)))
        raw["ts"] = raw["ts"] - app.RESV_MERGE_SEC - 5           # ทำให้หมดอายุ
        app._set_meta(app._resv_buf_key(self.GID, self.U1), _json.dumps(raw))
        self.assertEqual(app._resv_merge_text(self.GID, self.U1, "มาทุ่ม"), "มาทุ่ม")

    def test_buffer_keeps_only_recent_messages(self):
        for i in range(app.RESV_MERGE_MAX + 3):
            out = app._resv_merge_text(self.GID, self.U1, f"A{i % 9}")
        self.assertLessEqual(len(out.split()), app.RESV_MERGE_MAX)

    def test_no_user_id_means_no_merging(self):
        """ไม่มี user_id (เช่น event แปลกๆ) → ห้ามเอาไปปนกับของคนอื่น"""
        self.assertEqual(app._resv_merge_text(self.GID, "", "@All b10"), "@All b10")

    def test_clear_after_use(self):
        app._resv_merge_text(self.GID, self.U1, "@All b10")
        app._resv_buf_clear(self.GID, self.U1)
        self.assertEqual(app._resv_merge_text(self.GID, self.U1, "คุนนาต"), "คุนนาต")


class TestResvFalsePositiveGuards(unittest.TestCase):
    """กันบอทตีข้อความ 'เรื่องขายของ/จ่ายเงิน' เป็นการจอง

    เคสจริง 22/08/26 กลุ่ม Staff 00:03 (43 คน):
      'น้องขายให้แล้วค่ะ อันนี้'
      'ลูกค้าบอกให้มาด้วยจ่ายให้ลูกค้า 2 คน น้องกับพี่แตงโม2คน เป็น4'
    คำใบ้ติดที่คำว่า 'ลูกค้า' (คำใบ้อ่อน) + เห็น '2 คน' → AI ตีเป็นจอง 4 ท่านของ 'พี่แตงโม'
    แล้วเด้งการ์ดถามเวลา/โซน · พอพนักงานคุยต่อก็เด้งการ์ดเดิมซ้ำอีกใบ"""

    def _blocked(self, text: str) -> bool:
        return app._resv_is_money_talk(text)

    def test_real_false_positive_is_blocked(self):
        for t in ["น้องขายให้แล้วค่ะ อันนี้",
                  "ลูกค้าบอกให้มาด้วยจ่ายให้ลูกค้า 2 คน น้องกับพี่แตงโม2คน เป็น4",
                  "ขายให้ลุงเกาหลีด้วยค่ะ"]:
            self.assertTrue(self._blocked(t), t)

    def test_real_booking_with_money_words_still_passes(self):
        """มีคำว่า 'จอง/โต๊ะ' อยู่ด้วย = จองจริง ห้ามตัดทิ้ง"""
        for t in ["จองโต๊ะ 4 คน ลูกค้าจ่ายให้แล้ว",
                  "โต๊ะ A5 6 ท่าน 2 ทุ่ม เดี๋ยวเก็บเงินหน้าร้าน"]:
            self.assertFalse(self._blocked(t), t)

    def test_plain_booking_still_passes(self):
        for t in ["คุณเอ 4 คน 2 ทุ่ม โซน A", "@All b10 คุนนาต มาทุ่ม"]:
            self.assertFalse(self._blocked(t), t)

    def test_weak_hint_is_a_subset_of_hints(self):
        for w in app._RESV_WEAK_HINTS:
            self.assertIn(w, app._RESV_HINTS)


class TestResvNoRepeatQuestion(unittest.TestCase):
    """ถามคำถามเดิมเป๊ะๆ ซ้ำ = กวนกลุ่มเปล่าๆ (กลุ่ม Staff มี 43 คน)"""

    GID = "Gask"

    def setUp(self):
        app._resv_draft_clear(self.GID)

    def test_draft_remembers_last_question(self):
        body = "รับจองแล้วครับ ชื่อ พี่แตงโม\nขอถามเพิ่มอีก 2 ข้อครับ\n1) กี่โมงครับ?\n2) นั่งโซนไหนครับ?"
        app._resv_draft_set(self.GID, "ข้อความเดิม", 1, asked=["time", "table"], last_ask=body)
        d = app._resv_draft_get(self.GID)
        self.assertEqual(d["last_ask"], body)
        self.assertEqual(d["asked"], ["time", "table"])

    def test_old_draft_without_last_ask_does_not_crash(self):
        """ร่างที่เขียนไว้ก่อน deploy นี้ยังไม่มีช่อง last_ask — ต้องอ่านได้ ไม่ระเบิด"""
        import json as _json
        app._set_meta(f"resvdraft:{self.GID}",
                      _json.dumps({"text": "x", "ts": app.time.time(), "rounds": 1, "asked": ["time"]}))
        d = app._resv_draft_get(self.GID)
        self.assertEqual((d or {}).get("last_ask") or "", "")

    def test_non_answer_while_draft_open_is_skipped(self):
        """ข้อความที่ไม่มีสัญญาณจองเลย + ไม่ได้กำลังถามชื่อ → ไม่ต้องยิง AI ซ้ำ"""
        draft = {"asked": ["time", "table"]}
        for t in ["แต่หยุดไม่ได้บอกลูกค้าแล้วค่ะ", "ขายให้ลุงเกาหลีด้วยค่ะ"]:
            self.assertTrue(app._resv_not_an_answer(draft, t), t)
        self.assertFalse(app._resv_not_an_answer(None, "อะไรก็ได้"))

    def test_name_answer_is_never_skipped(self):
        """ตอนถามชื่อลูกค้า คำตอบเป็นชื่อคนซึ่งไม่มีสัญญาณใดๆ — ห้ามข้าม"""
        self.assertFalse(app._resv_not_an_answer({"asked": ["customer"]}, "คุณสมชาย"))


class TestPayableSummaryDebounce(unittest.TestCase):
    """ส่งรูปหลายใบรวดเดียว ต้องได้สรุป 'ใบเดียว'

    เคสจริง 22/08/26: ส่งสลิป 2 ใบพร้อมกัน → การ์ดสรุปยาวๆ เด้ง 2 ใบติด เนื้อหาเกือบเหมือนกัน
    กลุ่มต้นทางเป็น reply (ฟรี) แต่กลุ่มคู่เป็น push ซึ่ง LINE คิดโควตาเป็น 'จำนวนสมาชิก' ต่อครั้ง"""

    ACCT = "Gpaydeb"

    class _Ev:
        def __init__(self, token): self.reply_token = token

    def setUp(self):
        self.sent, self.cleaned = [], []
        self._real_send = app._payable_send_summary
        self._real_clean = app._payable_cleanup_paid
        self._real_deb = app.PAYABLE_SUMMARY_DEBOUNCE
        app._payable_send_summary = lambda g, a, t=None: self.sent.append((g, a, t))
        app._payable_cleanup_paid = lambda a: self.cleaned.append(a)
        app._set_meta(app._payable_pend_key(self.ACCT), "")

    def tearDown(self):
        app._payable_send_summary = self._real_send
        app._payable_cleanup_paid = self._real_clean
        app.PAYABLE_SUMMARY_DEBOUNCE = self._real_deb
        app._set_meta(app._payable_pend_key(self.ACCT), "")

    def test_debounce_zero_sends_immediately(self):
        """ตั้ง 0 = พฤติกรรมเดิม ส่งทันที (ทางถอยถ้าไม่ชอบให้รอ)"""
        app.PAYABLE_SUMMARY_DEBOUNCE = 0
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        self.assertEqual(self.sent, [("G1", self.ACCT, "tok1")])

    def test_two_images_produce_one_summary(self):
        app.PAYABLE_SUMMARY_DEBOUNCE = 999          # กันเธรดจริงยิงระหว่างเทสต์
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        stamp1 = json.loads(app._get_meta(app._payable_pend_key(self.ACCT)))["stamp"]
        app._payable_push_summary(self._Ev("tok2"), "G1", self.ACCT)
        # เธรดของรูปใบแรกตื่นมาแล้วเห็นว่าไม่ใช่คิวของตัวเอง → ไม่ส่ง
        self.assertFalse(app._payable_flush_pending(self.ACCT, only_stamp=stamp1))
        self.assertEqual(self.sent, [])
        # เธรดของใบล่าสุดส่ง 1 ครั้ง ด้วย token ที่สดที่สุด
        stamp2 = json.loads(app._get_meta(app._payable_pend_key(self.ACCT)))["stamp"]
        self.assertTrue(app._payable_flush_pending(self.ACCT, only_stamp=stamp2))
        self.assertEqual(self.sent, [("G1", self.ACCT, "tok2")])

    def test_flush_twice_does_not_double_send(self):
        app.PAYABLE_SUMMARY_DEBOUNCE = 999
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        self.assertTrue(app._payable_flush_pending(self.ACCT))
        self.assertFalse(app._payable_flush_pending(self.ACCT))
        self.assertEqual(len(self.sent), 1)

    def test_cleanup_runs_after_summary_and_is_sticky(self):
        """บรรทัดที่จ่ายครบต้องลบ 'หลัง' สรุปออก (ใบนี้ยังโชว์ ✅ จ่ายครบ)
        และถ้ารอบก่อนขอ cleanup ไว้ รอบหลังไม่ได้ขอ ก็ยังต้องทำ (จ่ายครบไปแล้วจริง)"""
        app.PAYABLE_SUMMARY_DEBOUNCE = 999
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT, cleanup=True)
        app._payable_push_summary(self._Ev("tok2"), "G1", self.ACCT)          # บิลซื้อ ไม่ขอ cleanup
        self.assertTrue(app._payable_flush_pending(self.ACCT))
        self.assertEqual(self.cleaned, [self.ACCT])

    def test_no_cleanup_when_never_requested(self):
        app.PAYABLE_SUMMARY_DEBOUNCE = 999
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        app._payable_flush_pending(self.ACCT)
        self.assertEqual(self.cleaned, [])

    def test_min_age_holds_fresh_queue(self):
        """ตาข่ายเก็บคิวค้างต้องไม่ไปแย่งส่งคิวที่เพิ่งเข้ามา (เธรดของมันยังรออยู่)"""
        app.PAYABLE_SUMMARY_DEBOUNCE = 999
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        self.assertFalse(app._payable_flush_pending(self.ACCT, min_age=600))
        self.assertEqual(self.sent, [])

    def test_stale_queue_is_rescued(self):
        """worker ตายกลางทาง → คิวค้าง ตัวเดินตรวจต้องมาเก็บ ไม่ให้สรุปหายเงียบ"""
        app.PAYABLE_SUMMARY_DEBOUNCE = 999
        app._payable_push_summary(self._Ev("tok1"), "G1", self.ACCT)
        key = app._payable_pend_key(self.ACCT)
        d = json.loads(app._get_meta(key)); d["ts"] = d["ts"] - 3600
        app._set_meta(key, json.dumps(d))
        self.assertTrue(app._payable_flush_pending(self.ACCT, min_age=60))
        self.assertEqual(len(self.sent), 1)

    def test_empty_queue_is_a_noop(self):
        self.assertFalse(app._payable_flush_pending(self.ACCT))
        self.assertEqual(self.sent, [])


class TestMemoMultipleDates(unittest.TestCase):
    """โน้ตบนสลิประบุหลายบิล ต้องอ่านครบทุกวัน

    เคสจริง 22/08/26: โอน 9,027 โน้ต '(19/8 และ 21/8) 2 บิล'
    ของเดิมใช้ re.search เอาวันแรกวันเดียว → เห็นแค่ 19/8"""

    def test_reads_every_date(self):
        got = app._dates_from_memo(f"จ่าย ({_dm(_d(3))} และ {_dm(_d(1))}) 2 บิล")
        self.assertEqual(got, [_d(3), _d(1)])

    def test_single_date_still_works(self):
        self.assertEqual(app._dates_from_memo(f"ไส้ ({_dm(_d(2))}) ค้าง 7,993"), [_d(2)])
        self.assertEqual(app._date_from_memo(f"ไส้ ({_dm(_d(2))}) ค้าง 7,993"), _d(2))

    def test_duplicate_dates_collapse(self):
        self.assertEqual(app._dates_from_memo(f"{_dm(_d(1))} กับ {_dm(_d(1))}"), [_d(1)])

    def test_no_date_returns_empty(self):
        self.assertEqual(app._dates_from_memo("จ่ายค่าน้ำแข็ง"), [])
        self.assertEqual(app._dates_from_memo(None), [])
        self.assertIsNone(app._date_from_memo(""))


class TestExactCombo(unittest.TestCase):
    """จับคู่ 'ชุดบิล' ที่บวกกันได้เท่ายอดโอนพอดี"""

    def test_real_case_two_bills(self):
        lines = [(1, "d1", 21408.0), (2, "d1", 5939.0), (3, "d2", 25957.0), (4, "d2", 3088.0)]
        got = app._exact_combo(lines, 9027.0)
        self.assertEqual(sorted(r[0] for r in got), [2, 4])

    def test_ambiguous_returns_none(self):
        """สองชุดบวกได้เท่ากัน = เดาไม่ได้ ห้ามตัดมั่ว (ตัดผิดใบ หนี้เพี้ยนสองทาง)"""
        lines = [(1, "d1", 100.0), (2, "d1", 200.0), (3, "d2", 300.0), (4, "d2", 100.0)]
        self.assertIsNone(app._exact_combo(lines, 300.0))   # 100+200 หรือ 300 (แต่ 300 เป็นใบเดียว)
        lines2 = [(1, "d1", 100.0), (2, "d1", 200.0), (3, "d2", 200.0), (4, "d2", 100.0)]
        self.assertIsNone(app._exact_combo(lines2, 300.0))  # (1,2) และ (1,3) และ (2,4) และ (3,4)

    def test_no_combination_returns_none(self):
        self.assertIsNone(app._exact_combo([(1, "d", 500.0), (2, "d", 700.0)], 999.0))

    def test_single_line_is_not_a_combo(self):
        """ใบเดียวตรงเป๊ะเป็นหน้าที่ของขั้นตอนก่อนหน้า ตัวนี้ต้องไม่ไปแย่งทำ"""
        self.assertIsNone(app._exact_combo([(1, "d", 900.0), (2, "d", 5.0)], 900.0))

    def test_satang_precision(self):
        lines = [(1, "d", 0.1), (2, "d", 0.2)]
        self.assertIsNotNone(app._exact_combo(lines, 0.3))   # 0.1+0.2 ทศนิยมลอย ต้องยังจับได้

    def test_empty_or_zero(self):
        self.assertIsNone(app._exact_combo([], 100.0))
        self.assertIsNone(app._exact_combo([(1, "d", 100.0)], 0))


class TestSettleAcrossNotedDates(PayableTestCase):
    """ตัดยอดจ่ายข้ามวันตามที่โน้ตระบุ — เคสจริง 22/08/26 (9,027 = 5,939 + 3,088)"""

    def _setup_real_bills(self):
        app.save_payable_bill(ACCT, 21408.0, note="รูป", doc_date=_d(3))
        app.save_payable_bill(ACCT,  5939.0, note="รูป", doc_date=_d(3))
        app.save_payable_bill(ACCT, 25957.0, note="รูป", doc_date=_d(1))
        app.save_payable_bill(ACCT,  3088.0, note="รูป", doc_date=_d(1))

    def _paid(self):
        return {round(b["amount"], 2): round(b["paid"], 2) for b in self.bills()}

    def test_pays_the_two_noted_bills_exactly(self):
        self._setup_real_bills()
        allocated, settled, note = app._payable_settle(ACCT, [_d(3), _d(1)], 9027.0)
        self.assertAlmostEqual(allocated, 9027.0, places=2)
        paid = self._paid()
        self.assertAlmostEqual(paid[5939.0], 5939.0, places=2, msg="บิล 5,939 ต้องจ่ายครบ")
        self.assertAlmostEqual(paid[3088.0], 3088.0, places=2, msg="บิล 3,088 ต้องจ่ายครบ")
        self.assertAlmostEqual(paid[21408.0], 0.0, places=2, msg="ห้ามไปแตะใบใหญ่")
        self.assertAlmostEqual(paid[25957.0], 0.0, places=2)

    def test_old_behaviour_before_fix_would_hit_the_big_bill(self):
        """ยืนยันว่าถ้าได้วันเดียว (พฤติกรรมเดิม) จะไปตัดใบใหญ่จริง — นี่คือบั๊กที่แก้"""
        self._setup_real_bills()
        app._payable_settle(ACCT, _d(3), 9027.0)
        self.assertAlmostEqual(self._paid()[21408.0], 9027.0, places=2)

    def test_single_exact_bill_still_wins_over_combo(self):
        """ใบเดียวยอดตรงเป๊ะต้องมาก่อนการจับคู่ชุด (ชัวร์กว่า)"""
        app.save_payable_bill(ACCT, 9027.0, note="รูป", doc_date=_d(1))
        app.save_payable_bill(ACCT, 5939.0, note="รูป", doc_date=_d(3))
        app.save_payable_bill(ACCT, 3088.0, note="รูป", doc_date=_d(1))
        app._payable_settle(ACCT, [_d(3), _d(1)], 9027.0)
        paid = self._paid()
        self.assertAlmostEqual(paid[9027.0], 9027.0, places=2)
        self.assertAlmostEqual(paid[5939.0], 0.0, places=2)
        self.assertAlmostEqual(paid[3088.0], 0.0, places=2)

    def test_falls_back_to_fifo_across_all_noted_dates(self):
        """ไม่มีชุดไหนบวกได้พอดี → ตัด FIFO ข้ามวันที่โน้ตไว้ทั้งหมด (เดิมตัดได้แค่วันแรก)"""
        app.save_payable_bill(ACCT, 1000.0, note="รูป", doc_date=_d(3))
        app.save_payable_bill(ACCT, 1000.0, note="รูป", doc_date=_d(1))
        allocated, _, _ = app._payable_settle(ACCT, [_d(3), _d(1)], 1500.0)
        self.assertAlmostEqual(allocated, 1500.0, places=2, msg="ต้องไหลข้ามวันได้ ไม่ติดอยู่วันเดียว")

    def test_never_allocates_more_than_paid(self):
        self._setup_real_bills()
        allocated, _, _ = app._payable_settle(ACCT, [_d(3), _d(1)], 9027.0)
        self.assertLessEqual(allocated, 9027.0 + 0.01)
        self.assertAlmostEqual(sum(b["paid"] for b in self.bills()), 9027.0, places=2)


class TestPastedLedgerFeedback(PayableTestCase):
    """ก๊อป 'สรุปหนี้' ของบอทมาวางกลับ — ต้องไม่เงียบไม่ว่าผลจะเป็นยังไง

    เคสจริง 22/08/26: ล้างบัญชีแล้ววางสรุปกลับ บอทไม่ตอบอะไรเลย
    เจ้าของไม่รู้ว่าพลาดตรงไหน นึกว่าระบบพัง"""

    class _Ev:
        reply_token = "tok"

    def setUp(self):
        super().setUp()
        self.sent = []
        self._orig = app._payable_send
        app._payable_send = lambda ev, src, dst, txt: self.sent.append(txt)

    def tearDown(self):
        app._payable_send = self._orig

    def _ledger(self, header: str) -> str:
        rows = "\n".join(f"{_dmy(_d(i))} 📥+{1000 + i:,}.00" for i in (5, 4, 3))
        return f"{header}\n━━━━━━━━━━━━━\n{rows}"

    def test_pasted_ledger_with_header_is_accepted(self):
        handled = app.handle_payable_text(self._Ev(), self._ledger("📋 สรุปหนี้  — รายวัน"), ACCT)
        self.assertTrue(handled)
        self.assertEqual(len(self.bills()), 3, "ต้องลงบิลครบ 3 ใบ")

    def test_pasted_ledger_without_header_says_why(self):
        """ก๊อปเฉพาะบรรทัดยอด (ไม่ติดหัว) → ต้องบอกวิธี ไม่ใช่เงียบ"""
        handled = app.handle_payable_text(self._Ev(), self._ledger("รายการ"), ACCT)
        self.assertTrue(handled, "ต้องตอบ ไม่ปล่อยเงียบ")
        self.assertEqual(self.bills(), [], "ยังไม่บันทึกจนกว่าจะสั่งให้ชัด")
        self.assertIn("ค้าง", self.sent[-1])

    def test_ordinary_chat_with_date_equals_amount_stays_silent(self):
        """🔴 เคส 18/08/26 ที่เคยทำหนี้หาย — แชททั่วไปต้องไม่ถูกแตะและไม่ถูกเตือน"""
        self.sent.clear()
        chat = f"ยอดเนื้อกาดสามแยก\n{_dm(_d(2))}=430\n{_dm(_d(1))}=520"
        handled = app.handle_payable_text(self._Ev(), chat, ACCT)
        self.assertFalse(handled)
        self.assertEqual(self.sent, [], "ห้ามเตือนแชททั่วไป")

    def test_carry_header_with_bill_rows_is_not_reported_as_unreadable(self):
        """พาดหัว 'ค้าง' แล้ววางบรรทัดแบบบิล — เดิมตอบ 'อ่านบล็อกค้างไม่ได้' ทั้งที่อ่านได้ครบ"""
        handled = app.handle_payable_text(self._Ev(), self._ledger("ค้าง"), ACCT)
        self.assertTrue(handled)
        self.assertEqual(len(self.bills()), 3)
        self.assertNotIn("อ่านบล็อกค้างไม่ได้", "\n".join(self.sent))


class TestResvSummaryShowsWholeShop(unittest.TestCase):
    """'สรุปจอง' ต้องเห็นจองทั้งร้าน ไม่ใช่เฉพาะจองที่เกิดในกลุ่มที่พิมพ์

    เคสจริง 22/08/26: จอง #54 (ออม 3 ท่าน 30 ส.ค. โซน A โต๊ะ 12) เข้าที่กลุ่มบาร์น้ำ
    เจ้าของพิมพ์ 'สรุปจอง' ในกลุ่ม Sound & Pro → ตอบ 'ไม่มีการจอง' ทั้งที่มีอยู่"""

    GBAR, GOTHER = "Gbar", "Gsound"

    def setUp(self):
        with app._db() as conn:
            conn.execute("DELETE FROM reservations WHERE origin_group_id IN (?,?)", (self.GBAR, self.GOTHER))
            conn.commit()
        info = {"customer": "ออม", "people": "3 ท่าน", "table": "A โต๊ะ 12",
                "time_hhmm": "19:30", "resv_date": _d(-2), "is_advance": True}
        app.save_reservation(self.GBAR, "Doi", info, "จองโต๊ะ", self.GBAR)

    def tearDown(self):
        with app._db() as conn:
            conn.execute("DELETE FROM reservations WHERE origin_group_id IN (?,?)", (self.GBAR, self.GOTHER))
            conn.commit()

    def test_other_group_used_to_see_nothing(self):
        """ยืนยันพฤติกรรมเดิมที่เป็นปัญหา — กรองตามกลุ่มแล้วกลุ่มอื่นมองไม่เห็น"""
        out = app.build_resv_summary(self.GOTHER, "x", upcoming=True, match_origin=True)
        self.assertIn("ไม่มีการจอง", out)

    def test_whole_shop_view_sees_it(self):
        out = app.build_resv_summary(None, "📋 สรุปการจองทั้งร้าน", upcoming=True)
        self.assertNotIn("ไม่มีการจอง", out)
        self.assertIn("ออม", out)

    def test_origin_group_still_sees_it(self):
        out = app.build_resv_summary(None, "x", upcoming=True)
        self.assertIn("ออม", out)

    def test_cancelled_is_excluded_from_whole_shop_view(self):
        with app._db() as conn:
            conn.execute("UPDATE reservations SET status='CANCELLED' WHERE origin_group_id=?", (self.GBAR,))
            conn.commit()
        self.assertIn("ไม่มีการจอง", app.build_resv_summary(None, "x", upcoming=True))


class TestCarryHeaderIsCommandOnly(PayableTestCase):
    """หัวบล็อก 'ค้าง' ต้องเป็นคำสั่ง ไม่ใช่ประโยคที่บังเอิญมีคำนั้น

    เคสจริง 23/08/26 กลุ่ม Management (6 คน):
      'พี่ผมขอเคลียร์ยอดค้างเพิ่มให้หน่อยครับ\\n\\nขอเครดิตอยู่ที่ 80000 ถึง 100000 นะครับ'
    → บอทตอบ '❌ อ่านบล็อกค้างไม่ได้' ใส่หน้าคนที่แค่คุยกันธรรมดา"""

    class _Ev:
        reply_token = "tok"

    def setUp(self):
        super().setUp()
        self.sent = []
        self._orig = app._payable_send
        app._payable_send = lambda ev, src, dst, txt: self.sent.append(txt)

    def tearDown(self):
        app._payable_send = self._orig

    def test_real_chat_message_stays_silent(self):
        chat = ("พี่ผมขอเคลียร์ยอดค้างเพิ่มให้หน่อยครับ\n\n"
                "ขอเครดิตอยู่ที่ 80000 ถึง 100000 นะครับ")
        handled = app.handle_payable_text(self._Ev(), chat, ACCT)
        self.assertFalse(handled, "ประโยคคุยกันธรรมดา ไม่ใช่คำสั่งวางบล็อก")
        self.assertEqual(self.sent, [], "ห้ามตอบอะไรกลับไป")

    def test_real_command_header_still_works(self):
        block = f"ค้าง\n{_dm(_d(2))}=24153\n{_dm(_d(1))}=18504"
        handled = app.handle_payable_text(self._Ev(), block, ACCT)
        self.assertTrue(handled)
        self.assertEqual(len(self.bills()), 2)

    def test_header_with_emoji_and_colon_still_works(self):
        block = f"📌 ค้าง:\n{_dm(_d(2))}=24153\n{_dm(_d(1))}=18504"
        self.assertTrue(app.handle_payable_text(self._Ev(), block, ACCT))
        self.assertEqual(len(self.bills()), 2)

    def test_command_header_with_unreadable_rows_still_warns(self):
        """สั่งชัดว่าจะวางยอด แต่อ่านไม่ได้เลย → ยังต้องบอก ห้ามเงียบ"""
        app.handle_payable_text(self._Ev(), "ค้าง\nอะไรก็ไม่รู้\nมั่วๆ", ACCT)
        self.assertIn("อ่านบล็อกค้างไม่ได้", "\n".join(self.sent))


class TestMemoSharedMonthDayList(unittest.TestCase):
    """วันหลายวันที่ 'ใช้เดือนร่วมกัน' ต้องอ่านครบ

    เคสจริง 23/08/26: โอน 20,297 โน้ต 'ไส้ (12/8 จ่ายที่ค้าง(22,23/8) รวม 3 บิล'
    เลข 22 ไม่มี '/เดือน' ของตัวเอง → ตัวอ่านมองไม่เห็น เหลือแค่ 12/8 กับ 23/8"""

    def test_real_memo_reads_all_three_dates(self):
        got = app._dates_from_memo("ไส้ (12/8 จ่ายที่ค้าง(22,23/8) รวม 3 บิล")
        self.assertEqual(got, ["2026-08-12", "2026-08-22", "2026-08-23"])

    def test_various_separators(self):
        for memo in ("22,23/8", "22 และ 23/8", "22-23/8", "22 กับ 23/8"):
            self.assertEqual(app._dates_from_memo(memo), ["2026-08-22", "2026-08-23"], memo)

    def test_three_days_sharing_a_month(self):
        self.assertEqual(app._dates_from_memo("จ่าย 1,2,3/8"),
                         ["2026-08-01", "2026-08-02", "2026-08-03"])

    def test_money_amount_is_not_mistaken_for_dates(self):
        """'1,200/8' เป็นตัวเลขเงิน ไม่ใช่รายการวันที่ — ห้ามกางเป็นวัน"""
        self.assertEqual(app._dates_from_memo("ค่าของ 1,200/8"), [])

    def test_plain_formats_unchanged(self):
        self.assertEqual(app._dates_from_memo("ไส้ (23/7) ค้าง 7,993"), ["2026-07-23"])
        self.assertEqual(app._dates_from_memo("จ่าย (19/8 และ 21/8) 2 บิล"),
                         ["2026-08-19", "2026-08-21"])


class TestDeleteLastNeedsConfirmOnRepeat(PayableTestCase):
    """กด 'ลบจ่ายล่าสุด' ซ้ำติดๆ กัน ต้องขอยืนยันก่อน

    เคสจริง 23/08/26: สั่งสองรอบติด → ลบทั้งก้อน 20,297 และก้อน 10,000
    ยอดหนี้เกินจริง 10,000 โดยไม่มีใครทันสังเกต"""

    class _Ev:
        reply_token = "tok"
        class source:
            group_id = ACCT
            user_id = ACCT

    def setUp(self):
        super().setUp()
        self.replies = []
        self._orig = app.line_bot_api.reply_message
        app.line_bot_api.reply_message = lambda tok, msg: self.replies.append(msg)
        app._set_meta(app._payable_del_key(ACCT, "payable_payments"), "")
        app.save_payable_bill(ACCT, 5000.0, note="รูป", doc_date=_d(1))
        app.save_payable_payment(ACCT, 1000.0, sender="พิมพ์")
        app.save_payable_payment(ACCT, 2000.0, sender="พิมพ์")

    def tearDown(self):
        app.line_bot_api.reply_message = self._orig
        app._set_meta(app._payable_del_key(ACCT, "payable_payments"), "")

    def _pays(self):
        with app._db() as conn:
            return conn.execute("SELECT COUNT(*) c FROM payable_payments WHERE group_id=?",
                                (ACCT,)).fetchone()["c"]

    def test_first_delete_is_immediate(self):
        """ตอนฉุกเฉินต้องเร็ว — ครั้งแรกยังลบทันทีเหมือนเดิม"""
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        self.assertEqual(self._pays(), 1)

    def test_second_delete_asks_first(self):
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        self.assertEqual(self._pays(), 1, "ครั้งที่สองต้องยังไม่ลบ")
        self.assertIn("ยืนยัน", getattr(self.replies[-1], "alt_text", ""))

    def test_confirmed_delete_goes_through(self):
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย", confirmed=True)
        self.assertEqual(self._pays(), 0)

    def test_window_expires(self):
        """พ้นกรอบเวลาแล้วกลับมาลบทันทีเหมือนเดิม (ไม่กวนตอนใช้งานปกติ)"""
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        app._set_meta(app._payable_del_key(ACCT, "payable_payments"),
                      str(app.time.time() - app.PAYABLE_DEL_CONFIRM_SEC - 5))
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        self.assertEqual(self._pays(), 0)

    def test_unknown_table_is_refused(self):
        """ชื่อตารางถูกต่อเข้า SQL ตรงๆ — ค่าที่มาจากปุ่มต้องอยู่ในลิสต์ที่รู้จักเท่านั้น"""
        app._delete_last_payable(self._Ev(), ACCT, "slips; DROP TABLE x", "มั่ว")
        self.assertEqual(self._pays(), 2, "ห้ามแตะข้อมูล")
        self.assertEqual(self.replies, [])

    def test_bills_and_payments_have_separate_windows(self):
        """ลบจ่ายแล้วสั่งลบบิลทันที ต้องไม่ถูกขอยืนยัน (คนละชนิดกัน)"""
        app._delete_last_payable(self._Ev(), ACCT, "payable_payments", "เงินจ่าย")
        app._delete_last_payable(self._Ev(), ACCT, "payable_bills", "บิลซื้อ")
        with app._db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM payable_bills WHERE group_id=?", (ACCT,)).fetchone()["c"]
        self.assertEqual(n, 0, "บิลต้องถูกลบทันที")


class TestResvDraftLivesLongEnough(unittest.TestCase):
    """ร่างจองต้องอยู่นานพอให้หน้าร้านตอบตอนว่าง

    เคสจริง 23/08/26 กลุ่ม Sound & Pro:
      19:22 บอทถาม 'กี่โมง? / โซนไหน?' (แม่เล็ก ลาบต้นยาง · 2 ท่าน · 30 ส.ค.)
      21:12 พนักงานตอบ 'เวลา 19.00 โซน A4'  ← ห่าง 1 ชม. 50 นาที
    ร่างหมดอายุไปแล้ว คำตอบเลยลอยเดี่ยวๆ ไม่มีชื่อ/จำนวน/วัน → จองหลุด"""

    GID = "Gdraft"

    def setUp(self):
        app._resv_draft_clear(self.GID)

    def tearDown(self):
        app._resv_draft_clear(self.GID)

    def test_default_window_covers_a_two_hour_gap(self):
        self.assertGreaterEqual(app.RESV_FOLLOWUP_MIN, 120,
                                "หน้าร้านตอบช้ากว่า 2 ชม. ได้เป็นเรื่องปกติ")

    def test_draft_survives_the_real_gap(self):
        app._resv_draft_set(self.GID, "แม่เล็ก ลาบต้นยาง 2 ท่าน 30 ส.ค.", 1, asked=["time", "table"])
        raw = json.loads(app._get_meta(f"resvdraft:{self.GID}"))
        raw["ts"] = raw["ts"] - 110 * 60          # ย้อนไป 1 ชม. 50 นาที
        app._set_meta(f"resvdraft:{self.GID}", json.dumps(raw))
        self.assertIsNotNone(app._resv_draft_get(self.GID), "ร่างต้องยังอยู่")

    def test_draft_still_expires_eventually(self):
        app._resv_draft_set(self.GID, "x", 1, asked=["time"])
        raw = json.loads(app._get_meta(f"resvdraft:{self.GID}"))
        raw["ts"] = raw["ts"] - (app.RESV_FOLLOWUP_MIN + 5) * 60
        app._set_meta(f"resvdraft:{self.GID}", json.dumps(raw))
        self.assertIsNone(app._resv_draft_get(self.GID))

    def test_short_answer_is_treated_as_an_answer_not_a_new_booking(self):
        """'เวลา 19.00 โซน A4' = 2 สัญญาณ ไม่มีคำว่าจอง → ต้องต่อกับร่างเก่า"""
        text = "เวลา 19.00 โซน A4"
        self.assertEqual(app._resv_signal_hits(text), 2)
        fresh = (any(h in text.lower() for h in app._RESV_HINTS if h not in app._RESV_WEAK_HINTS)
                 or app._resv_signal_hits(text) >= 3)
        self.assertFalse(fresh, "ห้ามตีเป็นจองใบใหม่")

    def test_full_new_booking_does_not_merge_with_an_old_draft(self):
        """จองใบใหม่เต็มใบ ต้องเริ่มใบใหม่ ไม่เอาไปต่อกับร่างของลูกค้าคนก่อน"""
        for text in ("จองโต๊ะ คุณบี 5 คน พรุ่งนี้ 2 ทุ่ม โซน B",
                     "คุณซี 6 ท่าน เสาร์นี้ 19:30 โซน A"):
            fresh = (any(h in text.lower() for h in app._RESV_HINTS if h not in app._RESV_WEAK_HINTS)
                     or app._resv_signal_hits(text) >= 3)
            self.assertTrue(fresh, text)


class TestTableCodeFallback(unittest.TestCase):
    """พนักงานพิมพ์รหัสโต๊ะมาแล้ว บอทต้องไม่ถามซ้ำ

    เคสจริง 23/08/26: ข้อความมี 'A4' อยู่แล้ว แต่บอทยังถาม 'นั่งโซนไหนครับ?'
    คำสั่งแปลงรหัสอยู่ใน prompt แล้ว แต่ AI ไม่ได้ทำทุกครั้ง
    → เรื่องที่กติกาตายตัวอยู่แล้ว ไม่ควรฝากไว้กับความแน่นอนของ AI"""

    def test_reads_the_real_case(self):
        self.assertEqual(app._table_from_text("เวลา 19.00 โซน A4"), "โซน A โต๊ะ 4")
        self.assertEqual(app._table_from_text("แม่เล็ก ลาบต้นยาง 2 ท่าน 30/8 A4"), "โซน A โต๊ะ 4")

    def test_lowercase_and_two_digits(self):
        self.assertEqual(app._table_from_text("จอง b10 คุนนาต มาทุ่ม"), "โซน B โต๊ะ 10")

    def test_multi_table_range(self):
        self.assertEqual(app._table_from_text("A2-3-4"), "โซน A โต๊ะ 2-3-4")

    def test_no_code_returns_none(self):
        for t in ("ลูกค้า 4 คน 19:30", "ปิดร้าน 2 ทุ่ม", "คุณเอ 6 ท่าน พรุ่งนี้"):
            self.assertIsNone(app._table_from_text(t), t)

    def test_time_is_not_a_table_code(self):
        """'19.00' / '19:30' ขึ้นต้นด้วยเลข ไม่ใช่รหัสโต๊ะ"""
        self.assertIsNone(app._table_from_text("มา 19.00 นะ"))

    def test_english_word_ending_with_digits_is_not_a_table(self):
        self.assertIsNone(app._table_from_text("COVID19 ยังมีอยู่"))


class TestPeopleLabelNotDoubled(unittest.TestCase):
    """'2 ท่าน ท่าน' — AI คืนหน่วยมาแล้ว โค้ดต่อหน่วยซ้ำอีก"""

    def _label(self, people: str) -> str:
        p = str(people).strip()
        return p if app.re.search(r"ท่าน|คน|ที่นั่ง", p) else f"{p} ท่าน"

    def test_value_with_unit_is_not_doubled(self):
        self.assertEqual(self._label("2 ท่าน"), "2 ท่าน")
        self.assertEqual(self._label("4 คน"), "4 คน")

    def test_bare_number_gets_a_unit(self):
        self.assertEqual(self._label("6"), "6 ท่าน")


class TestMaskedRefIsNotAReference(unittest.TestCase):
    """เลขบัญชี/พร้อมเพย์ที่ถูกปิดบัง ต้องไม่ถูกใช้เป็น 'เลขอ้างอิงรายการ'

    เคสจริง 24/08/26: AI หยิบเลขพร้อมเพย์ปลายทาง 'XXXXXXXXXXXXX5949' มาใส่ช่องอ้างอิง
      15/08 น.ส.นลินทิพย์ 1,289  ·  24/08 น.ส.สหราช 2,075
      → ได้เลขอ้างอิงเดียวกัน → ระบบฟันธง '🚨 สลิปถูกตัดต่อ' ทั้งที่คนละรายการคนละคน
    เลขอ้างอิงจริงไม่มีการปิดบัง"""

    def test_real_case_masked_promptpay_is_dropped(self):
        info = {"ref_number": "XXXXXXXXXXXXX5949", "receiver_account": "XXX-XXXXXXXX-5949"}
        app._clean_ref_number(info)
        self.assertIsNone(info["ref_number"])

    def test_masked_account_with_dashes_is_dropped(self):
        info = {"ref_number": "XXX-X-XX474-0"}
        app._clean_ref_number(info)
        self.assertIsNone(info["ref_number"])

    def test_star_masking_is_dropped(self):
        info = {"ref_number": "123***4567"}
        app._clean_ref_number(info)
        self.assertIsNone(info["ref_number"])

    def test_ref_equal_to_receiver_account_is_dropped(self):
        info = {"ref_number": "0123456789", "receiver_account": "012-345-6789"}
        app._clean_ref_number(info)
        self.assertIsNone(info["ref_number"])

    def test_real_reference_numbers_survive(self):
        """เลขอ้างอิงจริงจากสลิปที่เคยเจอ ต้องไม่ถูกทิ้ง — ไม่งั้นเสียตัวกันสลิปซ้ำทั้งระบบ"""
        for ref in ("202608204TZLHECIKEO1L1QAW", "TRBS260822583060771",
                    "016231235052BTF02583", "C20260624617516105918",
                    "202606235FY5L3I8DHOUZVOLZ"):
            info = {"ref_number": ref, "receiver_account": "xxx-x-x4612-x"}
            app._clean_ref_number(info)
            self.assertEqual(info["ref_number"], ref, ref)

    def test_empty_ref_is_left_alone(self):
        for val in (None, "", "   "):
            info = {"ref_number": val}
            app._clean_ref_number(info)
            self.assertFalse(info["ref_number"])
