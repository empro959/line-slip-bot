# ARCHITECTURE — line-slip-bot ("เอด" / @lza4817e)

LINE bot สำหรับร้าน **ไส้ย่างซอย๔ (E&M)** — Flask ไฟล์เดียว (`app.py`) รันด้วย gunicorn บน Render, เก็บข้อมูลใน PostgreSQL

## ภาพรวม 3 ระบบ
- **A. ตรวจสลิป** — อ่านสลิปโอนเงินด้วย Gemini AI, จับปลอม/ซ้ำ/โอนผิดบัญชี/ครอป, รายงานสรุป 00:30
- **B. จองโต๊ะ** — AI จับข้อความจอง → การ์ดปุ่มคอนเฟิร์ม, จองล่วงหน้าส่งกลุ่มบาร์น้ำ; ตื๊อคอนเฟิร์มทุกจอง (วันนี้+ล่วงหน้า) ตั้งแต่รับจอง; รายงาน 2 รอบ: 11:00 = จองล่วงหน้าที่ถึงวันงานวันนี้ (โผล่เฉพาะวันงาน) / 16:00 = จองในวัน (จองวันนี้เพื่อวันนี้); ดูล่วงหน้าทั้งหมดได้ที่คำสั่ง 'สรุปจอง'
- **C. บัญชีเจ้าหนี้การค้า (ดวงใจการสุรา)** — ในกลุ่ม `PAYABLE_GROUPS` เท่านั้น: AI แยกรูป "บิลซื้อ"(+หนี้)/"สลิปโอนจ่าย"(−หนี้), ตั้งยอดค้างยกมาครั้งเดียว, สรุปยอดค้างรายวันอัตโนมัติ ตี1 (01:00). กลุ่มนี้ **ไม่** ทำระบบ A/B (route แยกตั้งแต่ image/text handler)
- **D. กระทบบิล-สลิป ลูกค้า (โอนขาด/เกิน)** — เฉพาะกลุ่มใน `DINING_GROUPS` (เป็น subset ของ SLIP_GROUPS): เทียบ "ยอดบิลร้าน (ใบแจ้งรายการ)" กับ "ยอดสลิปที่ลูกค้าโอน". รูปเดียว(บิล+สลิป)=เทียบตรง / แยกรูป=จับคู่บิลค้างเก่าสุด FIFO ตามเวลา (สลิปไม่มีเลขโต๊ะให้ยึด — พีคอาจจับคู่พลาด). โอนเกิน→สะสมเข้า 'ยอดทริป' เงียบ / โอนขาด ≥ `DINING_SHORT_BAHT`(1)→เตือน + **เด้งปุ่มเคลียร์เฉพาะโต๊ะนั้น** (postback `dining_clear:{pair_id}` → มาร์ค `dining_pairs.collected=1` + ตัดยอดขาดคืนเข้ายอดสะสม). ปิดยอดทั้งทริปเมื่อพนักงานกดปุ่ม/พิมพ์ 'เก็บครบแล้ว' (postback `dining_confirm`). ต่อท้ายสรุปบิล-สลิปในรายงานรายวัน (00:30). ทำงาน *เพิ่มเติม* บนระบบ A (ไม่ route แยก — บิลร้านล้วนเข้าคิวแทนที่จะนับเป็น notslip)

