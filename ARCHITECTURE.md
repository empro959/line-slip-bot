# ARCHITECTURE — line-slip-bot ("เอด" / @lza4817e)

LINE bot สำหรับร้าน **ไส้ย่างซอย๔ (E&M)** — Flask ไฟล์เดียว (`app.py`) รันด้วย gunicorn บน Render, เก็บข้อมูลใน PostgreSQL

## ภาพรวม 3 ระบบ
- **A. ตรวจสลิป** — อ่านสลิปโอนเงินด้วย Gemini AI, จับปลอม/ซ้ำ/โอนผิดบัญชี/ครอป, รายงานสรุป 00:30
- **B. จองโต๊ะ** — AI จับข้อความจอง → การ์ดปุ่มคอนเฟิร์ม, จองล่วงหน้าส่งกลุ่มบาร์น้ำ; เตือนซ้ำ+สรุป 16:00 = 'เฉพาะจองวันนี้' (จองล่วงหน้าแจ้งเฉพาะวันงาน; ดูล่วงหน้าได้ที่คำสั่ง 'สรุปจอง')
- **C. บัญชีเจ้าหนี้การค้า (ดวงใจการสุรา)** — ในกลุ่ม `PAYABLE_GROUPS` เท่านั้น: AI แยกรูป "บิลซื้อ"(+หนี้)/"สลิปโอนจ่าย"(−หนี้), ตั้งยอดค้างยกมาครั้งเดียว, สรุปยอดค้างรายวันอัตโนมัติ ตี1 (01:00). กลุ่มนี้ **ไม่** ทำระบบ A/B (route แยกตั้งแต่ image/text handler)
- **D. กระทบบิล-สลิป ลูกค้า (โอนขาด/เกิน)** — เฉพาะกลุ่มใน `DINING_GROUPS` (เป็น subset ของ SLIP_GROUPS): เทียบ "ยอดบิลร้าน (ใบแจ้งรายการ)" กับ "ยอดสลิปที่ลูกค้าโอน". รูปเดียว(บิล+สลิป)=เทียบตรง / แยกรูป=จับคู่บิลค้างเก่าสุด FIFO ตามเวลา (สลิปไม่มีเลขโต๊ะให้ยึด — พีคอาจจับคู่พลาด). โอนเกิน→สะสมเข้า 'ยอดทริป' เงียบ / โอนขาด ≥ `DINING_SHORT_BAHT`(1)→เตือน + **เด้งปุ่มเคลียร์เฉพาะโต๊ะนั้น** (postback `dining_clear:{pair_id}` → มาร์ค `dining_pairs.collected=1` + ตัดยอดขาดคืนเข้ายอดสะสม). ปิดยอดทั้งทริปเมื่อพนักงานกดปุ่ม/พิมพ์ 'เก็บครบแล้ว' (postback `dining_confirm`). ต่อท้ายสรุปบิล-สลิปในรายงานรายวัน (00:30). ทำงาน *เพิ่มเติม* บนระบบ A (ไม่ route แยก — บิลร้านล้วนเข้าคิวแทนที่จะนับเป็น notslip)

## Stack
- **Web:** Flask + gunicorn (`gunicorn.conf.py`: 1 worker / 8 threads / timeout 120) — Render รัน `gunicorn app:app` (ต้องมี `gunicorn.conf.py` เพราะไม่อ่าน Procfile)
- **AI:** Gemini ผ่าน `google-genai` SDK (model `gemini-2.5-flash`) — แบบ paid billing
- **DB:** PostgreSQL (managed บน Render) — ตั้งผ่าน env `DATABASE_URL`; ถ้าไม่ตั้ง fallback เป็น SQLite (local)
- **LINE:** line-bot-sdk v3 (ใช้ API v2 compat: `linebot`, `linebot.models`)
- **Host:** Render Starter (ไม่หลับ), ping `/health` ทุก 5 นาทีด้วย UptimeRobot

