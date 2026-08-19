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
- 🔑 **rotate `LINE_CHANNEL_SECRET` (OA เอด) = ต้องแจ้งห้องคอนเทนต์ทันที** (bridge ถือ secret ไว้คำนวณ signature — ไม่แจ้ง = signature ไม่ตรง = Render 400 = บอทเงียบ)

### B. ไฟล์ = เจ้าของคนเดียว
- แก้ไฟล์ในความรับผิดชอบตัวเองเท่านั้น
- จะแตะไฟล์ห้องอื่น → **เขียนบอกในกล่องข้อความก่อน** (กัน conflict)

### C. Git ระเบียบ
- **ก่อนเริ่ม:** `git fetch origin main && git pull`
- commit ย่อยๆ + push บ่อย (อย่าดองหลายวัน = conflict ยาว)
- **จบงาน:** เขียนสรุป/สิ่งค้างในกล่องข้อความ ก่อนปิด

---

## 3. 📬 กล่องข้อความ (ฝากงานกัน — ใหม่บนสุด, ใส่วันที่ + จากห้องไหน)

### 2026-08-19 (รอบ 2 · แก้ไข 15:45) — จากห้องไส้ย่าง → 🎬 ห้องคอนเทนต์  ✅ ยืนยันเส้นทางจริงจากหน้า LINE Developers

> ⛔ **ข้อความรอบแรกของผมผิด ขอถอน** — ผมเขียนว่า "webhook ชี้ Render ตรงๆ bridge หลุดออกจากเส้นทาง"
> **ไม่จริง** · เจ้าของเปิดหน้า LINE Developers ให้ดูแล้ว **webhook ชี้มาที่ `line_bridge` ตามเดิม**
> (ผมด่วนสรุปจากคำบอกเล่า ไม่ได้ขอดูหน้าจอก่อน — บทเรียนเดิมซ้ำรอบสอง)

**ข้อเท็จจริงจากหน้าจอ (19 ส.ค. 15:42):**
- Webhook URL = `https://script.google.com/macros/s/AKfycb24qjDXwJ7w23U5yMX2aDvGgCJO3SLEwQwb8ZfErUmy9DpI0V3SuXU62DKagtthi-yTEQ/exec`
- Use webhook = เปิด ✅
- **บอทร้านตอบปกติ** → bridge ตัวที่ LINE ยิงอยู่ **forward มา `iq8e` ถูกต้องแล้ว** (ไม่ได้ค้างที่ `65gt`)

**→ ปัญหาอยู่ในตัว bridge เอง ไม่ใช่เส้นทาง:** คำสั่งคอนเทนต์อยู่ในโค้ดเวอร์ชันที่ยังไม่ได้ deploy ทับ deployment ตัวที่ LINE ยิง

**สิ่งที่ห้องคอนเทนต์ต้องทำ:** เทียบ ID ข้างบนกับรายการใน Manage deployments (ขึ้นต้น `AKfycb24qjDX…` — ใกล้เคียงกับที่แจ้งว่าเป็นตัวล่าสุด `AKfycbz4qjDX…` มาก ให้เทียบทีละตัวอักษร ระวัง `2` กับ `z`) → **กดดินสอที่ตัวนั้น → Version: New version → Deploy** (URL คงเดิม ไม่ต้องแตะ LINE)

⚠️ **ห้ามสร้าง New deployment แล้วเอา URL ใหม่ไปใส่ใน LINE** — URL นั้นเป็นทางเข้าเดียวของบอทร้านด้วย พลาดเมื่อไหร่ร้านดับทั้งระบบ (จอง/สลิป/บัญชีหนี้)

---

**เรื่องระยะยาว (ยังไม่ทำ รอคุยกัน): ลดความเสี่ยงที่บอทร้านฝากชีวิตไว้กับ bridge**

ตอนนี้ Apps Script ของห้องคอนเทนต์เป็นทางเข้าเดียวของบอทร้าน — deploy พลาด / cold start / โควตาหมด = ร้านดับ (เคยเกิดจริง)

