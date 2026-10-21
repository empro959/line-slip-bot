// วิธีรัน:  node dashboard/test_qa_pos.js   (จากรากรีโป)
// .gs ไม่มี CI — รันมือก่อน paste ขึ้น Apps Script ทุกครั้งที่แก้ตัว QA
// เรียก qaFindings_/dailyQa/setupDailyQa "ตัวจริง" จาก Code_full.gs ไม่ใช่สำเนาลอจิกในเทสต์
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync('dashboard/Code_full.gs','utf8');
let fails=0, logs=[];
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else {fails++; console.log('  ❌ '+n+(x?'  → '+x:''));} };

function ctxFor(o){
  o=o||{};
  const triggers=(o.triggers||[]).slice(), props=Object.assign({}, o.props||{});
  const c={ console,
    Logger:{log:m=>logs.push(String(m))},
    Utilities:{formatDate:(d,tz,f)=>new Date(d).toISOString().slice(0,10), getUuid:()=>'x', sleep:()=>{}},
    PropertiesService:{getScriptProperties:()=>({
      getProperty:k=>(k in props?props[k]:null), setProperty:(k,v)=>{props[k]=v;}, deleteProperty:k=>{delete props[k];}})},
    ScriptApp:{ getProjectTriggers:()=>triggers.slice(),
      deleteTrigger:t=>{const i=triggers.indexOf(t); if(i>=0)triggers.splice(i,1);},
      newTrigger:fn=>({timeBased:()=>({atHour:()=>({nearMinute:()=>({everyDays:()=>({create:()=>{
        const t={getHandlerFunction:()=>fn}; triggers.push(t); return t;}})})}),
        everyHours:()=>({create:()=>{const t={getHandlerFunction:()=>fn}; triggers.push(t); return t;}})})})},
    _t:triggers, _p:props };
  vm.createContext(c); vm.runInContext(src, c);
  // QA อ่าน 2 แหล่ง — ป้อนตรงๆ ไม่แตะ Drive จริง
  c.loadDaily_=()=>o.daily||[];
  c.getFile_=()=>o.months===undefined?null:{getBlob:()=>({getDataAsString:()=>JSON.stringify(o.months)})};
  return c;
}
const codes=f=>f.map(x=>x.code);
const lv=(f,code)=>(f.find(x=>x.code===code)||{}).level;

// ───── เคสจริงจากใบ SaleReport_2026813.pdf (ถอด text layer มาจริง ไม่ได้พิมพ์เอง) ─────
// Cash 22,323 · Prompay 70,118 · Credit Card 7,450 · Customer Ledger 402 · Other 3,343
// รวม = 103,636.00 · Total Sale By Group = 103,636.50  → ต่าง 0.50 จากปัดเศษรายหมวด
const DAY_813={date:'2026-08-13',period:'สิงหาคม 2569',
  payments:{'เงินสด':22323,'เงินโอน':70118,'บัตรเครดิต':7450,'ค้างชำระ':402,'อื่นๆ':3343},
  sales:[{category:'ส้มตำ',amount:50000},{category:'ของทอด',amount:53636.5}]};
const MONTH_813=()=>({period:'สิงหาคม 2569',total_sales:103636.5,
  payment_methods:[{method:'เงินสด',amount:22323},{method:'เงินโอน',amount:70118},
                   {method:'บัตรเครดิต',amount:7450},{method:'ค้างชำระ',amount:402},{method:'อื่นๆ',amount:3343}],
  payment_days:[{date:'2026-08-13',payments:DAY_813.payments}],
  daily_totals:[{date:'2026-08-13',sales:103636.5}]});

console.log('1) ใบจริง 13 ส.ค. → ต้องไม่มีเรื่องแดงเลย (ทิศกลับ: กัน false alarm)');
{ const c=ctxFor({}); const f=c.qaFindings_([DAY_813],[MONTH_813()]);
  const red=f.filter(x=>x.level==='แดง');
  chk('ไม่มีแดง', red.length===0, JSON.stringify(red));
  chk('ต่าง 0.50 ไม่ถูกฟ้องว่าบวกลบไม่ลงตัว', codes(f).indexOf('PAY_SUM')<0); }