## Stack
- **Web:** Flask + gunicorn (`gunicorn.conf.py`: 1 worker / 8 threads / timeout 120) — Render รัน `gunicorn app:app` (ต้องมี `gunicorn.conf.py` เพราะไม่อ่าน Procfile)
- **AI:** Gemini ผ่าน `google-genai` SDK (model `gemini-2.5-flash`) — แบบ paid billing
- **DB:** PostgreSQL บน **Neon** (serverless, free plan) — ตั้งผ่าน env `DATABASE_URL` (host `...neon.tech`); ถ้าไม่ตั้ง fallback เป็น SQLite (local). region = **AWS US-East (Virginia)** ตรงกับ Render (Virginia) → บอท↔DB เร็ว. ⚠️ Neon free มีลิมิต compute ~191 CU-hrs/เดือน → ดู 'ประหยัด Neon compute' ใน ops
- **LINE:** line-bot-sdk v3 (ใช้ API v2 compat: `linebot`, `linebot.models`)
- **Host:** Render Starter (Virginia/US-East, ไม่หลับ), ping `/health` ทุก 5 นาทีด้วย UptimeRobot

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
- **จัดยอดใหม่ (`_payable_reconcile`):** จับคู่จ่าย↔บิลใหม่ (แก้ตัดผิดใบ) แบบ **คงยอดค้างเดิมเป๊ะ + ตรวจก่อนเขียน** — คำนวณ FIFO ในหน่วยความจำก่อน แล้ว 'ฉีด orphan กลับ' (`orphan = Σจ่าย.allocated − Σบิล.paid` = จ่ายที่เคยตัดบิลจ่ายครบซึ่งถูก `_payable_cleanup_paid` ลบไปแล้ว), ตรวจ `total_before==total_after`; ไม่ตรง/ผิดปกติ → raise **ยกเลิกทั้งหมด ไม่แตะ DB** (atomic — worst case คือไม่ทำ ไม่มีทางทำเพี้ยน). ⚠️ บั๊กเดิม: reset allocation ทั้งหมดโดยไม่คง orphan → ยอดหักซ้ำ (เคสจริง 128,206 → −194,437) จึงเขียนใหม่แบบ self-verify

## งานเบื้องหลัง (daemon threads)
- `_report_backup_loop` — เรียก `maybe_send_daily_report()` ทุก 5 นาที (สำรองจาก /health ping)
- `maybe_send_daily_report` — หลัง 00:30 วันละครั้ง (idempotent ด้วย meta.last_report_date): รายงานสลิป→SLIP_GROUPS, แล้ว `_cleanup_old_data()` (สรุปจองย้ายไปรอบเช้าแล้ว)
- `maybe_send_resv_summary` — รายงานจอง 2 รอบ/วัน (idempotent แยกรอบ ผ่าน `_maybe_send_resv_slot`, กรอบ [ชม., +3)): รอบเช้า `RESV_ADVANCE_SUMMARY_HOUR`(11:00) = จองล่วงหน้าที่ 'ถึงวันงานวันนี้' (resv_date=วันนี้ + created < วันนี้ → โผล่เฉพาะวันงาน) / รอบบ่าย `RESV_TODAY_SUMMARY_HOUR`(16:00) = จอง 'ในวัน' (created=วันนี้ เพื่อวันนี้) → BAR_GROUP + RESV_GROUPS; ไม่มีจอง=ไม่ส่ง
- `maybe_send_payable_summary` — หลัง `PAYABLE_SUMMARY_HOUR` (ดีฟอลต์ ตี1) วันละครั้ง (idempotent ด้วย meta.last_payable_summary_date + รายกลุ่ม): สรุปหนี้ "เมื่อวาน"→PAYABLE_GROUPS
- `_reservation_reminder_loop` — เตือนจอง PENDING (ทั้งจองวันนี้และจองล่วงหน้า) ทุก `RESV_NAG_INTERVAL_MIN` นาที (ดีฟอลต์ 30, ทุกช่วงเวลาเท่ากัน) จนคอนเฟิร์ม/เกิน `RESV_NAG_MAX_HOURS` (ดีฟอลต์ 3 ชม.) นับจากตอนรับจอง
- รูปสลิป: **โหลดรูปทันทีตอนรับ webhook** ผ่าน `_dl_pool` (workers เยอะ `SLIP_DL_WORKERS`=6) → คว้าไฟล์ตอนยังสด กัน `410 'content is gone'` ช่วงสลิปเยอะ แล้วส่ง bytes เข้า `_slip_pool` (ThreadPoolExecutor, SLIP_WORKERS=2) อ่าน Gemini แบบจำกัด concurrency กัน memory พุ่ง/OOM (กลุ่มเจ้าหนี้/ไม่เปิดสลิป → worker โหลดเอง). **กัน memory บวมช่วง burst:** `_slip_inflight` (BoundedSemaphore `SLIP_MAX_INFLIGHT`=6) จำกัด 'รูปค้างใน RAM พร้อมกัน' — โหลดจองสิทธิ์ก่อน, worker คืนเมื่ออ่านเสร็จ (holds_sem, try/finally); เต็มโควตา = dl-worker บล็อก (backpressure). error โหลดรูป 410/404 = `notify_admin_error` แยกข้อความ 'รูปหาย/ยกเลิก' ออกจาก 'Gemini quota' (`_is_content_gone`). **Gemini อ่านล้มทั้ง flash+pro** (มัก 503/rate-limit ชั่วคราว) → **re-queue ลองใหม่** อีก `SLIP_RETRY_MAX`(2) รอบ ห่าง `SLIP_RETRY_DELAY`(45)s แทนดรอปทันที (คิว re-queue จำกัด `SLIP_RETRY_QUEUE_MAX`(8) กัน RAM บวมตอน Gemini ล่มยาว); ยอมแพ้แล้วเตือนแอดมินพร้อม **error จริง** จาก Gemini (last_err) ไม่ใช่ข้อความเดา. **Circuit breaker 'เครดิต Gemini หมด':** เจอ 429 credits depleted (`_is_gemini_credits_error`) → เปิด breaker หยุดยิง Gemini `GEMINI_OUT_COOLDOWN`(600)s (ไม่ re-queue เพราะยิงไปก็ 429) + เตือนแอดมิน 'เติมเครดิตด่วน'; ระหว่างหยุดปล่อย probe 1 ใบทุก `GEMINI_PROBE_INTERVAL`(60)s เช็คว่าเครดิตกลับมา — สำเร็จเมื่อไหร่ปลด breaker อ่านต่อเอง (~1 นาทีหลังเติม ไม่ต้อง deploy)
- **กันสแปมเมื่อบอทถูกเตะออกกลุ่ม:** ส่งรายงาน/สรุปแบบ idempotent "รายกลุ่ม" (กลุ่มที่ส่งสำเร็จแล้วไม่ส่งซ้ำ retry เฉพาะที่พลาด) + `LeaveEvent`→มาร์ค `left:{group}` + push เจอ 400 ("ไม่ใช่สมาชิก") → auto-prune มาร์ค left เอง + `JoinEvent`/มีข้อความเข้ามา → ปลดมาร์ค (self-heal)

