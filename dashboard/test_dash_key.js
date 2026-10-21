// วิธีรัน:  node dashboard/test_dash_key.js   (จากรากรีโป)
// ด่านคีย์ขาเขียนของแดชบอร์ด (§3.47) — เรียก doPost ตัวจริงจาก Code_full.gs
// ⚠️ จุดนี้คือ "ประตูเดียวที่เขียนทับข้อมูลการเงินทั้งปีได้" — พลาดแล้วเงินทั้งปีหาย
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync('dashboard/Code_full.gs','utf8');
let fails=0, logs=[], warns=[];
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else {fails++; console.log('  ❌ '+n+(x?'  → '+x:''));} };

function ctxFor(o){
  o=o||{};
  const props=Object.assign({}, o.props||{});
  const written=[];
  const c={ console:{log:m=>logs.push(String(m)), warn:m=>warns.push(String(m)), error:m=>warns.push(String(m))},
    Logger:{log:m=>logs.push(String(m))},
    Utilities:{getUuid:()=>'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', formatDate:()=>'x'},
    PropertiesService:{getScriptProperties:()=>({
      getProperty:k=>(k in props?props[k]:null), setProperty:(k,v)=>{props[k]=v;}, deleteProperty:k=>{delete props[k];}})},
    ContentService:{ createTextOutput:t=>({setMimeType:()=>({_body:t})}), MimeType:{JSON:'json'} },
    _w:written, _p:props };
  vm.createContext(c); vm.runInContext(src, c);
  // ไฟล์ปลายทาง — จับทุกการเขียนไว้ตรวจ
  c.getFile_=()=>(o.noFile?null:{ setContent:p=>{written.push(p);}, getLastUpdated:()=>({getTime:()=>1}) });
  c.getFolder_=()=>({ createFile:(n,p)=>{written.push(p); return {getLastUpdated:()=>({getTime:()=>2})};} });
  return c;
}
const post=(c,body)=>JSON.parse(c.doPost({postData:{contents:typeof body==='string'?body:JSON.stringify(body)}})._body);
const KEY='aaaaaaaabbbbccccddddeeeeeeeeeeee';

console.log('1) ไม่ได้ตั้ง DASH_KEY → ปฏิเสธ (fail-safe: ไม่มีคีย์ = ไม่ผ่าน ไม่ใช่ผ่านหมด)');
{ logs=[];warns=[]; const c=ctxFor({});
  const r=post(c,{action:'save',data:[{period:'ก'}]});
  chk('ok=false', r.ok===false, JSON.stringify(r));
  chk('status=denied', r.status==='denied');
  chk('ไม่เขียนไฟล์เลย', c._w.length===0, JSON.stringify(c._w));
  chk('บอกวิธีแก้ (makeDashKey)', (r.error||'').indexOf('makeDashKey')>=0, r.error);
  chk('มีร่องรอยใน log', warns.join().indexOf('ปฏิเสธคำขอเขียน')>=0); }

console.log('2) คีย์ผิด → ปฏิเสธ + ห้ามเขียน');
{ logs=[];warns=[]; const c=ctxFor({props:{DASH_KEY:KEY}});
  const r=post(c,{action:'save',k:'ผิด',data:[{period:'ก'}]});
  chk('status=denied', r.status==='denied', JSON.stringify(r));
  chk('ไม่เขียนไฟล์', c._w.length===0);
  chk('บอกให้ไปวางคีย์ใหม่ที่หน้า dashboard', (r.error||'').indexOf('☁️')>=0, r.error); }

console.log('3) คีย์ไม่มาเลย (คนนอกยิง body เปล่า) → ปฏิเสธ');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  const r=post(c,{action:'save',data:[{period:'ก'}]});
  chk('status=denied', r.status==='denied');
  chk('ไม่เขียนไฟล์', c._w.length===0); }

console.log('4) คีย์ถูก + data เป็น array → เขียนได้');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  const r=post(c,{action:'save',k:KEY,data:[{period:'สิงหาคม 2569',total_sales:1}]});
  chk('ok=true', r.ok===true, JSON.stringify(r));
  chk('เขียน 1 ครั้ง', c._w.length===1, String(c._w.length)); }

console.log('5) 🪤 บั๊กแฝง: คีย์ถูกแต่ไม่มี data → ห้ามเขียนคำว่า "undefined" ทับข้อมูลทั้งปี');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  const r=post(c,{action:'save',k:KEY});
  chk('status=bad_payload', r.status==='bad_payload', JSON.stringify(r));
  chk('ไม่เขียนไฟล์เลย', c._w.length===0, JSON.stringify(c._w));
  chk('บอกว่าไม่ได้เขียนอะไรลงไฟล์', (r.error||'').indexOf('ไม่ได้เขียน')>=0, r.error); }

console.log('6) data เป็น object/สตริง (ไม่ใช่ array) → ไม่รับ');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  chk('object → bad_payload', post(c,{k:KEY,data:{a:1}}).status==='bad_payload');
  chk('string → bad_payload', post(c,{k:KEY,data:'xxx'}).status==='bad_payload');
  chk('null → bad_payload', post(c,{k:KEY,data:null}).status==='bad_payload');
  chk('ไม่เขียนไฟล์เลยทั้ง 3 เคส', c._w.length===0, JSON.stringify(c._w)); }