## Storage layer (สำคัญ — เคยเป็นจุดเจ็บ)
- `_db()` = **connect ตรงทุกครั้ง** (Postgres เชื่อมเร็ว), สร้างตารางครั้งเดียวด้วย `_ensure_init()` (idempotent)
- `_Conn` wrapper รองรับทั้ง Postgres/SQLite ด้วยโค้ดชุดเดียว: แปลง `?`→`%s`, `insert_returning_id` (RETURNING vs lastrowid)
- **ห้าม** กลับไปใช้ background-thread + Event gating (`_storage_ready`) — มันค้างข้าม worker restart → "storage ยังไม่พร้อม" ทั้งระบบ (บทเรียนวันที่ migrate)
- เดิมเคยใช้ Render Persistent Disk → mount ไม่เสถียร (ช้า/ไม่ขึ้น) ข้อมูลหาย → ย้ายมา Postgres

## ตาราง (PostgreSQL)
- **slips** — สลิปที่อ่านได้ (group_id, slip_date, sender, amount, bank, ref_number, slip_datetime, verdict, recorded_at)
- **image_misses** — รูปที่รับแต่ไม่เป็นสลิป (กระทบยอด "รับรูป/อ่านได้/ตกหล่น")
- **reservations** — การจอง (customer, people, resv_datetime, table_no, resv_date[YYYY-MM-DD], status, notify_group_id, reminded_at, confirmed_by/at)
- **groups** — group_id ที่บอทเคยเจอ (ไว้ส่งรายงาน)
- **payable_bills** — บิลซื้อ/ยอดค้างยกมา (group_id, doc_date, amount, note, recorded_at, **paid**) → เพิ่มหนี้
  - บล็อก `ค้าง` ลงเป็นบิลรายวัน `note='ยอดค้างยกมา'` (กระจายทุกบรรทัดในรายงาน) — วางบล็อกใหม่ = ลบ carry เดิม + ล้าง opening แล้วลงใหม่
  - `paid` = ยอดที่สลิปจ่ายมา "ตัด" บรรทัดนี้ไปแล้ว (ไม่ลบบรรทัดทันที — โชว์ `✅ จ่ายครบ`/`จ่าย X เหลือ Y` ในสรุปรอบที่จ่าย แล้ว `_payable_cleanup_paid` ลบบรรทัดที่จ่ายครบหลังเด้งสรุป → รอบถัดไปหาย)
- **payable_payments** — เงินที่จ่ายเจ้าหนี้ (..., **allocated**, **settle_note**) → ลดหนี้ (กันซ้ำด้วย ref_number)
  - `allocated` = ส่วนของยอดจ่ายที่ถูกตัดเข้า `paid` ของบรรทัดบิล/ค้าง (Σ paid บิล = Σ allocated จ่าย เป็นเงินก้อนเดียวกัน); ส่วนที่เหลือ (amount−allocated) ลดยอดรวมตรงๆ
- ⚠️ **ห้าม cleanup ตาราง payable_payments / บรรทัดที่ยังค้าง** — เป็นบัญชีเดินสะสม (ลบได้เฉพาะบิลที่ `จ่ายครบ` ผ่าน `_payable_cleanup_paid`)
- **เด้งสรุปเมื่อจ่าย (ไม่ใช่ตี1):** มี 'สลิปจ่าย' → `_payable_push_summary` เด้งสรุป (กลุ่ม1 ไม่มียอดรวม / กลุ่ม2 มียอดรวม) แล้ว `_payable_cleanup_paid`. 'บิลซื้อ' = บันทึกเงียบ ไม่เด้งสรุป. `maybe_send_payable_summary` ปิดการใช้งานแล้ว
- **dining_bills** — บิลร้าน (ใบแจ้งรายการ) รอจับคู่กับสลิป (group_id, bill_date, table_no, amount, recorded_at, message_id, **matched** 0=รอ/1=จับคู่หรือรีเซ็ต/2=หมดอายุ). ระบบ D เท่านั้น
- **dining_pairs** — ผลจับคู่บิล↔สลิป (group_id, pair_date, table_no, bill_amount, slip_amount, **diff** [สลิป−บิล, ลบ=ขาด], recorded_at, source ['รูปเดียว'/'จับคู่เวลา'], **collected** [โอนขาดที่เก็บส่วนต่างครบ=เคลียร์โต๊ะนั้น → ตัดออกจากยอดสะสม]). ระบบ D
- **meta** — `payable_opening:{group}` (ยอดยกมาก้อนเดียว เฉพาะคำสั่ง `ตั้งยอดยกมา`), `dining_balance:{group}` (ยอดสะสมทริป ระบบ D — สะสมข้ามวันจนพิมพ์ 'เก็บครบแล้ว' รีเซ็ต), `left:{group}`, ฯลฯ
- **สูตรยอดค้าง:** `ค้างสะสม = payable_opening + Σ(bills.amount − bills.paid) − Σ(payments.amount − payments.allocated)` = `opening + Σบิล − Σจ่าย` (Σpaid=Σallocated หักกันพอดี ไม่นับซ้ำ)