## คำสั่งในแชท
- `help`/`คำสั่ง` — เมนู • `groupid` — ดู Group ID • `โควต้า` — ดู push ที่ใช้เดือนนี้ (แยกราย OA)
- สลิป: `สรุป`, `สรุป YYYY-MM-DD`, `รายงานเมื่อวาน`, `ลบล่าสุด`/`ลบ N`, `ล้างวันนี้`
- จอง: พิมพ์ประโยคจอง (AI จับ) → ต้องครบ ชื่อ/จำนวนคน/วันเวลา(มั่นใจ)/โซน → กดปุ่มคอนเฟิร์ม • `สรุปจอง`
- บัญชีหนี้ (เฉพาะ PAYABLE_GROUPS): ส่งรูปบิล/สลิป (AI แยกเอง) • `บิล 3500` (บันทึกด้วยเลข) • `ตั้งยอดยกมา 12000` • `สรุปหนี้`/`สรุปหนี้ YYYY-MM-DD` • `จัดยอดใหม่` (จับคู่จ่าย↔บิลใหม่ คงยอดเดิม self-verify) • `ลบบิลล่าสุด`/`ลบจ่ายล่าสุด` • `ล้างบัญชีหนี้` (ปุ่มยืนยัน)
- กระทบบิล-สลิป (เฉพาะ DINING_GROUPS): ส่งรูปบิลร้าน+สลิป (AI แยกเอง) • โอนขาด→เด้ง **ปุ่มเคลียร์เฉพาะโต๊ะนั้น** (เก็บส่วนต่างครบแล้วกด → ตัดยอดขาดโต๊ะนั้นออก) • `ยอดสะสม`/`ยอดทริป` (ดูยอด + โต๊ะที่ยังไม่เคลียร์ + การ์ดปุ่มปิดยอดทริป) • `เก็บครบแล้ว`/`ปิดยอด` หรือกดปุ่มปิดยอดทริป (postback `dining_confirm`) → รีเซ็ตทั้งทริปเป็น 0 (สิ้นวัน). กันกดซ้ำทุกปุ่มด้วย `_postback_once`
- `คู่มือ` — บอทส่งคู่มือย่อ • `ล้างทั้งหมด` — ล้างข้อมูลกลุ่มนี้ทั้งหมด (สลิป+จอง ทุกวัน, มีปุ่มยืนยัน)