console.log('7) 🔑 คีย์ต้องไม่ถูกเขียนลงไฟล์ข้อมูล');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  post(c,{action:'save',k:KEY,data:[{period:'ก'}]});
  chk('payload ไม่มีคีย์', c._w[0].indexOf(KEY)<0, c._w[0]);
  chk('payload ไม่มีฟิลด์ k', c._w[0].indexOf('"k"')<0, c._w[0]);
  chk('payload เป็น array ของเดือนตามเดิม', JSON.parse(c._w[0])[0].period==='ก'); }

console.log('8) array ว่าง = ลบทุกเดือน → ยอมให้ทำ (เจ้าของมีคีย์) แต่ต้องมีร่องรอย');
{ logs=[];warns=[]; const c=ctxFor({props:{DASH_KEY:KEY}});
  const r=post(c,{action:'save',k:KEY,data:[]});
  chk('ok=true', r.ok===true);
  chk('มีคำเตือนว่าลบทุกเดือน', warns.join().indexOf('ลบทุกเดือน')>=0, warns.join(' | ')); }

console.log('9) body ไม่ใช่ JSON → ต้องไม่ระเบิด');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  let r=null, threw=false;
  try { r=post(c,'ไม่ใช่ json เลย'); } catch(e){ threw=true; }
  chk('ไม่ throw', !threw);
  chk('ตอบ ok=false', r && r.ok===false, JSON.stringify(r));
  chk('ไม่เขียนไฟล์', c._w.length===0); }

console.log('10) ยังไม่มีไฟล์เดิม → สร้างใหม่ได้ (ต้องมีคีย์เหมือนกัน)');
{ const c=ctxFor({props:{DASH_KEY:KEY}, noFile:true});
  const r=post(c,{k:KEY,data:[{period:'ก'}]});
  chk('ok=true', r.ok===true, JSON.stringify(r));
  chk('สร้างไฟล์ 1 ครั้ง', c._w.length===1);
  const c2=ctxFor({noFile:true});
  post(c2,{data:[{period:'ก'}]});
  chk('ไม่มีคีย์ → ไม่สร้างไฟล์', c2._w.length===0); }

console.log('11) makeDashKey: ตั้ง property + เตือนเรื่องความลับ');
{ logs=[]; const c=ctxFor({});
  c.makeDashKey();
  chk('ตั้ง DASH_KEY แล้ว', (c._p.DASH_KEY||'').length===32, c._p.DASH_KEY);
  chk('บอกวิธีเอาไปใส่หน้า dashboard', logs.join().indexOf('☁️')>=0);
  chk('เตือนห้ามวางลงกล่องกลาง/รีโป/แชท', logs.join().indexOf('ห้ามวางคีย์นี้ลงไฟล์ในกล่องกลาง')>=0, logs.join(' | ')); }

console.log('12) showDashKeyStatus: ห้ามโชว์คีย์เต็ม');
{ logs=[]; const c=ctxFor({props:{DASH_KEY:KEY}});
  c.showDashKeyStatus();
  const out=logs.join(' ');
  chk('ไม่โชว์คีย์เต็ม', out.indexOf(KEY)<0, out);
  chk('โชว์ความยาว', out.indexOf('32')>=0, out);
  logs=[]; const c2=ctxFor({});
  c2.showDashKeyStatus();
  chk('ยังไม่ตั้ง → บอกว่าบันทึกมือจะถูกปฏิเสธ', logs.join().indexOf('ถูกปฏิเสธ')>=0);
  chk('และย้ำว่าการนำเข้าอัตโนมัติไม่กระทบ', logs.join().indexOf('ไม่กระทบ')>=0, logs.join(' | ')); }

console.log('13) ทิศกลับ: ด่านนี้ต้องไม่แตะเส้นทางอ่าน (doGet) และเส้นทางนำเข้า');
{ const c=ctxFor({props:{DASH_KEY:KEY}});
  // writeMonth_/writeMonths_ เขียน Drive ตรงๆ ไม่ผ่าน doPost — ยืนยันจากซอร์สว่าไม่มีการเช็คคีย์ในนั้น
  const body=src.slice(src.indexOf('function writeMonths_'), src.indexOf('function writeMonths_')+900);
  chk('writeMonths_ ไม่มีด่านคีย์ (นำเข้าอัตโนมัติไม่กระทบ)', body.indexOf('DASH_KEY')<0);
  const g=src.slice(src.indexOf('function doGet'), src.indexOf('var DASH_KEY_PROP'));
  chk('doGet ไม่มีด่านคีย์ (บอทยังอ่าน posday ได้)', g.indexOf('DASH_KEY')<0); }

console.log(fails?('\n🔴 ไม่ผ่าน '+fails+' เช็ค'):'\n🟢 ผ่านทุกเคส');
process.exit(fails?1:0);
