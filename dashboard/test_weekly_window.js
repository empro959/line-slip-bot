// วิธีรัน:  node dashboard/test_weekly_window.js   (จากรากรีโป)
// สรุปรายสัปดาห์ต้อง 'รันกี่รอบก็ได้เลขเดียวกัน' + รายงานต้องไม่ออกซ้ำ
// 🔴 เคสจริง 23/11/2569: สรุปรายสัปดาห์ออก 09:01 กับ 09:03 ได้ ฿697,944 กับ ฿600,003
//    ต่างกัน 97,941 = ยอดของวันที่หลุดออกจากหน้าต่างพอดี (มีแถววันใหม่โผล่ระหว่างสองรอบ)
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync('dashboard/Code_full.gs','utf8');
let fails=0, logs=[];
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else {fails++; console.log('  ❌ '+n+(x?'  → '+x:''));} };

const TODAY='2026-11-23';
function ctx(o){
  o=o||{};
  // ต้องมี SLIP_API_URL/OWNER_EMAILS ไม่งั้น pushOwner_/emailOwners_ ออกตั้งแต่ต้น ไม่ได้เทสอะไรเลย
  const props=Object.assign({SLIP_API_URL:'https://x.test/api/slip_daily',
                            OWNER_EMAILS:'owner@test'}, o.props||{});
  let locked=false;
  const c={ console, Logger:{log:m=>logs.push(String(m))},
    Utilities:{formatDate:(d,tz,f)=>TODAY},          // ตรึง 'วันนี้' ให้เทสไม่เน่าตามเวลา
    PropertiesService:{getScriptProperties:()=>({
      getProperty:k=>(k in props?props[k]:null), setProperty:(k,v)=>{props[k]=v;}})},
    LockService:{getScriptLock:()=>({
      waitLock:()=>{ if(locked && !o.lockFree) throw new Error('busy'); locked=true; },
      releaseLock:()=>{ locked=false; }})},
    ScriptApp:{getProjectTriggers:()=>[]},
    UrlFetchApp:{fetch:()=>{ (o.pushed||(o.pushed=[])).push(1);
      return {getResponseCode:()=>200,getContentText:()=>'{"ok":true}'}; }},
    MailApp:{sendEmail:()=>{ (o.mails||(o.mails=[])).push(1); }},
    _p:props };
  vm.createContext(c); vm.runInContext(src, c);
  c.loadDaily_=()=>(o.daily||[]);
  return c;
}
const day=(date,sales,exp,del)=>({date, sales:[{amount:sales}], expenses:[{amount:exp}],
                                  voids:{deleted:del||0}});
// 16–22 พ.ย. มีข้อมูลครบ (วันเก่าสุด 16/11 = 97,941 คือวันที่เคยหลุดออกไป)
const WEEK=[ day('2026-11-16',97941,7655), day('2026-11-17',80000,78000),
             day('2026-11-18',85000,80000), day('2026-11-19',169515,150000,473),
             day('2026-11-20',90000,85000), day('2026-11-21',88000,84000),
             day('2026-11-22',87902,71659) ];
const salesOf=m=>Number((m.match(/ยอดขายรวม: ฿([\d,]+)/)||[])[1].replace(/,/g,''));

console.log('1) 🔴 แถววันใหม่โผล่ระหว่างสองรอบ → ยอดต้องเท่าเดิม');
{ const a=ctx({daily:WEEK}).buildWeeklyMsg_();
  const b=ctx({daily:WEEK.concat([day(TODAY,0,0)])}).buildWeeklyMsg_();
  chk('ยอดขายรวมเท่ากันทั้งสองรอบ', salesOf(a)===salesOf(b), salesOf(a)+' vs '+salesOf(b));
  chk('ได้ยอดของช่วง 16–22 ครบ', salesOf(a)===698358, salesOf(a));
  // ยอดวันนี้ใหญ่ๆ ต้องไม่เข้ามาปน — วันนี้ยังขายไม่จบ เอามาถัวเฉลี่ยไม่ได้
  const big=ctx({daily:WEEK.concat([day(TODAY,999999,0)])}).buildWeeklyMsg_();
  chk('ยอดของวันนี้ไม่ถูกนับ', salesOf(big)===698358, salesOf(big)); }

console.log('2) การ์ดต้องบอกช่วงวันที่ ตรวจสอบย้อนหลังได้');
{ const m=ctx({daily:WEEK}).buildWeeklyMsg_();
  chk('มีช่วงวันที่บนการ์ด', /2026-11-16 → 2026-11-22/.test(m), m.split('\n')[0]);
  chk('ไม่เขียนลอยๆ ว่า "7 วันล่าสุด"', !/7 วันล่าสุด/.test(m)); }