## ENV vars (ตั้งที่ Render — repo เป็น public ห้าม hardcode)
- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_ADMIN_USER_ID`
- **แผน B (แยกกลุ่มคนละ OA):** `LINE_CHANNEL_ACCESS_TOKEN_2/_3/_4` (token OA สำรอง) + `LINE_CHANNEL_SECRET_2/_3/_4` (secret — ไว้ตอบ `groupid`) + `OA_ROUTE` (แมป `groupid:เลขOA`) + `RESV_INFO_GROUPS` (กลุ่มเด้งข้อมูลจอง เช่น บาร์น้ำ,sound). `PUSH_FREE_LIMIT`(300), `PUSH_WARN_RATIO`(0.8), `MEMBER_COUNT_TTL`(3600)
- `GEMINI_API_KEY`, `GEMINI_MODEL` (ดีฟอลต์ gemini-2.5-flash)
- `DATABASE_URL` (Neon Postgres — host `...neon.tech`; ควร region เดียวกับ Render เพื่อ latency ต่ำ)
- `SLIP_GROUPS`, `RESV_GROUPS`, `RESV_EXCLUDE_GROUPS`, `BAR_GROUP_ID`, `PAYEE_KEYWORDS`
- `IGNORE_GROUPS` — กลุ่มที่ "บอทเมินทั้งหมด" (ไม่เช็คสลิป/ไม่จอง/ไม่ตอบคำสั่ง/ไม่ส่งรายงาน-เตือน) ใช้กับกลุ่มที่เลิกใช้
- `PAYABLE_GROUPS` — กลุ่มบัญชีเจ้าหนี้ (เช่น กลุ่มดวงใจ); `PAYABLE_VENDOR` (ดีฟอลต์ "ดวงใจการสุรา"); `PAYABLE_SUMMARY_HOUR` (ดีฟอลต์ 1 = ตี1)
- `REPORT_REDIRECT` — ส่งรายงานของกลุ่มต้นทางไปเข้ากลุ่มปลายทางแทน (เนื้อหายังเป็นของต้นทาง) ครอบทุกรายงาน (สลิป/จอง/หนี้); รูปแบบ `ต้นทาง:ปลายทาง,ต้นทาง2:ปลายทาง2`
- `PAYABLE_MIRROR` — บัญชีหนี้แบบ "บัญชีเดียว 2 กลุ่ม"; รูปแบบ `primary:mirror`. primary=กลุ่มทำงาน (ส่งบิล/สลิป + บอท**ตอบยืนยันรายตัวด้วย reply ฟรี**ที่นี่) + เก็บข้อมูลจริง, mirror(กลุ่ม 2)=รับ **เฉพาะสรุปรายวัน** (ตี1) ไม่เด้งทุกบิล/สลิป (ประหยัดโควต้า push). ใช้ข้อมูลชุดเดียวกัน. ทั้ง 2 กลุ่มต้องอยู่ใน `PAYABLE_GROUPS` ด้วย
- `DINING_GROUPS` — กลุ่มที่เปิดระบบ D (กระทบบิล-สลิป โอนขาด/เกิน); เว้นว่าง=ปิด (บิลร้านนับเป็น notslip ตามเดิม). ต้องอยู่ใน SLIP_GROUPS ด้วย
- `DINING_SHORT_BAHT`(1) — เตือนเมื่อโอน 'ขาด' ตั้งแต่กี่บาทขึ้นไป (≥); `DINING_MATCH_MIN`(45) — บิลค้างรอจับคู่สลิปได้นานกี่นาที (เกิน=หมดอายุ); `DINING_MATCH_DELAY`(90) — หน่วงวินาทีก่อนจับคู่บิล-สลิป (รอบิลที่ส่งไล่ๆ กันถูกอ่านเข้าคิวครบ กันจับคู่สลับลำดับ; โอนขาดเตือนแบบ push)
- `RESV_NAG_MAX_HOURS`(3), `RESV_NAG_INTERVAL_MIN`(30 = ตื๊อคอนเฟิร์มทุกกี่นาที), `RESV_ADVANCE_SUMMARY_HOUR`(11 = รายงานจองล่วงหน้า), `RESV_TODAY_SUMMARY_HOUR`(16 = สรุปจองวันนี้), `RESV_REPORT_DAYS`(7), `RESV_KEEP_DAYS`(15), `MISS_KEEP_DAYS`(14), `SLIP_KEEP_DAYS`(60), `SLIP_WORKERS`(2 = อ่าน Gemini พร้อมกันกี่ใบ), `SLIP_DL_WORKERS`(6 = โหลดรูปพร้อมกันกี่ใบ), `SLIP_MAX_INFLIGHT`(6 = รูปค้าง RAM พร้อมกันสูงสุด — ลดถ้า RAM ตึง), `SLIP_RETRY_MAX`(2 = re-queue กี่รอบเมื่อ Gemini ล้ม), `SLIP_RETRY_DELAY`(45s), `SLIP_RETRY_QUEUE_MAX`(8), `GEMINI_OUT_COOLDOWN`(600s = หยุดยิงเมื่อเครดิตหมด), `GEMINI_PROBE_INTERVAL`(60s = probe เช็คเครดิตกลับ), `MEM_WARN_MB`(430), `QUIET_START_HOUR`(3)/`QUIET_END_HOUR`(10 = ช่วงเงียบลึก หยุด query ปล่อย Neon หลับ), `RESV_LOOP_SEC`(120 = รอบเช็คจอง PENDING นอกช่วงเงียบ)
- `SLIP_WARN_UNREAD`(1) — เตือนในกลุ่มเมื่อบอท "อ่านออกแต่ไม่ใช่สลิป" (notslip); ตั้ง 0 เพื่อปิดถ้ารก. หมายเหตุ: ระบบ "อ่านซ้ำอัตโนมัติด้วย pro" 1 รอบเมื่อรอบแรกอ่านไม่ออก/ไม่ใช่สลิป/ยอด≤0 **หรือ flash โยน error** (กู้ใบยาก/ถ่ายจอ + กัน Gemini ล้มชั่วคราวช่วงพีค)
- **เคส error (อ่านไม่สำเร็จจริง):** ดาวน์โหลดรูป/อ่านพังทั้ง flash+pro/บันทึกพลาด → **เงียบในกลุ่ม + เตือนเฉพาะแอดมิน** (ไม่เด้ง "ส่งสลิปใหม่" กวนกลุ่มอีก — รูปที่พลาดยังนับใน "ตกหล่น" เห็นได้ที่ `สรุป`)
- `PROMPTPAY_API_KEY` (SlipOK — ยังไม่เปิด; เปิดได้เพื่อตรวจกับธนาคารจริง 100%)
- **หลาย OA — ประหยัดค่า push (2 โหมด):** `LINE_CHANNEL_ACCESS_TOKEN_2` (`_3`.._5). reply ฟรีไม่นับ; **⚠️ LINE นับ push เข้ากลุ่ม = จำนวนสมาชิกกลุ่ม (ไม่ใช่ 1, bubble ไม่มีผล)** → `_recipient_count()` ดึงจำนวนสมาชิก (แคช `MEMBER_COUNT_TTL`=3600s) มานับโควต้าให้ตรงกับที่ LINE คิดจริง. แผนฟรีไทย = **300/เดือน/OA** (`PUSH_FREE_LIMIT`=300; ตั้ง env ให้ตรงแพ็กจริงได้ เช่น 15000). นับแยกรายเดือน meta `push_count[N]:YYYY-MM`. ใกล้เต็ม (`PUSH_WARN_RATIO`=0.8) → DM เตือนแอดมินครั้งเดียว/เดือน/OA
  - **⚠️ กฎ LINE: 1 กลุ่ม = OA ได้ตัวเดียว** — เชิญ OA ตัวที่ 2 เข้ากลุ่มที่มี OA อยู่แล้ว → เด้งออกทันที. ดังนั้น "failover สลับ OA ในกลุ่มเดียวกัน" **ทำไม่ได้**
  - **โหมดเดิม (ไม่ตั้ง `OA_ROUTE`):** OA ทุกตัวถือว่าอยู่กลุ่มเดียวกัน → `_push()` ใช้ OA1 จนเต็มแล้ว cascade OA2/3.. (ใช้ได้เฉพาะถ้าจริงๆ อยู่กลุ่มเดียวกันได้ — ซึ่งLINEห้าม จึงเลิกใช้)
  - **แผน B — แยกกลุ่มคนละ OA (`OA_ROUTE`):** แมป `groupid:เลขOA` (เช่น `Cslip:1,Cmanage:2,Cbar:3,Csound:4`) → `_push(กลุ่ม)` ส่งผ่าน OA ของกลุ่มนั้นตัวเดียว (ไม่ cascade). ปลายทางนอกแมป (DM แอดมิน) → OA1 ตัวเดียว. โควต้าแยกราย OA → รวมความจุฟรีได้หลายเท่า. เอด=กลุ่มสลิป(OA1, webhook+reply), รายงานสลิป→management (`REPORT_REDIRECT`)
  - **เด้งจองไป info groups (`RESV_INFO_GROUPS`):** ตั้งไว้=เปิดแผน B ฝั่งจอง → จองทุกใบคอนเฟิร์มที่กลุ่มรับจอง(สลิป)ที่เดียว + `_resv_broadcast_info()` เด้ง 'ข้อมูลจอง (ไม่มีปุ่ม)' ไปบาร์น้ำ/sound ผ่าน OA ของกลุ่มนั้น
  - **groupid ของ OA สำรอง:** OA สำรองเป็น push-only (ไม่มี webhook) เลยตอบ `groupid` ไม่ได้ → `/callback` ถ้า signature ไม่ใช่ช่องหลัก จะลองช่องสำรอง (ตรวจ HMAC-SHA256 ต่อ `LINE_CHANNEL_SECRET_N` แบบ constant-time) **เฉพาะบริการคำสั่ง `groupid`** → OA นั้นตอบไอดีตัวเอง (ได้ไอดีตรงแม้คนละ provider). เปิด webhook ชั่วคราวตอนตั้งค่า แล้วปิดกลับได้
  - OA สำรองทุกตัว: ปิด auto-reply + (ปกติ) ปิด webhook. ดูยอด: พิมพ์ `โควต้า`

## ข้อควรรู้ในการดูแล (ops)
- **ประหยัด Neon compute (กันชนลิมิต free ~191 CU-hrs/เดือน):** ช่วง 'เงียบลึก' `QUIET_START_HOUR`(3)–`QUIET_END_HOUR`(10) background loops (`_reservation_reminder_loop`, `_report_backup_loop`) + `/health` จะ **หยุด query DB** (นอนยาว / ตอบ `{status:ok,quiet:true}`) → ปล่อยให้ Neon serverless หลับ ~7 ชม./วัน (~ลด 29%). นอกช่วงนี้เช็คปกติ (reminder ทุก `RESV_LOOP_SEC`=120s). วัดจริงที่ Neon console → ถ้ายังใกล้ลิมิต ขยายหน้าต่างเงียบ/ลดความถี่เพิ่ม
- **อย่า deploy ตอนร้านเปิด** — ทุก deploy worker restart ~1 นาที สลิป/จองช่วงนั้นเสี่ยงหลุด
- **pin เวอร์ชันใน requirements.txt** แล้ว — กัน auto-upgrade ทำพัง (อัปเดตเมื่อทดสอบแล้วเท่านั้น)
- ดูสถานะ: `<render-url>/health` → `{status, slips_today, active_groups, storage, memory_mb}`
- **เฝ้า memory:** `_check_memory()` (เรียกจาก /health ping + backup loop ทุก ~5 นาที) — RSS เกิน `MEM_WARN_MB`(430) → DM เตือนแอดมิน (จำกัด 1 ครั้ง/30 นาที) กันก่อน OOM/restart

## ข้อจำกัดที่รู้อยู่
- AI อ่านสลิปจากรูป ~98% (1-2 ใบ/วันอาจพลาด) — 100% ต้องเปิด SlipOK API
- จองล่วงหน้า: AI คำนวณ resv_date จาก "วันนี้" ที่ inject — เคสกำกวมจะถามวันที่กลับ
