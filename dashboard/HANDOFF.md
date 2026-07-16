# HANDOFF — ระบบ Dashboard การเงิน + วิเคราะห์ (ห้อง dashboard → รวมห้อง)

> เอกสารส่งต่อสำหรับรวม 2 ห้อง (ห้องบอท app.py ↔ ห้อง dashboard การเงิน)
> ครอบคลุมทุกอย่างที่ห้อง dashboard สร้าง — อ่านไฟล์นี้ก่อนทำงานต่อ

---

## 1. ภาพรวมระบบ (2 subsystems เชื่อมกัน)

| ระบบ | ที่อยู่ | หน้าที่ |
|---|---|---|
| 🤖 บอทเช็คสลิป | Render (`line-slip-bot-65gt.onrender.com`) · `app.py` | เช็คสลิป (Gemini) · รับจอง · แจ้งเตือน LINE |
| 📊 Dashboard การเงิน | Netlify (`inspiring-mooncake-52e91e.netlify.app`) | แสดงผลรายรับ-รายจ่าย + วิเคราะห์ |
| ⚙️ ตัวดึง POS | Google Apps Script (บัญชี saiyangsoi) · `Code_full.gs` | Gmail→OCR→parse→Drive JSON |
| ☁️ ที่เก็บข้อมูล | Google Drive (saiyangsoi) | `pos_daily.json` + `saisang_data.json` |

**เส้นเชื่อม:** Apps Script `fetch <bot>/api/slip_daily` (เทียบสลิป) · `<bot>/api/push_owner` (ส่งแจ้งเตือน)

---

## 2. ไฟล์ในโฟลเดอร์นี้

- `Code_full.gs` — โค้ด Apps Script ทั้งหมด (วางใน script.google.com ของ saiyangsoi) · **ไม่มีความลับ** (token/url อยู่ใน Script Properties)
- `saisang_dashboard.html` — Dashboard (deploy Netlify) · `SYNC_URL` ถูก sanitize เป็น placeholder → ตัวจริงตั้งผ่านปุ่ม **☁️ ตั้งค่า Drive** (เก็บใน localStorage) หรือฝังตอน deploy
- `HANDOFF.md` — ไฟล์นี้

> `app.py` (บอท) อยู่ที่ root ของ repo — ห้องบอทดูแล

---

## 3. Data model

**`pos_daily.json`** (รายวันดิบ):
```
[{ date:"2026-07-10", period:"กรกฎาคม 2569",
   sales:[{category,amount,exc,vat}], expenses:[{category,amount,...}],
   payments:{เงินโอน,เงินสด,...}, menu:[{name,category,qty,amount}],
   zones:{โซน:ยอด}, voids:{cancelled,deleted,returns,discount}, bills:89 }]
```

**`saisang_data.json`** (รายเดือน — Dashboard อ่านตัวนี้):
```
[{ period, total_sales, total_expenses, net_profit,
   expense_categories[], sales_categories[], payment_methods[], payment_days[],
   daily_totals:[{date,sales,expenses,profit,food,raw,bev,bevc,bills}],
   menu_items[], zone_sales[], voids{}, recon{} }]
```
`daily_totals` = หัวใจของกราฟรายวัน/สัปดาห์ (food=ยอดขายอาหารไม่รวมญี่ปุ่น/เครื่องดื่ม, raw=ต้นทุนวัตถุดิบ, bev=ยอดขายเครื่องดื่ม, bevc=ต้นทุนเครื่องดื่ม)

---

## 4. Env / Config (ที่เจ้าของตั้งไว้)