console.log('2) ใบจริงมีเงินในช่องที่บอทไม่เทียบ → ต้องเตือนเหลือง + บอกจำนวนเงิน');
{ const c=ctxFor({}); const f=c.qaFindings_([DAY_813],[MONTH_813()]);
  chk('มี HIDDEN_PAY', codes(f).indexOf('HIDDEN_PAY')>=0);
  chk('เป็นเหลือง ไม่ใช่แดง', lv(f,'HIDDEN_PAY')==='เหลือง', lv(f,'HIDDEN_PAY'));
  const m=(f.find(x=>x.code==='HIDDEN_PAY')||{}).msg||'';
  chk('บอกยอด 3,745 (402+3,343)', m.indexOf('3,745')>=0, m); }

console.log('3) จุด 4: อ่านเลขผิด 1 ช่อง → บวกลบไม่ลงตัว ต้องเป็นแดง');
{ const c=ctxFor({});
  const bad=JSON.parse(JSON.stringify(DAY_813)); bad.payments['เงินโอน']=10118;   // 70,118 → 10,118
  const f=c.qaFindings_([bad],[MONTH_813()]);
  chk('มี PAY_SUM', codes(f).indexOf('PAY_SUM')>=0);
  chk('เป็นแดง', lv(f,'PAY_SUM')==='แดง');
  chk('บอกส่วนต่างในข้อความ', ((f.find(x=>x.code==='PAY_SUM')||{}).msg||'').indexOf('60,000')>=0,
      (f.find(x=>x.code==='PAY_SUM')||{}).msg); }

console.log('4) ช่องรับติดลบ = เป็นไปไม่ได้ → แดง');
{ const c=ctxFor({});
  const bad=JSON.parse(JSON.stringify(DAY_813)); bad.payments['เงินสด']=-5465;
  const f=c.qaFindings_([bad],[MONTH_813()]);
  chk('มี PAY_NEG', codes(f).indexOf('PAY_NEG')>=0);
  chk('เป็นแดง', lv(f,'PAY_NEG')==='แดง'); }

console.log('5) ชื่อช่องชำระเงินแปลกปลอม (ตัวแกะ/OCR เพี้ยน) → เหลือง ไม่กลืนเงียบ');
{ const c=ctxFor({});
  const bad=JSON.parse(JSON.stringify(DAY_813));
  delete bad.payments['อื่นๆ']; bad.payments['อ่ืนๆ']=3343;      // สระเพี้ยน
  const f=c.qaFindings_([bad],[MONTH_813()]);
  chk('มี PAY_KEY', codes(f).indexOf('PAY_KEY')>=0);
  chk('มีเงินติดอยู่ = แดง (บอทมองไม่เห็นเงินก้อนนี้)', lv(f,'PAY_KEY')==='แดง', lv(f,'PAY_KEY'));
  chk('บอกยอดที่ติดอยู่', ((f.find(x=>x.code==='PAY_KEY')||{}).msg||'').indexOf('3,343')>=0);
  chk('ไม่ฟ้อง PAY_SUM ซ้ำ — เอกสารยังบวกลบลงตัว (1 เช็ค 1 โรค)', codes(f).indexOf('PAY_SUM')<0,
      (f.find(x=>x.code==='PAY_SUM')||{}).msg); }

console.log('5b) ช่องแปลกปลอมที่ยอด 0 = ไม่มีเงินติดอยู่ → เหลืองพอ ไม่ต้องส่งเมล');
{ const c=ctxFor({});
  const bad=JSON.parse(JSON.stringify(DAY_813)); bad.payments['ทชท']=0;
  const f=c.qaFindings_([bad],[MONTH_813()]);
  chk('เป็นเหลือง', lv(f,'PAY_KEY')==='เหลือง', lv(f,'PAY_KEY')); }

console.log('6) อาการเคสจริง 9/10: มียอดขายแต่ไม่มีวิธีชำระ → แดง');
{ const c=ctxFor({});
  const d={date:'2026-10-09',period:'ตุลาคม 2569',payments:{},sales:[{category:'ส้มตำ',amount:103481}]};
  const f=c.qaFindings_([d],[{period:'ตุลาคม 2569',total_sales:103481,payment_methods:[],
    payment_days:[],daily_totals:[{date:'2026-10-09',sales:103481}]}]);
  chk('มี NO_PAY', codes(f).indexOf('NO_PAY')>=0);
  chk('อธิบายอาการที่เจ้าของเห็น', ((f.find(x=>x.code==='NO_PAY')||{}).msg||'').indexOf('ไม่มียอด POS')>=0); }

