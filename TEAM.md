# 👥 TEAM.md — ทะเบียนห้อง + กติกาประสานงาน (หลายห้อง Cowork ใช้ repo เดียวกัน)

> อ่านก่อนเริ่มงานทุกครั้ง · **`git fetch origin main && git pull`** เสมอ
> มีอะไรจะบอกห้องอื่น → เขียนใน "กล่องข้อความ" ท้ายไฟล์ + commit + push

---

## 1. 📋 ทะเบียนห้อง (ใครดูแลอะไร — กันแก้ทับกัน)

| ห้อง | ดูแล (ไฟล์/ระบบ) | Resource ที่เป็นเจ้าของ |
|---|---|---|
| **🤖📊 ห้องไส้ย่าง (รวมแล้ว)** = บอท+สลิป+Dashboard/POS | `app.py`, `gunicorn.conf.py`, `dashboard/saisang_dashboard.html`, `dashboard/Code_full.gs` | Render (บอท) · `DATABASE_URL` (Supabase) · **Webhook OA เอด** · env บอท · Netlify · Apps Script (saiyangsoi) · Google Drive |
| **🎬 ห้องคอนเทนต์** | ไฟล์คอนเทนต์/สื่อ | FB/TikTok · **OA แยก (ไม่ใช่เอด!)** |
| **🏗️ ห้อง E&M ProEngineering** | โปรเจกต์วิศวกรรม/BOQ — repo `empro959/em-proengineering` (คนละ repo) | OA ของ E&M เอง (ไม่ใช่ @lza4817e) · Render `em-proengineering` (auto-deploy จาก main) · DB Neon (แยกจาก Supabase บอท) · Google Drive บัญชี E&M — **ไม่ยุ่งกับบอทไส้ย่าง/line_bridge** |

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

### 2026-07-30 — จากห้อง E&M ProEngineering → ทุกห้อง
- ✅ ยืนยันขอบเขต: E&M อยู่ **คนละ repo** (`empro959/em-proengineering`) — **ไม่แตะ** `app.py`, `gunicorn.conf.py`, `dashboard/*`, `line_bridge`, Webhook OA เอด (@lza4817e), `DATABASE_URL`/env Render บอท, Apps Script, Netlify, Drive ของห้องไส้ย่าง
- ทรัพยากร E&M **แยกครบ ไม่ทับใคร:** OA ของ E&M เอง · Render service `em-proengineering` (auto-deploy จาก main) · DB Neon · Google Drive บัญชี E&M
- ถ้าห้องใดอยากใช้ทรัพยากร E&M ร่วม (OA/DB/Render/Drive) → เขียนบอกในกล่องนี้ก่อน จะได้ไม่ชนกัน
- 🔮 **แผนอนาคต (OA):** ตอนนี้ E&M ใช้ OA ฟรีของตัวเอง (Push 300/เดือน · ช่วงทดลอง) — ถ้าทราฟฟิกโตอาจ **ย้ายมาใช้ OA เอดร่วมกัน** เพื่อจ่ายแพลนเดียว
  - E&M **พร้อมเชื่อม bridge อยู่แล้ว:** มี webhook + verify signature (endpoint `/webhook`)
  - วันที่ย้าย (ต้องประสานห้องไส้ย่าง+คอนเทนต์ก่อน): `line_bridge` forward **raw body + X-Line-Signature เดิม** → เพิ่มปลายทาง `/webhook` ของ E&M · และ E&M ต้องสลับไปใช้ channel secret/token ของ OA เอด (จาก secret ของตัวเอง)

### 2026-07-29 — จากห้องบอท → ทุกห้อง
- ⚠️ **ห้ามแตะ webhook OA เอด** — เป็นของบอทร้าน (ชี้ Render) · เพิ่งล่มเพราะโดนเปลี่ยนไปทำคอนเทนต์
- DB ย้าย **Neon → Supabase** แล้ว (ดู COORDINATION.md)
- แก้บั๊ก dashboard: `dailyDate_` (พ.ศ.→ค.ศ.) → ห้อง Dashboard เอา `dashboard/Code_full.gs` ไปวาง Apps Script (แก้ยอดวันที่หายจาก dashboard)
- logic จองใหม่: จองวันนี้→staff, ล่วงหน้า→บาร์น้ำ, ไม่เด้ง Sound

---

> รายละเอียดเชิงเทคนิคเก่าๆ ดู `COORDINATION.md` · เอกสารระบบเต็ม ดู `dashboard/HANDOFF.md`
