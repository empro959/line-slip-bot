// วิธีรัน:  node dashboard/test_daily_repair.js   (จากรากรีโป)
// .gs ไม่มี CI — รันมือก่อน paste ขึ้น Apps Script ทุกครั้งที่แก้ตัวซ่อมอัตโนมัติ
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync('dashboard/Code_full.gs','utf8');
let fails=0, logs=[];
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else {fails++; console.log('  ❌ '+n+(x?'  → '+x:''));} };

function ctxFor(o){
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
  c._missingDays_=()=>({rows:[], all:o.missing(), first:'2026-10-01'});
  c.fillMissingDays=()=>{ logs.push('[fillMissingDays]'); return o.fill&&o.fill(); };
  c.loadDaily_=()=>(o.dailyNow?o.dailyNow():(o.daily||[]));
  // สตับเส้นนำเข้า/เขียน เพื่อทดสอบ "วันที่มาไม่ครบ" โดยไม่แตะ Drive/Gmail จริง
  c._importPosOneDate_=(iso)=>{ logs.push('[import]'+iso); if(o.onImport) o.onImport(iso); };
  c.writeMonths_=()=>{ logs.push('[writeMonths]'); };
  c.rebuildMonths_=()=>[];
  c.fetchSlipMap_=()=>({});
  c.rebuildNow=()=>{};
  c.findLatestPdf_=()=>o.latest===undefined?{name:'SaleReport_09102026.pdf',time:Date.parse('2026-10-10T01:29:00Z')}:o.latest;
  return c;
}

console.log('1) setupDailyRepair ตั้ง trigger 1 ตัว · กดซ้ำไม่ซ้อน');
{ logs=[]; const c=ctxFor({missing:()=>[]});
  c.setupDailyRepair(); c.setupDailyRepair(); c.setupDailyRepair();
  const mine=c._t.filter(t=>t.getHandlerFunction()==='dailyRepair');
  chk('มี dailyRepair ตัวเดียว', mine.length===1, mine.length); }

console.log('2) setupDailyRepair ห้ามลบ trigger ตัวอื่น');
{ logs=[]; const imp={getHandlerFunction:()=>'importPosReports'};
  const wk={getHandlerFunction:()=>'sendWeeklyAlert'};
  const c=ctxFor({missing:()=>[], triggers:[imp,wk]});
  c.setupDailyRepair();
  chk('importPosReports ยังอยู่', c._t.indexOf(imp)>=0);
  chk('sendWeeklyAlert ยังอยู่', c._t.indexOf(wk)>=0); }

console.log('3) dailyRepair: ไม่มีวันขาด → เงียบ ไม่ throw');
{ logs=[]; const c=ctxFor({missing:()=>[]});
  let threw=false; try{ c.dailyRepair(); }catch(e){ threw=true; }
  chk('ไม่ throw', !threw);
  chk('ไม่เรียก fillMissingDays', !logs.join('').includes('[fillMissingDays]')); }

console.log('4) dailyRepair: กู้ครบ → ไม่ throw');
{ logs=[]; let left=['2026-10-09'];
  const c=ctxFor({missing:()=>left, fill:()=>{ left=[]; }});
  let threw=false; try{ c.dailyRepair(); }catch(e){ threw=true; }
  chk('ไม่ throw', !threw);
  chk('บอกว่าเก็บครบ', logs.join('').includes('เก็บครบแล้ว')); }

console.log('5) dailyRepair: ยังขาด → ต้อง throw (ให้ Apps Script ส่งเมลแจ้ง ห้ามเงียบ)');
{ logs=[]; let left=['2026-10-09','2026-10-10'];
  const c=ctxFor({missing:()=>left});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('throw จริง', !!msg, msg);
  chk('บอกวันที่ขาดในข้อความ', msg.includes('2026-10-09') && msg.includes('2026-10-10'), msg);
  chk('บอกทางแก้ถ้าเป็นวันร้านหยุด', msg.includes('CLOSED_DAYS')); }