console.log('7) วันซ้ำใน pos_daily → แดง (rebuildMonths_ จะบวกซ้ำทั้งวัน)');
{ const c=ctxFor({}); const f=c.qaFindings_([DAY_813,DAY_813],[MONTH_813()]);
  chk('มี DUP_DAY', codes(f).indexOf('DUP_DAY')>=0);
  chk('เป็นแดง', lv(f,'DUP_DAY')==='แดง'); }

console.log('8) วันที่กับงวดไม่ตรงกัน (ตระกูลบั๊ก §3.12) → แดง');
{ const c=ctxFor({});
  const bad=JSON.parse(JSON.stringify(DAY_813)); bad.period='กรกฎาคม 2569';
  const f=c.qaFindings_([bad],[MONTH_813()]);
  chk('มี PERIOD_MISMATCH', codes(f).indexOf('PERIOD_MISMATCH')>=0); }

console.log('9) จุด 2: วันหายไปจาก saisang_data.json → แดง');
{ const c=ctxFor({}); const f=c.qaFindings_([DAY_813],[]);
  chk('มี LOST_DAY', codes(f).indexOf('LOST_DAY')>=0);
  chk('เป็นแดง', lv(f,'LOST_DAY')==='แดง'); }

console.log('10) จุด 2: ยอดรวมเดือน ≠ ผลบวกรายวัน → แดง');
{ const c=ctxFor({}); const m=MONTH_813(); m.total_sales=99999;
  const f=c.qaFindings_([DAY_813],[m]);
  chk('มี MONTH_SUM', codes(f).indexOf('MONTH_SUM')>=0); }

console.log('11) จุด 2: ช่องเงินรวมเดือน ≠ ผลบวกรายวัน → แดง');
{ const c=ctxFor({}); const m=MONTH_813();
  m.payment_methods[1].amount=70000;        // เงินโอน 70,118 → 70,000
  const f=c.qaFindings_([DAY_813],[m]);
  chk('มี PAY_ROLLUP', codes(f).indexOf('PAY_ROLLUP')>=0);
  chk('บอกว่าช่องไหน', ((f.find(x=>x.code==='PAY_ROLLUP')||{}).msg||'').indexOf('เงินโอน')>=0); }

console.log('12) จุด 3: ไม่มีใน daily_totals → posday คืน sales=0 ซึ่งอ่านว่า "ขายได้ 0"');
{ const c=ctxFor({}); const m=MONTH_813(); m.daily_totals=[];
  const f=c.qaFindings_([DAY_813],[m]);
  chk('มี NO_SALES', codes(f).indexOf('NO_SALES')>=0);
  chk('ย้ำกฎ 0 ≠ ไม่รู้', ((f.find(x=>x.code==='NO_SALES')||{}).msg||'').indexOf('ไม่ใช่ "ไม่รู้"')>=0); }

console.log('13) ข้อมูลค้างใน saisang_data ที่ไม่มีใน pos_daily → เหลือง');
{ const c=ctxFor({}); const m=MONTH_813();
  m.payment_days.push({date:'2026-08-12',payments:{'เงินสด':1}});
  m.daily_totals.push({date:'2026-08-12',sales:1}); m.total_sales=103637.5;
  m.payment_methods[0].amount=22324;
  const f=c.qaFindings_([DAY_813],[m]);
  chk('มี ORPHAN_DAY', codes(f).indexOf('ORPHAN_DAY')>=0);
  chk('เป็นเหลือง', lv(f,'ORPHAN_DAY')==='เหลือง'); }

console.log('14) dailyQa: ไม่มีแดง → ต้องไม่ throw');
{ logs=[]; const c=ctxFor({daily:[DAY_813],months:[MONTH_813()]});
  let threw=''; try{ c.dailyQa(); }catch(e){ threw=String(e.message||e); }
  chk('ไม่ throw', !threw, threw);
  chk('บอกว่าผ่าน', logs.join('').indexOf('QA ผ่าน')>=0); }

console.log('15) dailyQa: มีแต่เหลือง → ห้าม throw (เตือนทุกวันเรื่องที่ไม่ผิด = ไม่มีใครเชื่ออีก)');
{ logs=[]; const c=ctxFor({daily:[DAY_813],months:[MONTH_813()]});
  const f=c.qaFindings_([DAY_813],[MONTH_813()]);
  chk('เคสนี้มีเหลืองจริง', f.filter(x=>x.level==='เหลือง').length>0);
  let threw=''; try{ c.dailyQa(); }catch(e){ threw=String(e.message||e); }
  chk('ยังไม่ throw', !threw, threw); }