## งานเบื้องหลัง (daemon threads)
- `_report_backup_loop` — เรียก `maybe_send_daily_report()` ทุก 5 นาที (สำรองจาก /health ping)
- `maybe_send_daily_report` — หลัง 00:30 วันละครั้ง (idempotent ด้วย meta.last_report_date): รายงานสลิป→SLIP_GROUPS, แล้ว `_cleanup_old_data()` (สรุปจองล่วงหน้าย้ายไป 16:00 แล้ว)
- `maybe_send_resv_summary` — กรอบ 16:00–18:59 วันละครั้ง: สรุปจอง 'เฉพาะวันนี้' (จองล่วงหน้าโผล่เมื่อ resv_date=วันนี้) → BAR_GROUP + RESV_GROUPS; ไม่มีจอง=ไม่ส่ง
- `maybe_send_payable_summary` — หลัง `PAYABLE_SUMMARY_HOUR` (ดีฟอลต์ ตี1) วันละครั้ง (idempotent ด้วย meta.last_payable_summary_date + รายกลุ่ม): สรุปหนี้ "เมื่อวาน"→PAYABLE_GROUPS
- `_reservation_reminder_loop` — เตือนจอง PENDING ทุก 5 นาที (18-22น.)/15 นาที (เวลาอื่น) จนคอนเฟิร์ม/เกิน RESV_NAG_MAX_HOURS
- รูปสลิป: ประมวลผลผ่าน `_slip_pool` (ThreadPoolExecutor, SLIP_WORKERS=2) กัน burst ทำ memory พุ่ง/OOM
- **กันสแปมเมื่อบอทถูกเตะออกกลุ่ม:** ส่งรายงาน/สรุปแบบ idempotent "รายกลุ่ม" (กลุ่มที่ส่งสำเร็จแล้วไม่ส่งซ้ำ retry เฉพาะที่พลาด) + `LeaveEvent`→มาร์ค `left:{group}` + push เจอ 400 ("ไม่ใช่สมาชิก") → auto-prune มาร์ค left เอง + `JoinEvent`/มีข้อความเข้ามา → ปลดมาร์ค (self-heal)

