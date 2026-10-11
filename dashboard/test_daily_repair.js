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
  c.loadDaily_=()=>o.daily||[];
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

console.log(fails? '\n🔴 ตก '+fails+' เคส' : '\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