console.log('3) วันที่หายไปจากช่วง ต้องบอก ไม่ใช่หารเฉลี่ยเงียบๆ');
{ const m=ctx({daily:WEEK.filter(d=>d.date!=='2026-11-18')}).buildWeeklyMsg_();
  chk('บอกว่าไม่มีข้อมูลกี่วัน', /ไม่มีข้อมูล 1 วัน/.test(m), m);
  chk('บอกวันไหน', /2026-11-18/.test(m));
  chk('บอกว่ายอดรวมต่ำกว่าจริง', /ต่ำกว่าจริง/.test(m)); }

console.log('4) ไม่มีข้อมูลเลยทั้งสัปดาห์ = เส้นนำเข้าพัง ห้ามเงียบ');
{ const m=ctx({daily:[day('2026-01-01',100,50)]}).buildWeeklyMsg_();
  chk('ยังตอบ ไม่ใช่ null', !!m);
  chk('ฟ้องว่าเส้นนำเข้าน่าจะพัง', /ไม่มีข้อมูลสักวัน/.test(m), m); }

console.log('5) 🔴 รายงานต้องไม่ออกซ้ำ แม้ trigger ยิงสองรอบ');
{ const o={daily:WEEK, lockFree:true}; const c=ctx(o);
  c.sendWeeklyAlert(); c.sendWeeklyAlert(); c.sendWeeklyAlert();
  chk('push ออกครั้งเดียว', (o.pushed||[]).length===1, (o.pushed||[]).length);
  chk('อีเมลออกครั้งเดียว', (o.mails||[]).length===1, (o.mails||[]).length);
  chk('บอกเหตุผลที่ไม่ส่งซ้ำ', logs.some(l=>/ส่งไปแล้วรอบ/.test(l))); }

console.log('6) รายวันกับรายสัปดาห์กันซ้ำแยกกัน ไม่บล็อกกันเอง');
{ const o={daily:WEEK, lockFree:true}; const c=ctx(o);
  c.buildDailyMsg_=()=>'📊 สรุปยอดขาย (ทดสอบ)';
  c.sendWeeklyAlert(); c.sendDailyAlert();
  chk('ออกทั้งสองใบ', (o.pushed||[]).length===2, (o.pushed||[]).length); }

console.log('7) วันถัดไปส่งได้ตามปกติ (มาร์คผูกกับวันที่)');
{ const o={daily:WEEK, lockFree:true, props:{SENT_weekly:'2026-11-16'}}; const c=ctx(o);
  c.sendWeeklyAlert();
  chk('ส่งออก', (o.pushed||[]).length===1, (o.pushed||[]).length);
  chk('มาร์คด้วยวันสุดท้ายของช่วง', c._p.SENT_weekly==='2026-11-22', c._p.SENT_weekly); }

console.log('9) คืนเดียวนำเข้าหลายวัน → การ์ดของคนละวันต้องส่งได้ทั้งคู่');
{ const o={daily:WEEK, lockFree:true}; const c=ctx(o);
  c.buildDailyMsg_=()=>'📊 สรุปยอดขาย 2026-11-21';  c.sendDailyAlert();
  c.buildDailyMsg_=()=>'📊 สรุปยอดขาย 2026-11-21';  c.sendDailyAlert();   // วันเดิม = ซ้ำ
  chk('วันเดิมส่งใบเดียว', (o.pushed||[]).length===1, (o.pushed||[]).length);
  c.buildDailyMsg_=()=>'📊 สรุปยอดขาย 2026-11-22';  c.sendDailyAlert();   // คนละวัน = ส่งได้
  chk('คนละวันส่งเพิ่มได้', (o.pushed||[]).length===2, (o.pushed||[]).length); }

console.log('8) จับล็อกไม่ได้ (อีกรอบกำลังส่ง) → ไม่ส่งซ้ำ และบอกเหตุผล');
{ logs=[]; const o={daily:WEEK}; const c=ctx(o);
  c.sendWeeklyAlert();                 // รอบแรกจับล็อกค้างไว้ (stub ไม่ปล่อยถ้า lockFree=false)
  const before=(o.pushed||[]).length;
  c.sendWeeklyAlert();
  chk('ไม่ push เพิ่ม', (o.pushed||[]).length===before, (o.pushed||[]).length);
  chk('มีเหตุผลใน log', logs.some(l=>/ไม่ส่งซ้ำ/.test(l)), logs.join(' | ')); }

console.log(fails?('\n❌ ตก '+fails+' ข้อ'):'\n✅ ผ่านหมด');
process.exit(fails?1:0);