## คำสั่งในแชท
- `help`/`คำสั่ง` — เมนู • `groupid` — ดู Group ID • `โควต้า` — ดู push ที่ใช้เดือนนี้ (แยกราย OA)
- สลิป: `สรุป`, `สรุป YYYY-MM-DD`, `รายงานเมื่อวาน`, `ลบล่าสุด`/`ลบ N`, `ล้างวันนี้`
- จอง: พิมพ์ประโยคจอง (AI จับ) → ต้องครบ ชื่อ/จำนวนคน/วันเวลา(มั่นใจ)/โซน → กดปุ่มคอนเฟิร์ม • `สรุปจอง`
- บัญชีหนี้ (เฉพาะ PAYABLE_GROUPS): ส่งรูปบิล/สลิป (AI แยกเอง) • `บิล 3500` (บันทึกด้วยเลข) • `ตั้งยอดยกมา 12000` • `สรุปหนี้`/`สรุปหนี้ YYYY-MM-DD` • `ลบบิลล่าสุด`/`ลบจ่ายล่าสุด`
- กระทบบิล-สลิป (เฉพาะ DINING_GROUPS): ส่งรูปบิลร้าน+สลิป (AI แยกเอง) • โอนขาด→เด้ง **ปุ่มเคลียร์เฉพาะโต๊ะนั้น** (เก็บส่วนต่างครบแล้วกด → ตัดยอดขาดโต๊ะนั้นออก) • `ยอดสะสม`/`ยอดทริป` (ดูยอด + โต๊ะที่ยังไม่เคลียร์ + การ์ดปุ่มปิดยอดทริป) • `เก็บครบแล้ว`/`ปิดยอด` หรือกดปุ่มปิดยอดทริป (postback `dining_confirm`) → รีเซ็ตทั้งทริปเป็น 0 (สิ้นวัน). กันกดซ้ำทุกปุ่มด้วย `_postback_once`
- `คู่มือ` — บอทส่งคู่มือย่อ • `ล้างทั้งหมด` — ล้างข้อมูลกลุ่มนี้ทั้งหมด (สลิป+จอง ทุกวัน, มีปุ่มยืนยัน)

