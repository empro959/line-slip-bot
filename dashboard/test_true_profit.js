// วิธีรัน:  node dashboard/test_true_profit.js   (จากรากรีโป)
// กำไรจริงของร้าน + สรุป 5 กลุ่ม (คำสั่งเจ้าของ 3 พ.ย. 2569) — เรียกฟังก์ชันตัวจริงจากไฟล์แดชบอร์ด
// ⚠️ ตัวเลขชุดนี้คือ "เกณฑ์ผ่าน" ที่เจ้าของให้มาสำหรับเดือน ต.ค. 2569 — ผิดแม้บาทเดียวถือว่าไม่ผ่าน
const fs=require('fs'), vm=require('vm');

// ดึงเฉพาะบล็อกตัวคำนวณออกมารัน — ไม่ต้องมี DOM
const html=fs.readFileSync('dashboard/saisang_dashboard.html','utf8');
const a=html.indexOf('// ═══ กำไรจริงของร้าน');
const b=html.indexOf('// ═══ GOOGLE DRIVE SYNC ═══');
if(a<0||b<0||b<=a){ console.log('🔴 หาบล็อกตัวคำนวณในไฟล์แดชบอร์ดไม่เจอ'); process.exit(1); }
const ctx={}; vm.createContext(ctx); vm.runInContext(html.slice(a,b), ctx);

let fails=0;
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else { fails++; console.log('  ❌ '+n+(x?'  → '+x:'')); } };
const eq=(n,got,want)=>chk(n+'  = '+want, got===want, 'ได้ '+got);

// ── เดือน ต.ค. 2569 ตามตัวเลขที่เจ้าของยืนยัน ──
const OCT={ period:'ตุลาคม 2569', total_sales:2257907, expense_categories:[
  {category:'วัตถุดิบ',            amount:414038},
  {category:'เครื่องดื่ม',          amount:683142},
  {category:'พนักงานหน้าร้าน',      amount:300000},
  {category:'ครัว',                amount:200000},
  {category:'นักร้อง',              amount:120000},
  {category:'ก๋วยเตี๋ยว',           amount:60000},
  {category:'CEO',                 amount:50000},
  {category:'พีอาร์',               amount:24512},
  {category:'ค่าเช่า(กย)',          amount:75000},
  {category:'ค่าสาธารณูปโภค(กย)',   amount:55850},
  {category:'ค่าภาษี',              amount:28382},
  {category:'คชจ.ในร้าน',           amount:70000},
  {category:'ของสิ้นเปลือง',        amount:40000},
  {category:'BNI',                 amount:12450},
  {category:'เก็บออมร้านญี่ปุ่น',    amount:135871},
  {category:'ชำระหนี้',             amount:31300},
]};

console.log('\n▶ เกณฑ์ผ่านของเจ้าของ — ต.ค. 2569');
const r=ctx.mF(OCT);
eq('ยอดขาย',            r.sales,  2257907);
eq('ค่าใช้จ่ายจริง',      r.exp,    2133374);
eq('กำไร',              r.profit,  124533);
eq('โอน/ชำระหนี้ รวม',   r.xfer,    167171);
chk('กำไร +5.5%',       r.margin.toFixed(1)==='5.5', 'ได้ '+r.margin.toFixed(1));

console.log('\n▶ ภาษีคงเหลือ (ข้อ 3)');
eq('VAT ขาย 7/107',      r.vatOut,    147714);
eq('ค่าภาษีที่จ่าย',       r.taxPaid,    28382);
eq('ภาษีคงเหลือ',        r.taxDue,    119332);
eq('กำไรหลังกันภาษี',     r.profitNet,   5201);
chk('ค่าภาษียังนับเป็นค่าใช้จ่ายในบิลประจำเดือน (ไม่หักซ้ำ)', r.g.bill===159232);
chk('มีรายการค่าภาษีให้เจ้าของตรวจ', r.taxRows.length===1 && r.taxRows[0].amount===28382);

console.log('\n▶ 5 กลุ่ม');
eq('วัตถุดิบ',        r.g.raw,   414038);
eq('เครื่องดื่ม',      r.g.drink, 683142);
eq('ค่าแรง',          r.g.labor, 754512);
eq('บิลประจำเดือน',    r.g.bill,  159232);
eq('อื่นๆ',           r.g.other, 122450);
eq('ไม่เข้ากลุ่ม',      0,  0);
chk('5 กลุ่มรวมแล้วเท่ากับค่าใช้จ่ายจริง',
    r.g.raw+r.g.drink+r.g.labor+r.g.bill+r.g.other+0 === r.exp);