**Render (บอท) Environment:**
- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` — OA1 (บอทเช็คสลิป)
- `GEMINI_API_KEY`, `PROMPTPAY_API_KEY`
- `SLIP_API_TOKEN` — รหัสให้ Apps Script เรียก /api/slip_daily + /api/push_owner
- `LINE_ADMIN_USER_ID` = `C91b4fc6...` (**กลุ่ม management**)
- `LINE_ADMIN_PUSH_TOKEN` = **access token ของ OA2** (แจ้งเตือนส่งผ่าน OA2 เพราะ OA2 อยู่ในกลุ่ม management · ถ้าไม่ตั้ง = ใช้ OA1)
- `SLIP_GROUPS`, `RESV_GROUPS`, `PAYABLE_GROUPS`, ฯลฯ

**Apps Script > Project Settings > Script Properties:**
- `SLIP_API_URL` = `<bot>/api/slip_daily?token=<SLIP_API_TOKEN>`

**Apps Script > Services:** เปิด **Drive API** (Advanced) — จำเป็นสำหรับ OCR

---

## 5. ฟังก์ชัน Apps Script สำคัญ

| ฟังก์ชัน | หน้าที่ |
|---|---|
| `importPosReports` | **trigger ตี 1 ทุกวัน** — ดึง POS วันล่าสุด + สรุป + แจ้งเตือน |
| `setupDailyTrigger` | ตั้ง trigger (ตี 1 daily + จันทร์ 9 โมง weekly) — รันครั้งเดียว |
| `backfillPos` | เก็บย้อนหลัง (ข้ามวันเก่า/เดือน Excel โดยไม่ OCR + retry rate limit) |
| `backfillBills` | เติมจำนวนบิลย้อนหลัง (OCR รายงานลูกค้า) |
| `rebuildNow` | สร้าง saisang_data.json ใหม่จาก pos_daily (ไม่ OCR) |
| `setSlipUrl` | ตั้ง SLIP_API_URL ลง Script Properties |
| `juneUseExcel` / `dropPosMonth_` | ปลดเดือนออกจาก POS ให้ Excel คุม |
| `sendDailyAlert` / `buildDailyMsg_` | สรุป+เตือนอัจฉริยะ (เทียบค่าเฉลี่ยร้าน) เข้า LINE |
| `analyzeWeekday` | ต้นทุนแฝง (เงินรั่ว) แยกวันในสัปดาห์ |
| `profitByWeekday` | กำไร/ขาดทุน + ยอดต่อบิล แยกวันในสัปดาห์ |
| `beverageWatch` | กำไรเครื่องดื่มรายเดือน (เป้า 60%) |
| `forecastCashflow` | พยากรณ์เงินสด + จุดลดต้นทุน |
| `debugDaily` / `debugReports` | ตรวจข้อมูล/ไฟล์รายงาน (ไม่ OCR) |

---

## 6. ⚠️ Gotchas สำคัญ (ห้ามลืม)

1. **ชื่อไฟล์รายงาน POS = วันเนื้อหา +1** (POS พิมพ์เช้าวันรุ่งขึ้น) → suffix = `YYYY`+เดือน+วัน ของ (วันธุรกิจ+1) · โค้ด backfill map ด้วย offset นี้
2. **มิถุนายน = Excel** (รัน `juneUseExcel` ปลดออกจาก POS แล้ว) → POS ห้ามแตะ June อีก
3. **หนึ่งเดือน = หนึ่งแหล่งข้อมูล** (POS ออโต้ **หรือ** Excel อัปมือ) อย่าให้ชนกัน
4. **Google OCR มี rate limit** — backfill ข้ามวันที่เก็บแล้วโดยไม่ OCR + retry พัก 60 วิ (สูงสุด 2 ครั้ง) · อย่ากดรันรัวๆ
5. **Dashboard กันแคช** — `fetch(...&_=Date.now(), {cache:'no-store'})`
6. **แจ้งเตือนใช้ OA2** (`LINE_ADMIN_PUSH_TOKEN`) เพราะกลุ่ม management มีแต่ OA2 · OA1 push เข้าไม่ได้ (400)
7. **อย่ากด "บันทึกขึ้น Drive"** ตอนจอโหลดข้อมูลไม่ครบ → ทับข้อมูล POS
8. รายงาน POS **ไม่มี** ชื่อพนักงาน/รายชั่วโมง → ทำ peak-hour / จับทุจริตรายคน **ไม่ได้**

---

## 7. Deploy

- **Dashboard:** zip (`index.html` + `_headers`) → ลากขึ้น Netlify · เปิดเติม `?v=N` กันแคช HTML
- **Apps Script:** วาง `Code_full.gs` ทับ → Save → run

---

## 8. สรุปข้อมูลธุรกิจ (วิเคราะห์ล่าสุด)

- **ร้านเท่าทุน** (cost ratio ~100%) — แต่รวม "ชำระหนี้ UOB" (หนี้) + "CEO" (ค่าแรงเจ้าของ) ในต้นทุน → กำไรดำเนินงานจริงดีกว่านั้น
- **เครื่องดื่ม ~57% ของยอดขาย · margin 46%** (ปกติของบาร์เบียร์/เหล้าขวด) → เล่นที่ราคา+ปริมาณ · โปร "5+1" กินกำไร
- **3 คืนขาดทุน (จ/พ/พฤ) ~86k/เดือน** = จุดลดต้นทุนใหญ่สุด (ค่าแรงวันธรรมดาไม่ยืดหยุ่นตามยอด)
- **เสาร์ cost ratio 95%** (สูงผิดปกติเทียบศุกร์ 73%) — ต้นทุนเสาร์มีอะไรเกิน
- **อาหารญี่ปุ่น = ฝากขาย** (กำไร 15% จ่ายคืนร้านญี่ปุ่น = หมวด "เก็บออมญี่ปุ่น")

---

## 9. งานค้าง

- 🧾 **ต้นทุนเมนู** → ทำ Menu Engineering (ดาวเด่น/ตัวถ่วง) — รอเจ้าของส่งต้นทุน
- 💡 แผนลดต้นทุนวันธรรมดา — เป็นการตัดสินใจฝั่งเจ้าของ

## 10. เสร็จแล้ว (ทำงานอยู่)
บอทเช็คสลิป · POS อัตโนมัติตี 1 · แจ้งเตือนอัจฉริยะ→กลุ่ม management (OA2) · Dashboard + กราฟรายวัน/สัปดาห์ (รายรับ-จ่าย, อาหาร/เครื่องดื่ม เทียบต้นทุน, กำไรแยกวันในสัปดาห์) · โหมดสว่าง-มืด · เครื่องมือวิเคราะห์ครบ (analyzeWeekday, profitByWeekday, beverageWatch, forecastCashflow)