console.log('16) dailyQa: มีแดง → ต้อง throw (ให้ Apps Script ส่งเมลแจ้ง ห้ามเงียบ)');
{ logs=[]; const bad=JSON.parse(JSON.stringify(DAY_813)); bad.payments['เงินโอน']=10118;
  const c=ctxFor({daily:[bad],months:[MONTH_813()]});
  let threw=''; try{ c.dailyQa(); }catch(e){ threw=String(e.message||e); }
  chk('throw จริง', !!threw, threw);
  chk('บอกวิธีดูต่อ', threw.indexOf('qaPosData')>=0, threw); }

console.log('17) QA ต้องไม่เขียนข้อมูลอะไรเลย (ตัวตรวจที่แก้ของที่ตรวจ ไม่ใช่ตัวตรวจ)');
{ const c=ctxFor({daily:[DAY_813],months:[MONTH_813()]});
  let wrote=false;
  c.saveDaily_=()=>{wrote=true;}; c.writeMonths_=()=>{wrote=true;}; c.writeMonth_=()=>{wrote=true;};
  c.qaPosData();
  chk('ไม่เรียกตัวเขียนเลย', !wrote); }

console.log('18) setupDailyQa: กดซ้ำ 3 ครั้งได้ trigger ตัวเดียว · ห้ามลบตัวอื่น');
{ logs=[]; const imp={getHandlerFunction:()=>'importPosReports'};
  const rep={getHandlerFunction:()=>'dailyRepair'};
  const c=ctxFor({triggers:[imp,rep]});
  c.setupDailyQa(); c.setupDailyQa(); c.setupDailyQa();
  chk('มี dailyQa ตัวเดียว', c._t.filter(t=>t.getHandlerFunction()==='dailyQa').length===1);
  chk('importPosReports ยังอยู่', c._t.indexOf(imp)>=0);
  chk('dailyRepair ยังอยู่', c._t.indexOf(rep)>=0); }

console.log('19) ไม่มีข้อมูลเลย → บอกให้ไปเช็คบัญชี/โฟลเดอร์ ไม่ใช่รายงานว่า "ผ่าน"');
{ logs=[]; const c=ctxFor({daily:[],months:[]});
  const r=c.qaPosData();
  chk('ไม่มีแดง', r.red.length===0);
  chk('เตือนว่าไม่มีข้อมูล', logs.join('').indexOf('ไม่มีข้อมูลให้ตรวจ')>=0); }

// ───── เคสจริงจากการกวาดรอบแรก 21 ต.ค. (pos_daily 110 วัน · saisang_data 10 เดือน) ─────
// เดือน ม.ค.-มิ.ย. 69 นำเข้าจากใบ "รายเดือน" (buildMonth_) → ไม่มี daily_totals/payment_days
// QA รอบแรกฟ้องแดง 11 เรื่องว่า 'ผลบวกรายวัน 0' = เตือนหลอก ต้องไม่เกิดอีก
const MONTH_MAY=()=>({period:'พฤษภาคม 2569',total_sales:2376243,
  payment_methods:[{method:'Prompay',amount:2005407},{method:'เงินสด',amount:309065},
                   {method:'บัตรเครดิต',amount:40845},{method:'ค้างชำระ',amount:33919},{method:'อื่นๆ',amount:17113}]});

console.log('20) เดือนที่มาจากใบรายเดือน → ห้ามฟ้อง MONTH_SUM/PAY_ROLLUP (เตือนหลอก 21 ต.ค.)');
{ const c=ctxFor({}); const f=c.qaFindings_([],[MONTH_MAY()]);
  chk('ไม่ฟ้อง MONTH_SUM', codes(f).indexOf('MONTH_SUM')<0,
      (f.find(x=>x.code==='MONTH_SUM')||{}).msg);
  chk('ไม่ฟ้อง PAY_ROLLUP', codes(f).indexOf('PAY_ROLLUP')<0,
      (f.find(x=>x.code==='PAY_ROLLUP')||{}).msg);
  chk('บอกเหตุผลเป็นเหลือง', lv(f,'MONTH_NO_DAYS')==='เหลือง', lv(f,'MONTH_NO_DAYS')); }