console.log('\n▶ โอน/ชำระหนี้ แยกบรรทัด');
const sav=r.xferRows.find(x=>x.category.indexOf('เก็บออม')===0);
const deb=r.xferRows.find(x=>x.category.indexOf('ชำระหนี้')===0);
eq('เก็บออมร้านญี่ปุ่น', sav&&sav.amount, 135871);
eq('ชำระหนี้',         deb&&deb.amount,  31300);

console.log('\n▶ กฎเหล็ก');
chk('ไม่แตะข้อมูลดิบ — expense_categories เท่าเดิม', OCT.expense_categories.length===16);
chk('total_expenses เดิมที่เก็บไว้ ไม่ถูกเขียนทับ', OCT.total_expenses===undefined);
const once=JSON.stringify(ctx.mF(OCT)), twice=JSON.stringify(ctx.mF(OCT));
chk('เรียกซ้ำได้ผลเท่าเดิม (ฟังก์ชันบริสุทธิ์)', once===twice);

console.log('\n▶ ชื่อหมวดจริงที่ไม่ตรงกับชื่อย่อในใบสั่งงาน');
// 🪤 ของจริงในไฟล์คือ 'พนักงานครัว' และ 'พนง.ก๋วยเตี๋ยว' ไม่ใช่ 'ครัว'/'ก๋วยเตี๋ยว'
//    จับแบบ "ขึ้นต้นด้วย" จะหลุดทั้งคู่ = ค่าแรงหาย 928,012 บาท (ยอด 4 เดือน ม.ค.-เม.ย.)
const REAL={period:'ตุลาคม 2569', total_sales:0, expense_categories:[
  {category:'พนักงานครัว',amount:100},{category:'พนง.ก๋วยเตี๋ยว',amount:50},
  {category:'ค่าเช่า(กย)',amount:20}]};
const rr=ctx.mF(REAL);
eq('พนักงานครัว + พนง.ก๋วยเตี๋ยว → ค่าแรง', rr.g.labor, 150);
eq('ค่าเช่า(กย) → บิลประจำเดือน',          rr.g.bill,   20);

console.log('\n▶ หมวดใหม่ต้องไม่ทำให้ยอดไม่บรรจบ');
const UNK={period:'ตุลาคม 2569', total_sales:100, expense_categories:[
  {category:'วัตถุดิบ',amount:10},{category:'หมวดใหม่ที่ไม่เคยมี',amount:7}]};
const u=ctx.mF(UNK);
eq('ยังนับในค่าใช้จ่ายรวม', u.exp, 17);
eq('ตกลงถัง อื่นๆ',        u.g.other, 7);
chk('5 กลุ่มยังบรรจบกับยอดรวม',
    u.g.raw+u.g.drink+u.g.labor+u.g.bill+u.g.other === u.exp);
chk('แต่ยังมีรายชื่อให้เจ้าของเห็นว่ายังไม่ได้จัดกลุ่ม',
    u.otherRows.length===1 && u.otherRows[0].category==='หมวดใหม่ที่ไม่เคยมี');
chk('หมวดที่ตั้งใจให้อยู่ อื่นๆ ไม่ต้องขึ้นเตือน',
    ctx.mF({total_sales:0,expense_categories:[{category:'BNI',amount:1},
      {category:'ของสิ้นเปลือง',amount:1},{category:'คชจ.ในร้าน',amount:1},
      {category:'อื่นๆ',amount:1}]}).otherRows.length===0);

console.log('\n▶ ธง "รอเจ้าของยืนยัน" (ห้ามแก้เอง)');
const F={period:'ตุลาคม 2569', total_sales:0, expense_categories:[
  {category:'ค่าเช่า(มิย)',        amount:90000},
  {category:'ค่าสาธารณูปโภค(มิย)', amount:55232},
  {category:'ค่าเช่า(กย)',         amount:90000},
  {category:'ค่าภาษี',             amount:1},
]};
const fl=ctx.mFlags(F);
eq('ติดธง 2 รายการ', fl.length, 2);
chk('(กย) ในงวด ต.ค. = ปกติ ไม่ติดธง', !fl.some(x=>x.category.indexOf('(กย)')>=0));
chk('(มิย) ในงวด ต.ค. = ติดธง',        fl.some(x=>x.category==='ค่าเช่า(มิย)'));
chk('ข้อความบอกเหตุผล',                 /ห่าง 4 เดือน/.test(fl[0].note), fl[0]&&fl[0].note);
const F2={period:'มกราคม 2570', total_sales:0, expense_categories:[{category:'ค่าเช่า(ธค)',amount:1}]};
chk('ข้ามปี: (ธค) ในงวด ม.ค. = ปกติ', ctx.mFlags(F2).length===0);

