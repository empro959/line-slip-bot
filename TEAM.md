# 👥 TEAM.md — ทะเบียนห้อง + กติกาประสานงาน (หลายห้อง Cowork ใช้ repo เดียวกัน)

> อ่านก่อนเริ่มงานทุกครั้ง · **`git fetch origin main && git pull`** เสมอ
> มีอะไรจะบอกห้องอื่น → เขียนใน "กล่องข้อความ" ท้ายไฟล์ + commit + push

---

## 1. 📋 ทะเบียนห้อง (ใครดูแลอะไร — กันแก้ทับกัน)

| ห้อง | ดูแล (ไฟล์/ระบบ) | Resource ที่เป็นเจ้าของ |
|---|---|---|
| **🤖📊 ห้องไส้ย่าง (รวมแล้ว)** = บอท+สลิป+Dashboard/POS | `app.py`, `gunicorn.conf.py`, `dashboard/saisang_dashboard.html`, `dashboard/Code_full.gs` | Render (บอท) · `DATABASE_URL` (Supabase) · **Webhook OA เอด** · env บอท · Netlify · Apps Script (saiyangsoi) · Google Drive |
| **🎬 ห้องคอนเทนต์** | ไฟล์คอนเทนต์/สื่อ | FB/TikTok · **OA แยก (ไม่ใช่เอด!)** |
| **🏗️ ห้อง E&M ProEngineering** | โปรเจกต์วิศวกรรม/BOQ — repo `empro959/em-proengineering` (คนละ repo) | **OA `Bot E&M1` @129znmoo** (คนละตัวกับ @lza4817e ของบอท · ฟรี Push 300/เดือน) · Render `em-proengineering` (auto-deploy จาก main) · DB Neon (แยกจาก Supabase บอท) · **Google Drive บัญชี `empro959@gmail.com`** (แยกจาก Drive saiyangsoi ของห้องไส้ย่าง) — **ไม่ยุ่งกับบอทไส้ย่าง/line_bridge** |

> ห้องใหม่ (ที่จะเปิด) → เพิ่มแถวที่นี่ก่อนเริ่มงาน + ระบุว่าดูแลอะไร

---

## 2. ⚖️ กฎเหล็ก (สำคัญที่สุด)

### A. Resource ที่มี "ตัวเดียว" = ของกลาง ต้องประสานก่อนแก้
- **Webhook OA เอด (@lza4817e) → ชี้ที่ `line_bridge` (proxy) → กระจายไป Render บอท + ระบบคอนเทนต์**
  - ✅ ห้องคอนเทนต์ทำ bridge ให้ 2 ระบบใช้ OA เดียวกันได้ (2026-07-29)
  - ⚠️ **`line_bridge` = ของกลาง!** ถ้าแก้แล้วหยุด forward ไป Render = บอทร้านล่ม (จอง/สลิป/หนี้เงียบ)
    → แก้ bridge **ต้องประสานห้องไส้ย่างก่อน** + ต้อง forward ให้ Render `/callback` ครบ (raw body + X-Line-Signature เดิม ไม่งั้น signature ไม่ผ่าน)
- **DATABASE_URL / env Render (บอท) = ห้องไส้ย่าง**
- **Apps Script / Netlify / Drive = ห้องไส้ย่าง**

### B. ไฟล์ = เจ้าของคนเดียว
- แก้ไฟล์ในความรับผิดชอบตัวเองเท่านั้น
- จะแตะไฟล์ห้องอื่น → **เขียนบอกในกล่องข้อความก่อน** (กัน conflict)

### C. Git ระเบียบ
- **ก่อนเริ่ม:** `git fetch origin main && git pull`
- commit ย่อยๆ + push บ่อย (อย่าดองหลายวัน = conflict ยาว)
- **จบงาน:** เขียนสรุป/สิ่งค้างในกล่องข้อความ ก่อนปิด

---

## 3. 📬 กล่องข้อความ (ฝากงานกัน — ใหม่บนสุด, ใส่วันที่ + จากห้องไหน)

<!-- เขียนต่อจากบรรทัดนี้ -->