console.log('21) เคสจริง พ.ค. 69: ช่อง "Prompay" ไม่ถูกแปลงเป็น "เงินโอน" → แดง');
{ const c=ctxFor({}); const f=c.qaFindings_([],[MONTH_MAY()]);
  chk('มี PAY_KEY_MONTH', codes(f).indexOf('PAY_KEY_MONTH')>=0);
  chk('เป็นแดง', lv(f,'PAY_KEY_MONTH')==='แดง', lv(f,'PAY_KEY_MONTH'));
  const m=(f.find(x=>x.code==='PAY_KEY_MONTH')||{}).msg||'';
  chk('บอกยอด 2,005,407', m.indexOf('2,005,407')>=0, m);
  chk('บอกผลกระทบว่าอ่าน "เงินโอน" จะได้ 0', m.indexOf('เงินโอน')>=0 && m.indexOf('0')>=0);
  chk('ช่องชื่อไทยอื่นไม่ถูกฟ้อง', f.filter(x=>x.code==='PAY_KEY_MONTH').length===1); }

console.log('22) ทิศกลับของข้อ 20: เดือนที่มีรายวันแล้วรวมไม่ตรง → ยังต้องฟ้องแดง');
{ const c=ctxFor({}); const m=MONTH_813(); m.total_sales=99999;
  const f=c.qaFindings_([DAY_813],[m]);
  chk('ยังฟ้อง MONTH_SUM', codes(f).indexOf('MONTH_SUM')>=0);
  chk('ไม่ติดป้าย MONTH_NO_DAYS', codes(f).indexOf('MONTH_NO_DAYS')<0); }

console.log('23) รายงานต้องรวมกลุ่ม — 108 บรรทัดเรื่องเดียวกันคือ log ที่ไม่มีใครอ่าน');
{ logs=[];
  const days=[], pays=[], tots=[];
  for(let i=1;i<=30;i++){ const dt='2026-09-'+String(i).padStart(2,'0');
    const pm={'เงินสด':1000,'ค้างชำระ':100};
    days.push({date:dt,period:'กันยายน 2569',payments:pm,sales:[{category:'ส้มตำ',amount:1100}]});
    pays.push({date:dt,payments:pm}); tots.push({date:dt,sales:1100}); }
  const c=ctxFor({daily:days,months:[{period:'กันยายน 2569',total_sales:33000,
    payment_methods:[{method:'เงินสด',amount:30000},{method:'ค้างชำระ',amount:3000}],
    payment_days:pays,daily_totals:tots}]});
  c.qaPosData();
  const out=logs.join('\n');
  chk('มีบรรทัดหัวกลุ่มบอกจำนวน', out.indexOf('[HIDDEN_PAY] 30 รายการ')>=0, out.slice(0,300));
  chk('หัวกลุ่มบอกยอดรวมเป็นบาท', /\[HIDDEN_PAY\] 30 รายการ · รวม ~3,000 บาท/.test(out));
  chk('ไม่กางทั้ง 30 บรรทัด', out.split('ในช่องค้างชำระ/อื่นๆ').length-1 <= 3,
      String(out.split('ในช่องค้างชำระ/อื่นๆ').length-1));
  logs=[]; c.qaPosDataAll();
  chk('qaPosDataAll กางครบ 30', logs.join('\n').split('ในช่องค้างชำระ/อื่นๆ').length-1 === 30); }

console.log('24) qaDay: เจาะดูวันเดียว — เรื่องเงินห้ามเดา ต้องเห็นของจริง');
{ logs=[]; const c=ctxFor({daily:[DAY_813]});
  const r=c.qaDay('2026-08-13');
  chk('คืนส่วนต่างถูก (103,636 − 103,636.50 = −0.50)', Math.abs(r.diff+0.5)<0.001, String(r.diff));
  chk('โชว์ทุกช่องชำระเงิน', logs.join('').indexOf('ค้างชำระ')>=0);
  chk('โชว์ยอดขายรายหมวด', logs.join('').indexOf('ส้มตำ')>=0);
  chk('โชว์ตัวเลขที่อาจอธิบายส่วนต่าง', logs.join('').indexOf('ส่วนลด')>=0);
  logs=[]; chk('ไม่มีวันนั้น → บอกชัด ไม่ throw', c.qaDay('2026-01-01')===null && logs.join('').indexOf('ไม่มีวันที่')>=0); }

console.log(fails?('\n🔴 ไม่ผ่าน '+fails+' เช็ค'):'\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