console.log('6) dailyRepair: fillMissingDays พัง → ยังต้อง throw ไม่กลืนเงียบ');
{ logs=[]; let left=['2026-10-09'];
  const c=ctxFor({missing:()=>left, fill:()=>{ throw new Error('OCR quota'); }});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('throw จริง', !!msg, msg);
  chk('log เก็บ error ไว้', logs.join('').includes('OCR quota')); }

console.log('7) fixPosNightly: ด่านกันซ้ำบล็อกอยู่ → ต้องบอกออกมา');
{ logs=[]; const c=ctxFor({missing:()=>[], props:{LAST_IMPORTED_EMAIL_MS:String(Date.parse('2026-10-11T00:00:00Z'))}});
  c.fixPosNightly();
  chk('เตือนว่าใบนี้นำเข้าไปแล้ว', logs.join('').includes('ถูกนำเข้าไปแล้ว')); }

console.log('8) fixPosNightly: ไม่มี trigger importPosReports → ต้องฟ้อง');
{ logs=[]; const c=ctxFor({missing:()=>[]});
  c.fixPosNightly();
  chk('ฟ้องว่าไม่มี importPosReports', logs.join('').includes('ไม่มี importPosReports')); }

console.log('9) fixPosNightly: หาอีเมลไม่เจอ → ต้องฟ้อง');
{ logs=[]; const c=ctxFor({missing:()=>[], latest:null});
  c.fixPosNightly();
  chk('ฟ้องว่าหาไฟล์ SaleReport ไม่เจอ', logs.join('').includes('หาไฟล์ SaleReport ในเมลไม่เจอ')); }

console.log('10) fixPosNightly ต้องตั้งตัวซ่อมให้ด้วยในตัวเดียว');
{ logs=[]; const c=ctxFor({missing:()=>[]});
  c.fixPosNightly();
  chk('มี dailyRepair แล้ว', c._t.some(t=>t.getHandlerFunction()==='dailyRepair')); }

// ═══ "วันที่มาไม่ครบ" — QA เจอ 14/09 ที่ตัวซ่อมมองไม่เห็นตลอดกาล (§3.46) ═══
const DAY_NOPAY={date:'2026-09-14',period:'กันยายน 2569',payments:{},sales:[{category:'ส้มตำ',amount:34926}]};
const DAY_OK   ={date:'2026-09-15',period:'กันยายน 2569',payments:{'เงินสด':100},sales:[{category:'ส้มตำ',amount:100}]};

console.log('11) _incompleteDays_: มียอดขายแต่ไม่มีวิธีชำระ → todo · วันครบ → ไม่นับ');
{ const c=ctxFor({missing:()=>[], daily:[DAY_NOPAY,DAY_OK]});
  const r=c._incompleteDays_();
  chk('เจอ 14/09', r.todo.join()==='2026-09-14', JSON.stringify(r));
  chk('ไม่นับวันที่ครบ', r.todo.indexOf('2026-09-15')<0); }

console.log('12) ยอดขาย 0 + ไม่มีวิธีชำระ → ไม่นับ (ไม่มีข้อมูลเลย คนละโรค)');
{ const c=ctxFor({missing:()=>[], daily:[{date:'2026-09-20',payments:{},sales:[]}]});
  chk('ไม่นับ', c._incompleteDays_().todo.length===0); }

console.log('13) expenses=null แต่มีวิธีชำระ → ไม่นับ (ตั้งใจ · กู้ใหม่ก็ยัง null = วนเปล่า)');
{ const c=ctxFor({missing:()=>[], daily:[{date:'2026-09-21',payments:{'เงินสด':5},expenses:null,sales:[{category:'ก',amount:5}]}]});
  chk('ไม่นับ', c._incompleteDays_().todo.length===0); }

console.log('14) ลองกู้ครบโควตาแล้ว → ไป gaveup ไม่ใช่ todo (ห้ามวนกู้ตลอดกาล)');
{ const c=ctxFor({missing:()=>[], daily:[DAY_NOPAY], props:{POS_NOPAY_TRIES:'{"2026-09-14":3}'}});
  const r=c._incompleteDays_();
  chk('ไม่อยู่ใน todo', r.todo.length===0, JSON.stringify(r));
  chk('อยู่ใน gaveup', r.gaveup.join()==='2026-09-14'); }

