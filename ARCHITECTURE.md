# ARCHITECTURE — line-slip-bot ("เอด" / @lza4817e)

LINE bot สำหรับร้าน **ไส้ย่างซอย๔ (E&M)** — Flask ไฟล์เดียว (`app.py`) รันด้วย gunicorn บน Render, เก็บข้อมูลใน PostgreSQL

## ภาพรวม 2 ระบบ
- **A. ตรวจสลิป** — อ่านสลิปโอนเงินด้วย Gemini AI, จับปลอม/ซ้ำ/โอนผิดบัญชี/ครอป, รายงานสรุป 00:30
- **B. จองโต๊ะ** — AI จับข้อความจอง → การ์ดปุ่มคอนเฟิร์ม, จองล่วงหน้าส่งกลุ่มบาร์น้ำ, เตือนซ้ำจนคอนเฟิร์ม, รายงานจองล่วงหน้า 00:30

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
- **meta** — key/value: `last_report_date`/`last_resv_summary_date` (กันส่งซ้ำรายวัน), `sent:{job}:{date}:{group}` (กันส่งซ้ำ "รายกลุ่ม" เผื่อบางกลุ่มพลาดต้อง retry — ล้างของเก่าใน cleanup), `left:{group}` (กลุ่มที่บอทถูกเตะออก = เลิกส่ง กันค้าง retry/สแปม)

## งานเบื้องหลัง (daemon threads)
- `_report_backup_loop` — เรียก `maybe_send_daily_report()` ทุก 5 นาที (สำรองจาก /health ping)
- `maybe_send_daily_report` — หลัง 00:30 วันละครั้ง (idempotent ด้วย meta.last_report_date): รายงานสลิป→SLIP_GROUPS, รายงานจองล่วงหน้า→BAR_GROUP, แล้ว `_cleanup_old_data()`
- `_reservation_reminder_loop` — เตือนจอง PENDING ทุก 5 นาที (18-22น.)/15 นาที (เวลาอื่น) จนคอนเฟิร์ม/เกิน RESV_NAG_MAX_HOURS
- รูปสลิป: ประมวลผลผ่าน `_slip_pool` (ThreadPoolExecutor, SLIP_WORKERS=2) กัน burst ทำ memory พุ่ง/OOM
- **กันสแปมเมื่อบอทถูกเตะออกกลุ่ม:** ส่งรายงาน/สรุปแบบ idempotent "รายกลุ่ม" (กลุ่มที่ส่งสำเร็จแล้วไม่ส่งซ้ำ retry เฉพาะที่พลาด) + `LeaveEvent`→มาร์ค `left:{group}` + push เจอ 400 ("ไม่ใช่สมาชิก") → auto-prune มาร์ค left เอง + `JoinEvent`/มีข้อความเข้ามา → ปลดมาร์ค (self-heal)

## คำสั่งในแชท
- `help`/`คำสั่ง` — เมนู • `groupid` — ดู Group ID
- สลิป: `สรุป`, `สรุป YYYY-MM-DD`, `รายงานเมื่อวาน`, `ลบล่าสุด`/`ลบ N`, `ล้างวันนี้`
- จอง: พิมพ์ประโยคจอง (AI จับ) → ต้องครบ ชื่อ/จำนวนคน/วันเวลา(มั่นใจ)/โซน → กดปุ่มคอนเฟิร์ม • `สรุปจอง`
- `คู่มือ` — บอทส่งคู่มือย่อ • `ล้างทั้งหมด` — ล้างข้อมูลกลุ่มนี้ทั้งหมด (สลิป+จอง ทุกวัน, มีปุ่มยืนยัน)

## ENV vars (ตั้งที่ Render — repo เป็น public ห้าม hardcode)
- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_ADMIN_USER_ID`
- `GEMINI_API_KEY`, `GEMINI_MODEL` (ดีฟอลต์ gemini-2.5-flash)
- `DATABASE_URL` (Postgres — ต้อง region เดียวกับ web service)
- `SLIP_GROUPS`, `RESV_GROUPS`, `RESV_EXCLUDE_GROUPS`, `BAR_GROUP_ID`, `PAYEE_KEYWORDS`
- `IGNORE_GROUPS` — กลุ่มที่ "บอทเมินทั้งหมด" (ไม่เช็คสลิป/ไม่จอง/ไม่ตอบคำสั่ง/ไม่ส่งรายงาน-เตือน) ใช้กับกลุ่มที่เลิกใช้
- `RESV_NAG_MAX_HOURS`(6), `RESV_REPORT_DAYS`(7), `RESV_KEEP_DAYS`(15), `MISS_KEEP_DAYS`(14), `SLIP_KEEP_DAYS`(60), `SLIP_WORKERS`(2)
- `PROMPTPAY_API_KEY` (SlipOK — ยังไม่เปิด; เปิดได้เพื่อตรวจกับธนาคารจริง 100%)

## ข้อควรรู้ในการดูแล (ops)
- ⚠️ **Render Postgres ฟรีถูกลบหลัง 90 วัน** → ต้อง upgrade เป็น paid หรือย้ายข้อมูลก่อนครบ
- **อย่า deploy ตอนร้านเปิด** — ทุก deploy worker restart ~1 นาที สลิป/จองช่วงนั้นเสี่ยงหลุด
- **pin เวอร์ชันใน requirements.txt** แล้ว — กัน auto-upgrade ทำพัง (อัปเดตเมื่อทดสอบแล้วเท่านั้น)
- ดูสถานะ: `<render-url>/health` → `{status, slips_today, active_groups, storage}`

## ข้อจำกัดที่รู้อยู่
- AI อ่านสลิปจากรูป ~98% (1-2 ใบ/วันอาจพลาด) — 100% ต้องเปิด SlipOK API
- จองล่วงหน้า: AI คำนวณ resv_date จาก "วันนี้" ที่ inject — เคสกำกวมจะถามวันที่กลับ
