# 📨 ส่งให้ห้อง Dashboard/Apps Script — อัปเดตจากห้องบอท (2026-07-25)

> อ่านก่อนทำงานต่อ · **`git fetch origin main && git pull`** เอาโค้ดล่าสุดก่อนเสมอ
> รายละเอียดเต็มอยู่ใน `COORDINATION.md` (โน้ตล่าสุดบนสุด)

---

## 🔴 เรื่องสำคัญที่สุด: ย้าย DB Neon → Supabase

- **บอทเปลี่ยน `DATABASE_URL` เป็น Supabase แล้ว** (ไม่ใช่ Neon)
  - เหตุ: Neon free (100 CU-hr/เดือน) compute เต็มซ้ำ → ย้ายไป Supabase (ไม่มีลิมิต compute-hour)
  - ย้ายข้อมูลครบ: สลิป 37 วัน + บัญชีหนี้ (ค้าง 202,293) + จอง/meta
- **กระทบอีกห้องยังไง:** โดยตรง = ไม่มี (dashboard/Apps Script คุยผ่าน `/api/slip_daily` เหมือนเดิม URL/token ไม่เปลี่ยน)
  - แต่ **ห้ามตั้ง `DATABASE_URL` กลับไป Neon** ถ้าไปยุ่งกับ env Render
- endpoint ใหม่: **`/api/db_export?token=`** (dump ทุกตารางเป็น JSON, อ่านอย่างเดียว) ไว้ backup DB ได้

---

## 🎨 ไฟล์ dashboard ที่แก้วันนี้ (⚠️ ถ้าอีกห้องแก้ไฟล์เดียวกัน = ต้อง pull ก่อน)

แก้ **`dashboard/saisang_dashboard.html`** (commit บน main แล้ว):
1. **โลโก้ร้านจริง** — เปลี่ยนไอคอน PWA (manifest/apple-touch/favicon) + โลโก้บนหน้า (`brand-logo`) เป็นโลโก้ไส้ย่างซอย๔ ตัวจริง **พื้นโปร่งใส** (ตัดสีขาวออกหมด)
2. **ปุ่มสลับ Dark/Light** — แก้ให้โชว์บนมือถือ (เดิม `.topbar-right .btn { display:none }` ซ่อนปุ่มธีมด้วย → เพิ่ม `#theme-btn` เข้า list ยกเว้น)

> โลโก้จริงดึงมาจาก **saiyangsoi Google Drive** ไฟล์ `LOGO ไส้ย่าง สีขาว.jpg` (แล้วตัดพื้นขาวเป็นโปร่งใส)
> **Deploy dashboard:** ลาก deploy.zip ล่าสุด (index.html + _headers) ขึ้น Netlify เปิด `?v=18`

**Apps Script (`Code_full.gs`) วันนี้ไม่ได้แก้** — เหมือนเดิม

---

## 🔧 อื่นๆ ที่บอทแก้วันนี้ (ไม่กระทบไฟล์อีกห้อง แต่ให้รู้ไว้)

- **routing จอง:** จองวันนี้→กลุ่ม staff คอนเฟิร์ม / จองล่วงหน้า→บาร์น้ำ / สรุปเข้ากลุ่ม staff
- **แก้ false positive "สลิปซ้ำ":** เช็คผู้โอนด้วย (คนละคนยอดเท่ากัน=ไม่เตือน) + บอกซ้ำกับใบไหน
- **แรม:** เพิ่ม gunicorn `max_requests` รีไซเคิล worker กัน OOM
- **กันสแปม:** เตือน "เครดิต Gemini หมด" ครั้งเดียว/รอบ

---

## ⏳ งานค้าง (รอเจ้าของ)

| เรื่อง | สถานะ |
|---|---|
| 💳 Gemini เครดิตหมด → เติม/auto-pay | 🔴 สลิปยังเช็คไม่ได้ |
| 📊 รายงานเงียบ → ขอ Render Logs ตอน `ทดสอบรายงาน` | 🟡 |
| 🔢 ตั้ง `PUSH_FREE_LIMIT` = โควตาจริง OA1 (15000) | 🟡 |
| 🧠 เฝ้าแรม 486MB | 🟢 |
| 📱 Deploy dashboard ล่าสุด (?v=18) | 🟡 |

---

## 📌 สรุปสถานะระบบ (ให้ตรงกันทั้ง 2 ห้อง)

- **บอท:** Render · gunicorn 1 worker/8 threads · deploy จาก `main` (auto)
- **DB:** **Supabase** (Postgres, Session pooler 5432) — *เปลี่ยนจาก Neon*
- **Dashboard:** Netlify (drag zip) · **POS:** Apps Script บัญชี saiyangsoi
- **AI:** Gemini 2.5-flash/pro · **LINE:** OA1(จ่าย)+OA2-4(ฟรี), แผน B แยกกลุ่มคนละ OA
- **Git:** งานเล็ก→push main · งานใหญ่/เสี่ยง→branch+PR

---

*ห้องบอท ↔ ห้อง dashboard ใช้ repo เดียวกัน · ก่อนเริ่มงานทุกครั้ง fetch main + อ่าน COORDINATION.md*