console.log('15) _fillIncompleteDays_: เรียกตัวนำเข้า + นับครั้งที่พยายาม');
{ logs=[]; const c=ctxFor({missing:()=>[], daily:[DAY_NOPAY]});
  c._fillIncompleteDays_();
  chk('เรียกตัวนำเข้าของวันนั้น', logs.join().indexOf('[import]2026-09-14')>=0, logs.join(' | '));
  chk('จดครั้งที่พยายาม = 1', JSON.parse(c._p.POS_NOPAY_TRIES||'{}')['2026-09-14']===1, c._p.POS_NOPAY_TRIES); }

console.log('16) กู้สำเร็จ → ล้างตัวนับ + สร้างเดือนใหม่');
{ logs=[]; let data=[JSON.parse(JSON.stringify(DAY_NOPAY))];
  const c=ctxFor({missing:()=>[], dailyNow:()=>data,
    onImport:()=>{ data=[Object.assign({},DAY_NOPAY,{payments:{'เงินสด':34926}})]; }});
  const r=c._fillIncompleteDays_();
  chk('นับว่าสำเร็จ 1 วัน', r.done===1, JSON.stringify(r));
  chk('ล้างตัวนับแล้ว', !JSON.parse(c._p.POS_NOPAY_TRIES||'{}')['2026-09-14'], c._p.POS_NOPAY_TRIES);
  chk('สร้างข้อมูลเดือนใหม่', logs.join().indexOf('[writeMonths]')>=0); }

console.log('17) dailyRepair: ไม่มีวันขาด แต่มีวันมาไม่ครบ → ต้องลองกู้ และ throw ถ้ายังไม่ครบ');
{ logs=[]; const c=ctxFor({missing:()=>[], daily:[DAY_NOPAY]});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('ลองกู้จริง', logs.join().indexOf('[import]2026-09-14')>=0);
  chk('throw จริง', !!msg, msg);
  chk('ข้อความบอกว่าเป็นโรค "ไม่มีวิธีชำระ"', msg.indexOf('ไม่มีวิธีชำระ')>=0, msg); }

console.log('18) dailyRepair: เหลือแต่วันที่เลิกพยายามแล้ว → ห้าม throw แต่ต้องรายงาน');
{ logs=[]; const c=ctxFor({missing:()=>[], daily:[DAY_NOPAY], props:{POS_NOPAY_TRIES:'{"2026-09-14":3}'}});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('ไม่ throw (ไม่เตือนทุกเช้าเรื่องที่เครื่องแก้ไม่ได้)', !msg, msg);
  chk('ยังรายงานใน log', logs.join().indexOf('เลิกพยายามแล้ว')>=0, logs.join(' | '));
  chk('ไม่เรียกตัวนำเข้าซ้ำ', logs.join().indexOf('[import]')<0); }

console.log('19) dailyRepair: กู้วันมาไม่ครบสำเร็จ → ไม่ throw');
{ logs=[]; let data=[JSON.parse(JSON.stringify(DAY_NOPAY))];
  const c=ctxFor({missing:()=>[], dailyNow:()=>data,
    onImport:()=>{ data=[Object.assign({},DAY_NOPAY,{payments:{'เงินสด':34926}})]; }});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('ไม่ throw', !msg, msg);
  chk('บอกว่าเก็บครบแล้ว', logs.join().indexOf('เก็บครบแล้ว')>=0); }

console.log('20) ทิศกลับ: ไม่มีทั้งวันขาดและวันมาไม่ครบ → เงียบ ไม่ throw ไม่เรียกอะไร');
{ logs=[]; const c=ctxFor({missing:()=>[], daily:[DAY_OK]});
  let msg=''; try{ c.dailyRepair(); }catch(e){ msg=String(e.message||e); }
  chk('ไม่ throw', !msg, msg);
  chk('ไม่เรียกตัวนำเข้า', logs.join().indexOf('[import]')<0);
  chk('ไม่เรียก fillMissingDays', logs.join().indexOf('[fillMissingDays]')<0); }

console.log(fails? '\n🔴 ตก '+fails+' เคส' : '\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
