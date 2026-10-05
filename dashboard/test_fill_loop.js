// วิธีรัน:  node dashboard/test_fill_loop.js   (จากรากรีโป · ไม่ต้องลงอะไรเพิ่ม)
// .gs ไม่มี CI — เทสต์นี้จึงต้องรันมือก่อน paste โค้ดขึ้น Apps Script ทุกครั้งที่แก้ตัววนรอบ
// 📌 เก็บไว้ในรีโปเพราะเทสต์ที่อยู่แต่ในเครื่องชั่วคราว = หายตอนคอนเทนเนอร์รีเซ็ต (เจอมาแล้ว)
// รันฟังก์ชันจริงจาก dashboard/Code_full.gs (ห้ามก๊อปโค้ดมาไว้ในเทสต์)
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync('dashboard/Code_full.gs','utf8');

let fails=0, logs=[];
function check(name, cond, extra){ if(cond) console.log('  ✅ '+name); else {fails++; console.log('  ❌ '+name+(extra?'  → '+extra:''));} }

function makeCtx(opts){
  const triggers=(opts.triggers||[]).slice();
  const props={};
  const ctx={
    console,
    Logger:{log:(m)=>logs.push(String(m))},
    Utilities:{formatDate:()=>'05/10 15:30', getUuid:()=>'x', sleep:()=>{}},
    PropertiesService:{getScriptProperties:()=>({
      getProperty:k=>(k in props?props[k]:null),
      setProperty:(k,v)=>{props[k]=v;},
      deleteProperty:k=>{delete props[k];}
    })},
    ScriptApp:{
      getProjectTriggers:()=>triggers.slice(),
      deleteTrigger:t=>{const i=triggers.indexOf(t); if(i>=0)triggers.splice(i,1);},
      newTrigger:fn=>({timeBased:()=>({everyHours:()=>({create:()=>{
        const t={getHandlerFunction:()=>fn, _hours:1}; triggers.push(t); return t;}})})})
    },
    _triggers:triggers, _props:props
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  // สตับเฉพาะตัวที่ต้องแตะ Gmail/Drive จริง — ตัวที่กำลังทดสอบไม่ถูกแตะ
  ctx._missingDays_=()=>({rows:[], all:opts.missing(), first:'2026-09-01'});
  ctx.fillMissingDays=()=>{ logs.push('[fillMissingDays ถูกเรียก]'); return opts.fill&&opts.fill(); };
  return ctx;
}

console.log('1) ไม่มีวันขาด → ห้ามตั้ง trigger');
{ logs=[]; const c=makeCtx({missing:()=>[]});
  c.startFillLoop();
  check('ไม่สร้าง trigger', c._triggers.length===0, c._triggers.length);
  check('ไม่เรียก fillMissingDays', !logs.join('').includes('[fillMissingDays'));
  check('บอกว่าไม่ต้องกู้', logs.join('').includes('ไม่มีวันที่ขาด')); }

console.log('2) มีวันขาด → ตั้ง trigger 1 ตัว + ลองรอบแรกทันที');
{ logs=[]; let left=['2026-09-14','2026-09-20'];
  const c=makeCtx({missing:()=>left});
  c.startFillLoop();
  check('มี trigger 1 ตัว', c._triggers.length===1, c._triggers.length);
  check('ชื่อ handler ถูก', c._triggers[0].getHandlerFunction()==='fillMissingDaysLoop');
  check('เรียก fillMissingDays ทันที', logs.join('').includes('[fillMissingDays'));
  check('นับรอบเป็น 1', c._props.FILL_LOOP_TRIES==='1', c._props.FILL_LOOP_TRIES); }

console.log('3) กด startFillLoop ซ้ำ → ต้องไม่ได้ trigger ซ้อน');
{ logs=[]; let left=['2026-09-14'];
  const c=makeCtx({missing:()=>left});
  c.startFillLoop(); c.startFillLoop(); c.startFillLoop();
  check('ยังมี trigger แค่ 1 ตัว', c._triggers.length===1, c._triggers.length); }

console.log('4) กู้ครบระหว่างรอบ → ปิด trigger + ล้างตัวนับเอง');
{ logs=[]; let left=['2026-09-14'];
  const c=makeCtx({missing:()=>left, fill:()=>{ left=[]; }});
  c.startFillLoop();
  check('trigger ถูกลบ', c._triggers.length===0, c._triggers.length);
  check('ตัวนับถูกล้าง', !('FILL_LOOP_TRIES' in c._props));
  check('บอกว่าครบแล้ว', logs.join('').includes('ครบทุกวันแล้ว')); }

console.log('5) ยังเหลือ → trigger ต้องอยู่ต่อ และนับรอบเพิ่ม');
{ logs=[]; let left=['2026-09-14','2026-09-20'];
  const c=makeCtx({missing:()=>left, fill:()=>{ left=left.slice(1); }});
  c.startFillLoop();
  check('trigger ยังอยู่', c._triggers.length===1);
  check('เหลือ 1 วัน', left.length===1);
  c.fillMissingDaysLoop();
  check('รอบ 2 แล้วครบ → trigger ถูกลบ', c._triggers.length===0, c._triggers.length);
  check('ไม่มีคำว่า 0 วันหลอก', !logs.join('').includes('เหลืออีก 0 วัน')); }

console.log('6) fillMissingDays พัง → ห้ามทำให้รอบถัดไปหาย');
{ logs=[]; let left=['2026-09-14'];
  const c=makeCtx({missing:()=>left, fill:()=>{ throw new Error('boom'); }});
  c.startFillLoop();
  check('trigger ยังอยู่', c._triggers.length===1, c._triggers.length);
  check('log บอกว่าพังแต่ไปต่อ', logs.join('').includes('รอบนี้พัง') && logs.join('').includes('รอบหน้าลองใหม่')); }

console.log('7) ครบเพดาน 24 รอบ → หยุดเอง ไม่วนไม่รู้จบ');
{ logs=[]; let left=['2026-09-14'];
  const c=makeCtx({missing:()=>left});
  c.startFillLoop();
  for(let i=0;i<40 && c._triggers.length;i++) c.fillMissingDaysLoop();
  check('trigger ถูกลบหลังครบเพดาน', c._triggers.length===0);
  check('รันไม่เกิน 24 รอบ', (logs.join('\n').match(/รอบอัตโนมัติครั้งที่/g)||[]).length===24,
        (logs.join('\n').match(/รอบอัตโนมัติครั้งที่/g)||[]).length);
  check('บอกว่าน่าจะไม่ใช่เรื่องโควตาแล้ว', logs.join('').includes('ไม่ใช่เรื่องโควตาแล้ว')); }

console.log('8) stopFillLoop ห้ามลบ trigger ตัวอื่นของโปรเจกต์');
{ logs=[];
  const other1={getHandlerFunction:()=>'importPosReports'};
  const other2={getHandlerFunction:()=>'rebuildNow'};
  let left=['2026-09-14'];
  const c=makeCtx({missing:()=>left, triggers:[other1,other2]});
  c.startFillLoop();
  check('ตอนตั้ง: มี 3 ตัว', c._triggers.length===3, c._triggers.length);
  c.stopFillLoop();
  check('เหลือ 2 ตัวของเดิม', c._triggers.length===2, c._triggers.length);
  check('importPosReports ยังอยู่', c._triggers.indexOf(other1)>=0);
  check('rebuildNow ยังอยู่', c._triggers.indexOf(other2)>=0); }

console.log('9) ไม่มีวันขาด แต่มี trigger ค้างจากรอบก่อน → ต้องเก็บกวาดให้');
{ logs=[];
  const stale={getHandlerFunction:()=>'fillMissingDaysLoop'};
  const keep={getHandlerFunction:()=>'importPosReports'};
  const c=makeCtx({missing:()=>[], triggers:[stale,keep]});
  c.startFillLoop();
  check('ตัวค้างถูกลบ', c._triggers.indexOf(stale)<0);
  check('ตัวอื่นไม่ถูกแตะ', c._triggers.indexOf(keep)>=0); }

console.log(fails? '\n🔴 ตก '+fails+' เคส' : '\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