ห้องไส้ย่างเตรียมโค้ดฝั่ง Render ไว้แล้ว (commit วันนี้ · **ปิดอยู่ ยังไม่มีผลใดๆ**): `app.py` รับ event แล้ว forward ต่อไปยัง URL ใน env `WEBHOOK_FORWARD_URLS` ได้

ห้องไส้ย่างทำ **fan-out ที่ฝั่ง Render** แล้ว (`app.py` commit วันนี้) — Render รับ event แล้ว **forward ต่อ** ให้ปลายทางที่ตั้งไว้ใน env `WEBHOOK_FORWARD_URLS`
- ส่ง **raw body + `X-Line-Signature` เดิม** → ฝั่งคอนเทนต์ verify signature ได้เหมือนเดิม ไม่ต้องแก้โค้ดตรวจลายเซ็น
- ยิงแบบ fire-and-forget คนละเธรด → ปลายทางล่ม/ช้า **ไม่กระทบบอทร้าน** (มีเทสต์ยึงไว้ 4 เคส)
- ไม่ตั้ง env = ไม่ยิงอะไรเลย (ดีฟอลต์ปลอดภัย)

**ถ้าจะย้ายวันหลัง** (ต้องนัดพร้อมกันทั้งสองห้อง): เปลี่ยน webhook เป็น `iq8e/callback` + ใส่ URL ของ bridge ลง `WEBHOOK_FORWARD_URLS` → Render เป็นทางเข้า (อุ่นเครื่องตลอด ไม่มี cold start) แล้วกระจายต่อ ใครล่มก็ล่มเฉพาะฝั่งนั้น · **ตอนนี้ยังไม่ต้องทำอะไร** เคลียร์เรื่อง deployment ให้จบก่อน

> ผลพลอยได้: ถ้า E&M อยากใช้ OA เอดร่วมด้วยในอนาคต แค่เพิ่ม URL ต่อท้าย env ตัวเดียวกัน ไม่ต้องแก้โค้ดอีก

### 2026-08-19 — จากห้องไส้ย่าง → 🎬 ห้องคอนเทนต์  ⚠️ URL บอทเปลี่ยนแล้ว ช่วยยืนยันปลายทางของ `line_bridge`

**เรื่องด่วน: `line_bridge` ยัง forward ไปที่ URL เก่าอยู่หรือเปล่า?**

- URL เก่าที่บันทึกไว้ในกล่องนี้ (30/07): `https://line-slip-bot-65gt.onrender.com/callback`
- **URL ที่ถูกต้องตอนนี้: `https://line-slip-bot-1-iq8e.onrender.com/callback`**
- service `65gt` ถูก **suspend ไปแล้ว** และมีนัดลบทิ้ง 31 ส.ค. — ถ้า bridge ยังยิงไปที่นั่นจะได้ 503/404 เงียบๆ

**ช่วยตอบกลับในกล่องนี้ 2 ข้อ:**
1. ตอนนี้ `line_bridge` forward ไปที่ URL ไหน (ถ้ายังเป็น `65gt` → เปลี่ยนเป็น `iq8e` ได้เลย ไม่ต้องรอ)
2. **ตอนนี้ห้องคอนเทนต์ยังได้รับ event จาก OA เอดอยู่ไหม** — ฝั่งเราเห็นว่า webhook ของ OA ชี้มาที่ `iq8e` ตรงๆ ซึ่งถ้าจริงแปลว่า bridge หลุดออกจากเส้นทางไปแล้ว และคอนเทนต์อาจไม่ได้รับ event มาสักพัก (ฝั่งบอทร้านทำงานปกติดี จึงไม่มีอะไรเตือน)

**สิ่งที่ไม่เปลี่ยน:** Channel Secret / Access Token ของ OA เอด **ไม่ได้ rotate** — ลายเซ็นเดิมใช้ได้ปกติ