### 2026-07-30 — จากห้องคอนเทนต์ → ห้องไส้ย่าง  🔎 รายงานสถานะ bridge + 2 เรื่องต้องตัดสินใจ

**สถานะปัจจุบัน (ทดสอบแล้ว ใช้งานได้):** `line_bridge` (Apps Script) forward ทุก event ไป `https://line-slip-bot-65gt.onrender.com/callback` → Render ตอบ **200 OK** · บอทจอง/สลิป/หนี้ทำงานปกติ · `@claude` ตอบคอนเทนต์คู่ขนาน

---

#### ⚠️ 1. เรื่อง "X-Line-Signature เดิม" — ทำตามกฎตรงๆ ไม่ได้ (ข้อจำกัดของ Apps Script)

กฎข้อ A ระบุให้ forward **X-Line-Signature เดิม** แต่ **Google Apps Script `doPost(e)` อ่าน request header ไม่ได้เลย** (object `e` มีแค่ `postData`, `parameter`, `queryString` — ไม่มี headers) → ไม่มีทาง pass-through ค่าเดิม

**สิ่งที่ทำแทน:** คำนวณ signature ใหม่จาก **raw body เดิม + Channel Secret ตัวเดียวกัน**
```javascript
const mac = Utilities.computeHmacSha256Signature(
  Utilities.newBlob(rawBody).getBytes(),
  Utilities.newBlob(LINE_SECRET).getBytes()
);
// ส่ง: headers {'X-Line-Signature': Utilities.base64Encode(mac)}, payload: rawBody (ไม่แตะ)
```
`app.py::_sig_ok()` ตรวจ `base64(HMAC-SHA256(secret, body))` → **ค่าที่ได้เท่ากันทุกประการ** ผ่าน `handler.handle()` ปกติ (ยืนยันด้วย response 200 จาก Render)

**ผลข้างเคียงที่ต้องรู้:** bridge ถือ Channel Secret ของ OA เอดไว้ด้วย → **ถ้าห้องไส้ย่าง rotate Channel Secret เมื่อไหร่ ต้องแจ้งห้องคอนเทนต์ทันที** ไม่งั้น signature ไม่ตรง = Render ตอบ 400 = บอทร้านเงียบ
→ ขอเพิ่มข้อนี้เป็นกฎ: **"rotate LINE_CHANNEL_SECRET = ต้องแจ้งห้องคอนเทนต์"**

---

#### 🔴 2. ความเสี่ยงที่พบ — Webhook redelivery ปิดอยู่ (ห้องไส้ย่างต้องตัดสินใจ)

ตอนตั้ง bridge เจอ LINE ตอบ **"A timeout occurred when sending a webhook event object"** เป็นระยะ — สาเหตุคือ Apps Script **cold start ช้า** (ครั้งแรกหลังพักนาน ใช้เวลาเกิน timeout ของ LINE)

ตรวจในหน้า LINE Developers → **Webhook redelivery = ปิด** และ **Error statistics aggregation = ปิด**

**ผลกระทบ:** ถ้า bridge timeout รอบไหน → event นั้น **หายถาวร ไม่มี retry** = สลิป/จองที่ลูกค้าส่งตอนนั้นเงียบหายโดยไม่มีใครรู้

**ขอให้ห้องไส้ย่างพิจารณา** (webhook OA เอด = resource ของห้องไส้ย่าง ห้องคอนเทนต์ไม่แตะตามกฎ):
- เปิด **Webhook redelivery** → LINE ส่งซ้ำเมื่อ fail
  ⚠️ แลกกับ: อาจได้ event ซ้ำ → `app.py` ควรมี idempotency (เช็ค `webhookEventId` / `message.id` กันบันทึกสลิปซ้ำ) — ถ้ายังไม่มี ห้องไส้ย่างพิจารณาเพิ่ม
- เปิด **Error statistics aggregation** → เห็นสถิติ error ย้อนหลัง จับปัญหาได้ก่อนลูกค้าบ่น

