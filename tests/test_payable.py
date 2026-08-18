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
        self.assertNotIn("❌", msg, "บรรทัดบิล 📥 ไม่ใช่ error ให้ข้ามเฉยๆ")
        self.assertAlmostEqual(app._payable_outstanding(ACCT), 48237.0, places=2)
        self.assertEqual(len(self.bills()), 3, "รับเฉพาะบรรทัด 'ยกมา' ไม่เอาบรรทัดบิล")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