## ENV vars (ตั้งที่ Render — repo เป็น public ห้าม hardcode)
- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_ADMIN_USER_ID`
- `GEMINI_API_KEY`, `GEMINI_MODEL` (ดีฟอลต์ gemini-2.5-flash)
- `DATABASE_URL` (Postgres — ต้อง region เดียวกับ web service)
- `SLIP_GROUPS`, `RESV_GROUPS`, `RESV_EXCLUDE_GROUPS`, `BAR_GROUP_ID`, `PAYEE_KEYWORDS`
- `IGNORE_GROUPS` — กลุ่มที่ "บอทเมินทั้งหมด" (ไม่เช็คสลิป/ไม่จอง/ไม่ตอบคำสั่ง/ไม่ส่งรายงาน-เตือน) ใช้กับกลุ่มที่เลิกใช้
- `PAYABLE_GROUPS` — กลุ่มบัญชีเจ้าหนี้ (เช่น กลุ่มดวงใจ); `PAYABLE_VENDOR` (ดีฟอลต์ "ดวงใจการสุรา"); `PAYABLE_SUMMARY_HOUR` (ดีฟอลต์ 1 = ตี1)
- `REPORT_REDIRECT` — ส่งรายงานของกลุ่มต้นทางไปเข้ากลุ่มปลายทางแทน (เนื้อหายังเป็นของต้นทาง) ครอบทุกรายงาน (สลิป/จอง/หนี้); รูปแบบ `ต้นทาง:ปลายทาง,ต้นทาง2:ปลายทาง2`
- `PAYABLE_MIRROR` — บัญชีหนี้แบบ "บัญชีเดียว 2 กลุ่ม"; รูปแบบ `primary:mirror`. primary=กลุ่มทำงาน (ส่งบิล/สลิป + บอท**ตอบยืนยันรายตัวด้วย reply ฟรี**ที่นี่) + เก็บข้อมูลจริง, mirror(กลุ่ม 2)=รับ **เฉพาะสรุปรายวัน** (ตี1) ไม่เด้งทุกบิล/สลิป (ประหยัดโควต้า push). ใช้ข้อมูลชุดเดียวกัน. ทั้ง 2 กลุ่มต้องอยู่ใน `PAYABLE_GROUPS` ด้วย
- `DINING_GROUPS` — กลุ่มที่เปิดระบบ D (กระทบบิล-สลิป โอนขาด/เกิน); เว้นว่าง=ปิด (บิลร้านนับเป็น notslip ตามเดิม). ต้องอยู่ใน SLIP_GROUPS ด้วย
- `DINING_SHORT_BAHT`(1) — เตือนเมื่อโอน 'ขาด' ตั้งแต่กี่บาทขึ้นไป (≥); `DINING_MATCH_MIN`(45) — บิลค้างรอจับคู่สลิปได้นานกี่นาที (เกิน=หมดอายุ); `DINING_MATCH_DELAY`(90) — หน่วงวินาทีก่อนจับคู่บิล-สลิป (รอบิลที่ส่งไล่ๆ กันถูกอ่านเข้าคิวครบ กันจับคู่สลับลำดับ; โอนขาดเตือนแบบ push)
- `RESV_NAG_MAX_HOURS`(6), `RESV_REPORT_DAYS`(7), `RESV_KEEP_DAYS`(15), `MISS_KEEP_DAYS`(14), `SLIP_KEEP_DAYS`(60), `SLIP_WORKERS`(2)
- `SLIP_WARN_UNREAD`(1) — เตือนในกลุ่มเมื่อบอท "อ่านออกแต่ไม่ใช่สลิป" (notslip); ตั้ง 0 เพื่อปิดถ้ารก. หมายเหตุ: ระบบ "อ่านซ้ำอัตโนมัติด้วย pro" 1 รอบเมื่อรอบแรกอ่านไม่ออก/ไม่ใช่สลิป/ยอด≤0 **หรือ flash โยน error** (กู้ใบยาก/ถ่ายจอ + กัน Gemini ล้มชั่วคราวช่วงพีค)
- **เคส error (อ่านไม่สำเร็จจริง):** ดาวน์โหลดรูป/อ่านพังทั้ง flash+pro/บันทึกพลาด → **เงียบในกลุ่ม + เตือนเฉพาะแอดมิน** (ไม่เด้ง "ส่งสลิปใหม่" กวนกลุ่มอีก — รูปที่พลาดยังนับใน "ตกหล่น" เห็นได้ที่ `สรุป`)
- `PROMPTPAY_API_KEY` (SlipOK — ยังไม่เปิด; เปิดได้เพื่อตรวจกับธนาคารจริง 100%)
- **Failover หลาย OA (ประหยัดค่า push):** `LINE_CHANNEL_ACCESS_TOKEN_2` (`_3`.._5 ได้) + `PUSH_FREE_LIMIT`(300). reply ฟรีไม่นับ; push กินโควต้าฟรี ~300/เดือน/OA. `_push()` ใช้ OA1 จนนับถึง `PUSH_FREE_LIMIT` เดือนนี้แล้วสลับ OA2/3.. อัตโนมัติ (นับแยกรายเดือนใน meta `push_count[N]:YYYY-MM` reset เองขึ้นเดือนใหม่); เจอ 429 (เต็มจริง) ก็สลับตัวถัดไปทันทีกันตกหล่น; 400 not-member ยัง re-raise ให้ call site มาร์ค left เหมือนเดิม. OA สำรองต้องเชิญเข้า**ทุกกลุ่ม**เหมือน OA1 + ปิด auto-reply/webhook (push อย่างเดียว). ดูยอดที่ใช้: พิมพ์ `โควต้า` ในแชท

## ข้อควรรู้ในการดูแล (ops)
- ⚠️ **Render Postgres ฟรีถูกลบหลัง 90 วัน** → ต้อง upgrade เป็น paid หรือย้ายข้อมูลก่อนครบ
- **อย่า deploy ตอนร้านเปิด** — ทุก deploy worker restart ~1 นาที สลิป/จองช่วงนั้นเสี่ยงหลุด
- **pin เวอร์ชันใน requirements.txt** แล้ว — กัน auto-upgrade ทำพัง (อัปเดตเมื่อทดสอบแล้วเท่านั้น)
- ดูสถานะ: `<render-url>/health` → `{status, slips_today, active_groups, storage, memory_mb}`
- **เฝ้า memory:** `_check_memory()` (เรียกจาก /health ping + backup loop ทุก ~5 นาที) — RSS เกิน `MEM_WARN_MB`(430) → DM เตือนแอดมิน (จำกัด 1 ครั้ง/30 นาที) กันก่อน OOM/restart

## ข้อจำกัดที่รู้อยู่
- AI อ่านสลิปจากรูป ~98% (1-2 ใบ/วันอาจพลาด) — 100% ต้องเปิด SlipOK API
- จองล่วงหน้า: AI คำนวณ resv_date จาก "วันนี้" ที่ inject — เคสกำกวมจะถามวันที่กลับ