// ══ กลุ่ม 'บำรุงรักษา' + ภาษีที่ปนกัน (เจ้าของสั่ง/ยืนยัน 12/11/2569) ══
console.log('\n7) กลุ่ม "บำรุงรักษา" แยกออกจาก "อื่นๆ"');
{
  const M={period:'มกราคม 2569', total_sales:1070, expense_categories:[
    {category:'ค่าบำรุงรักษา', amount:1300},
    {category:'คชจ.สาขานิมมานฯ', amount:3346},
    {category:'คชจ.ในร้าน', amount:100}]};
  const r=ctx.mF(M);
  chk('ค่าบำรุงรักษาเข้ากลุ่ม maint', r.g.maint===1300, r.g.maint);
  chk('ไม่ตกไปอยู่ "อื่นๆ"', r.g.other===3446, r.g.other);
  chk('ยอดค่าใช้จ่ายรวมยังบรรจบ', r.exp===4746, r.exp);
  // เจ้าของยืนยัน 12/11/2569: คชจ.สาขานิมมานฯ นับรวม และอยู่ 'อื่นๆ' ได้ ไม่ต้องติดธงแล้ว
  chk('คชจ.สาขานิมมานฯ ไม่ติดธง "ยังไม่ได้จัดกลุ่ม"',
      !r.otherRows.some(x=>x.category==='คชจ.สาขานิมมานฯ'),
      JSON.stringify(r.otherRows));
}

console.log('\n8) 🔴 ภาษี: "ค่าภาษี" มี VAT + 50 ทวิ ปนกัน → ต้องบอกเป็นช่วง ห้ามเดาสัดส่วน');
{
  const M={period:'มกราคม 2569', total_sales:107000, expense_categories:[
    {category:'ค่าภาษี', amount:3000}]};
  const r=ctx.mF(M);
  chk('VAT ขาย 7/107 = 7,000', r.vatOut===7000, r.vatOut);
  chk('ต่ำสุด = หักที่จ่ายไปทั้งก้อน (4,000)', r.taxDueMin===4000, r.taxDueMin);
  chk('สูงสุด = ไม่หักเลย (7,000)', r.taxDueMax===7000, r.taxDueMax);
  chk('ช่วงกว้างเท่ายอดที่จ่าย', r.taxDueMax-r.taxDueMin===r.taxPaid);
  // ⚠️ ตั้งใจไม่เปลี่ยน 'กำไรหลังกันภาษี' ไปใช้ตัวสูงสุด — จะพลิกบางเดือนเป็นขาดทุนบนหน้าจอ
  //    ซึ่งเป็นการตีความ ไม่ใช่สิ่งที่เจ้าของสั่ง (ถ้าสั่งเมื่อไรค่อยสลับไป taxDueMax)
  chk('กำไรหลังกันภาษียังคิดจากตัวต่ำสุด', r.profitNet===(r.sales-r.exp)-r.taxDueMin, r.profitNet);
}

console.log('\n9) ป้าย "แยก N กลุ่ม" ต้องนับจาก EXP_GROUPS ไม่ใช่ฮาร์ดโค้ด');
{
  const html=require('fs').readFileSync('dashboard/saisang_dashboard.html','utf8');
  chk('มีช่อง #grp-n บนหน้าจอ', /id="grp-n"/.test(html));
  chk('เขียนค่าจาก EXP_GROUPS.length+1',
      /getElementById\('grp-n'\)[\s\S]{0,80}EXP_GROUPS\.length\s*\+\s*1/.test(html));
  chk('ไม่มีเลขกลุ่มฮาร์ดโค้ดค้างอยู่', !/แยก 5 กลุ่ม/.test(html));
}

console.log(fails?('\n🔴 ไม่ผ่าน '+fails+' เคส'):'\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