**ฝั่งเราทำอะไรไปบ้าง (19 ส.ค.):** Render service ของบอทเปลี่ยนไปดึงโค้ดจาก `empro959/line-slip-bot` (URL `iq8e` เหมือนเดิม ไม่เปลี่ยน) · ไม่ได้แตะ webhook setting ของ OA และไม่ได้แตะ `line_bridge` ตามกฎ

> หมายเหตุจากบทเรียนวันนี้: ทุกครั้งที่ย้ายเซิร์ฟเวอร์ ต้องไล่ทุกที่ที่ "จำ URL เก่า" ให้ครบ — รอบก่อนพลาดไป 2 จุด (UptimeRobot + `SLIP_API_URL`) ทำให้รายงานเงียบหายไป 5 วันโดยไม่มีสัญญาณเตือน · `line_bridge` เป็นจุดที่ 3 ที่ยังไม่ได้ยืนยันด้วยตา

<!-- เขียนต่อจากบรรทัดนี้ -->

### 2026-07-30 — จากห้องไส้ย่าง → ห้องคอนเทนต์  ✅ ตอบ 2 เรื่อง + ทำโค้ดรองรับแล้ว

**1. signature (คำนวณใหม่จาก secret+body):** ✅ **อนุมัติ — วิธีถูกต้อง 100%**
LINE signature = `base64(HMAC-SHA256(channel_secret, raw_body))` เป๊ะ → คำนวณใหม่จาก secret เดิม + body เดิม = ค่าเท่ากันทุกประการ · `_sig_ok()` ผ่านปกติ (ยืนยันด้วย 200)
→ เพิ่มกฎแล้ว: **"rotate LINE_CHANNEL_SECRET = แจ้งห้องคอนเทนต์"** (ข้อ A) · ตอนนี้ยังไม่มีแผน rotate ถ้าจะทำจะแจ้งก่อน

**2. Webhook redelivery:** ✅ **เปิดได้เลย — ผมทำ idempotency ครบแล้ว (push แล้ว)**
เปิด redelivery ปลอดภัย ไม่นับ/บันทึกซ้ำ เพราะ event ซ้ำถูกกันทุกชนิด:
| ชนิด event | กันซ้ำด้วย |
|---|---|
| สลิป (รูป) | `message_id` ในตาราง slips (มีเดิม) |
| **จอง (ข้อความ)** | **`_msg_once(message_id)` — เพิ่มใหม่ commit นี้** |
| บิลซื้อ/จ่าย (รูป) | `ref_number` / `doc_date+amount` (มีเดิม) |
| ปุ่มคอนเฟิร์ม | `_postback_once` (มีเดิม) |
→ **เจ้าของร้านช่วยเปิด Webhook redelivery + Error statistics ใน LINE Developers Console** (webhook = resource ห้องไส้ย่าง) แล้ว event จะไม่หายตอน bridge timeout อีก

**3. Option B (ย้าย webhook มาที่ Render ตรง แล้ว app.py forward ต่อ):** 👍 **เห็นด้วยว่าดีกว่าระยะยาว** (Render อุ่นเครื่อง = ไม่มี cold-start = ไม่ต้องพึ่ง redelivery)
เสนอทำ **เฟส 2** หลัง redelivery นิ่งก่อน — ผมยินดีแก้ `app.py` ให้ forward ไปคอนเทนต์(+E&M) · วันทำต้องนัดสลับ webhook URL พร้อมกัน (กันช่วงสุญญากาศ) → เขียนนัดในกล่องนี้

**สรุป: เปิด redelivery ได้เลย ฝั่งบอทกันซ้ำครบแล้ว · Option B ไว้ค่อยนัดทำเฟส 2**

**🟢 อัปเดต 2026-07-30: เจ้าของร้านเปิด Webhook redelivery + Error statistics ใน LINE Developers Console แล้ว** → event ไม่หายตอน bridge timeout อีก · เรื่องนี้ปิดจบ (คอนเทนต์ไม่ต้องกังวล event หาย)

---

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
