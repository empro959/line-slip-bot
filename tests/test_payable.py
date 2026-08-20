"""เทสต์ regression ของ 'บัญชีเจ้าหนี้การค้า' (ระบบที่ 3) + ตัวแปลง SQL Postgres

ทำไมต้องมี: ส่วนนี้คือ 'ตัวเลขเงิน' ที่เคยพังจริงมาแล้วหลายรอบ —
  • ยอดค้างติดลบ 128,206 → −194,437 (reconcile ทิ้ง orphan)
  • _cleanup_old_data() พังเงียบทุกวันบน Postgres (ลืม escape '%')
  • จ่ายไปตัดผิดใบ (วันที่บนสลิปชนะยอดที่ตรงเป๊ะ)
เคสพวกนี้ตาเปล่าไม่เห็น ต้องมีเทสต์ยึงไว้

วิธีรัน:  python3 -m unittest discover -s tests -v
ไม่ต้องมี DB จริง/คีย์จริง — ใช้ SQLite ไฟล์ชั่วคราว และคีย์ปลอม (ไม่ยิงเน็ตออก)
"""
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
    """_payable_push_summary — reply token หมดอายุ (Gemini อ่านรูปนาน) ต้อง fallback push
    ไม่งั้นสรุปหนี้หายเงียบ (เคสจริง: ส่งบิลแล้วสรุปไม่เด้ง)"""

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

        app._payable_push_summary(self._Event(), ACCT, ACCT)

        self.assertIn(ACCT, self.pushed, "reply พังแล้วต้อง push กลุ่มต้นทางแทน")
        self.assertIn(MIRROR, self.pushed, "กลุ่ม mirror ต้องได้สรุปด้วย")

    def test_happy_path_uses_free_reply_for_source_group(self):
        """ปกติ (reply ไม่พัง): กลุ่มต้นทางใช้ reply ฟรี ไม่กินโควตา push"""
        app.line_bot_api.reply_message = self._ok_reply
        app.save_payable_bill(ACCT, 100.0, note="รูป", doc_date=_d(0))

        app._payable_push_summary(self._Event(), ACCT, ACCT)

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


class TestResvFollowupWindow(unittest.TestCase):
    """พนักงานพิมพ์จองใหม่หลังบอทถามข้อมูลที่ขาด — ต้องไม่หลุด

    เคสจริง 19/08/26 กลุ่ม Staff: บอทถามหา 'เวลา' ตอน 15:15 → พนักงานพิมพ์ใหม่ครบทุกอย่าง
    ตอน 15:19 แต่ไม่มีคำว่า 'จอง' → ด่านคำใบ้ตัดทิ้งเงียบ จองวันที่ 20/8 หายทั้งใบ"""

    def setUp(self):
        app._set_meta("resvwait:Gstaff", "0")

    def test_window_closed_by_default(self):
        self.assertFalse(app._resv_followup_open("Gstaff"))

    def test_window_opens_after_bot_asked(self):
        app._set_meta("resvwait:Gstaff", str(int(app.time.time())))
        self.assertTrue(app._resv_followup_open("Gstaff"))

    def test_window_expires(self):
        """หน้าต่างต้องปิดเอง ไม่ค้างยิง AI ให้ทุกข้อความที่มีตัวเลขทั้งวัน"""
        stale = int(app.time.time()) - (app.RESV_FOLLOWUP_MIN * 60 + 60)
        app._set_meta("resvwait:Gstaff", str(stale))
        self.assertFalse(app._resv_followup_open("Gstaff"))

    def test_bad_value_does_not_crash(self):
        app._set_meta("resvwait:Gstaff", "ไม่ใช่ตัวเลข")
        self.assertFalse(app._resv_followup_open("Gstaff"))

    def test_real_message_that_was_dropped_has_no_hint_word(self):
        """ยืนยันสาเหตุ: ข้อความจริงที่หลุด ไม่มีคำใบ้สักคำ แต่มีตัวเลข (เงื่อนไขที่ใช้เปิดทาง)"""
        real = "คุณยานา จำนวน 12 คน เวลา 19.30 0826062980 โซน A2-3-4 วันที่ 20/8/69 @All"
        self.assertFalse(any(h in real.lower() for h in app._RESV_HINTS))
        self.assertTrue(any(c.isdigit() for c in real))


if __name__ == "__main__":
    unittest.main(verbosity=2)