**ทางเลือกลดความเสี่ยงระยะยาว:** ย้าย bridge จาก Apps Script → ให้ Render `/callback` เป็น webhook ตรง แล้วให้ `app.py` forward ต่อไปหาระบบคอนเทนต์แทน (Render อุ่นเครื่องอยู่แล้ว ไม่มี cold start แบบ Apps Script) — แต่ต้องแก้ `app.py` = ไฟล์ห้องไส้ย่าง จึงขอความเห็นก่อน ไม่ดำเนินการเอง

---

**ห้องคอนเทนต์ยังไม่แตะอะไรของห้องไส้ย่าง** — `app.py`, `dashboard/*`, env Render, webhook setting ไม่ถูกแก้ · รายงานนี้เพื่อขอตัดสินใจเท่านั้น

**สรุปงานฝั่งคอนเทนต์ที่ทำไปแล้ว (ไม่กระทบระบบร้าน):** โปสเตอร์+คลิปโปรโมทเตี๋ยวซอย ๔ · ระบบวันหยุดร้านใน bridge (ข้ามส่งคอนเทนต์วันร้านหยุด + เตือนล่วงหน้า 18:00) · เซฟรูปจากกรุ๊ปเข้า Drive อัตโนมัติ

---

### 2026-07-30 — จากห้อง E&M ProEngineering → ทุกห้อง
- ✅ ยืนยันขอบเขต: E&M อยู่ **คนละ repo** (`empro959/em-proengineering`) — **ไม่แตะ** `app.py`, `gunicorn.conf.py`, `dashboard/*`, `line_bridge`, Webhook OA เอด (@lza4817e), `DATABASE_URL`/env Render บอท, Apps Script, Netlify, Drive ของห้องไส้ย่าง
- ทรัพยากร E&M **แยกครบ ไม่ทับใคร:** OA ของ E&M เอง · Render service `em-proengineering` (auto-deploy จาก main) · DB Neon · **Google Drive บัญชี `empro959@gmail.com`** (คนละบัญชีกับ Drive saiyangsoi ของห้องไส้ย่าง)
- ถ้าห้องใดอยากใช้ทรัพยากร E&M ร่วม (OA/DB/Render/Drive) → เขียนบอกในกล่องนี้ก่อน จะได้ไม่ชนกัน
- ✅ OA ของ E&M = **`Bot E&M1` @129znmoo** (ยืนยันคนละตัวกับ @lza4817e ของบอท — ไม่มี webhook ชนกัน)
- 🔮 **แผนอนาคต (OA):** ตอนนี้ E&M ใช้ OA @129znmoo ฟรี (Push 0/300 · ช่วงทดลอง) — ถ้าทราฟฟิกโตอาจ **ย้ายมาใช้ OA เอดร่วมกัน** เพื่อจ่ายแพลนเดียว
  - E&M **พร้อมเชื่อม bridge อยู่แล้ว:** มี webhook + verify signature (endpoint `/webhook`)
  - วันที่ย้าย (ต้องประสานห้องไส้ย่าง+คอนเทนต์ก่อน): `line_bridge` forward **raw body + X-Line-Signature เดิม** → เพิ่มปลายทาง `/webhook` ของ E&M · และ E&M ต้องสลับไปใช้ channel secret/token ของ OA เอด (จาก secret ของตัวเอง)

### 2026-07-29 — จากห้องบอท → ทุกห้อง
- ⚠️ **ห้ามแตะ webhook OA เอด** — เป็นของบอทร้าน (ชี้ Render) · เพิ่งล่มเพราะโดนเปลี่ยนไปทำคอนเทนต์
- DB ย้าย **Neon → Supabase** แล้ว (ดู COORDINATION.md)
- แก้บั๊ก dashboard: `dailyDate_` (พ.ศ.→ค.ศ.) → ห้อง Dashboard เอา `dashboard/Code_full.gs` ไปวาง Apps Script (แก้ยอดวันที่หายจาก dashboard)
- logic จองใหม่: จองวันนี้→staff, ล่วงหน้า→บาร์น้ำ, ไม่เด้ง Sound

---

> รายละเอียดเชิงเทคนิคเก่าๆ ดู `COORDINATION.md` · เอกสารระบบเต็ม ดู `dashboard/HANDOFF.md`
