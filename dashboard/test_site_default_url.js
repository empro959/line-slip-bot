// วิธีรัน:  node dashboard/test_site_default_url.js   (จากรากรีโป)
// ค่าตั้งต้นของเว็บ (window.SAISANG_SYNC_URL) → SYNC_URL — ดึงโค้ดจริงจากไฟล์ HTML มารัน
// ⚠️ พลาดตรงนี้ = หน้าแดชบอร์ดเปิดมาว่างเปล่า หรือยิง fetch ไปที่ข้อความ placeholder
const fs=require('fs'), vm=require('vm');
let fails=0;
const chk=(n,c,x)=>{ if(c) console.log('  ✅ '+n); else {fails++; console.log('  ❌ '+n+(x?'  → '+x:''));} };

// ── ตัดเฉพาะบล็อกหาค่าตั้งต้นออกมาจาก HTML ตัวจริง (ไม่ก๊อปโค้ดมาไว้ในเทส) ──
const html=fs.readFileSync('dashboard/saisang_dashboard.html','utf8');
const a=html.indexOf('const SYNC_URL_DEFAULT=');
const b=html.indexOf('let DASH_KEY=');
if(a<0||b<0||b<a) { console.log('❌ หาบล็อก SYNC_URL_DEFAULT ในไฟล์ HTML ไม่เจอ — เทสนี้ไม่ได้ตรวจอะไรเลย'); process.exit(1); }
const BLOCK=html.slice(a,b);

// config.js ตัวจริงด้วย — จะได้รู้ว่ามันไม่เขียนทับค่าที่ snippet ตั้งไว้
const CONFIG=fs.readFileSync('dashboard/config.js','utf8');

/** รันบล็อกจริง แล้วคืน SYNC_URL ที่ได้
 *  siteUrl: ค่าที่ snippet ตั้งไว้ (undefined = ไม่ได้ตั้ง) · stored: ค่าใน localStorage
 *  withConfig: โหลด config.js ของจริงทับลงไปก่อน (เหมือนบนเว็บจริง) */
function run({siteUrl, stored, withConfig}){
  const win={};
  if(siteUrl!==undefined) win.SAISANG_SYNC_URL=siteUrl;
  const c={ window:win, localStorage:{ getItem:k=>(k==='saisang_sync_url'?(stored??null):null) } };
  c.window.localStorage=c.localStorage;
  vm.createContext(c);
  if(withConfig) vm.runInContext(CONFIG, c);     // ไฟล์จริง
  // 🪤 `let SYNC_URL` ใน vm ไม่โผล่เป็น c.SYNC_URL (lexical binding ไม่ใช่ global)
  //    ต้องต่อท้ายด้วยชื่อตัวแปร แล้วอ่านค่าที่ runInContext คืนมา
  return vm.runInContext(BLOCK+'\n;SYNC_URL;', c);   // บล็อกจริงจาก HTML
}
const REAL='https://script.google.com/macros/s/AKfycbxTESTTESTTEST/exec';

console.log('1) ตั้งค่าเว็บไว้ + เครื่องยังไม่เคยตั้ง → ใช้ค่าของเว็บเลย (ไม่ต้องกรอก)');
chk('ได้ URL ของเว็บ', run({siteUrl:REAL, stored:null})===REAL, run({siteUrl:REAL, stored:null}));

console.log('2) ไม่ได้ตั้งอะไรเลย → ว่าง (ต้องไม่ยิง fetch ไปที่ข้อความ placeholder)');
{ const r=run({siteUrl:undefined, stored:null});
  chk('SYNC_URL เป็นค่าว่าง', r==='', JSON.stringify(r));
  chk('ไม่ใช่ข้อความ <<...>>', !/^</.test(String(r)), JSON.stringify(r)); }

console.log('3) เครื่องตั้งค่าเองไว้ → ค่าของเครื่องชนะค่าของเว็บ');
{ const mine='https://script.google.com/macros/s/MINE/exec';
  chk('ได้ค่าของเครื่อง', run({siteUrl:REAL, stored:mine})===mine); }

console.log('4) เครื่องมี placeholder เก่าค้างใน localStorage → ถอยไปใช้ค่าของเว็บ');
chk('ได้ค่าของเว็บ', run({siteUrl:REAL, stored:'<<ใส่ URL ตรงนี้>>'})===REAL);

console.log('5) config.js ในรีโป (ค่าว่าง) → ต้องไม่ทำให้เว็บคิดว่าตั้งค่าแล้ว');
{ const r=run({siteUrl:undefined, stored:null, withConfig:true});
  chk('ยังว่างอยู่', r===''); }

console.log('6) 🔴 config.js ต้องไม่เขียนทับค่าที่ Netlify Snippet Injection ตั้งไว้');
{ const r=run({siteUrl:REAL, stored:null, withConfig:true});
  chk('ค่าจาก snippet ยังอยู่', r===REAL, JSON.stringify(r)); }

console.log('7) 🔴 config.js ในรีโปสาธารณะต้องไม่มีค่าจริง/คีย์ติดไปด้วย');
{ chk('ไม่มีลิงก์จริงฝังอยู่', CONFIG.indexOf('script.google.com')<0);
  chk('ไม่มี DASH_KEY ที่ตั้งค่าไว้', !/DASH_KEY\s*=\s*['"][^'"]/.test(CONFIG));
  chk('ค่าเริ่มต้นเป็นสตริงว่าง', /SAISANG_SYNC_URL\s*=\s*window\.SAISANG_SYNC_URL\s*\|\|\s*''/.test(CONFIG)); }

console.log('8) 🔴 หน้า HTML ต้องโหลด config.js จริง ไม่งั้นตั้งค่าแล้วก็ไม่มีผล');
chk('มี <script src="config.js">', /<script src="config\.js"><\/script>/.test(html));

console.log('9) 🔴 เลขรุ่นต้องมีจริงและตรงกัน — เคยลากไฟล์เก่าทับแล้วไม่มีใครรู้');
{ const m=html.match(/const BUILD='([^']+)';/);
  chk('มีค่า BUILD ในไฟล์', !!m, 'ไม่เจอ');
  chk('มีช่องโชว์บนหน้าจอ (#build-tag)', /id="build-tag"/.test(html));
  chk('มีโค้ดเขียนเลขรุ่นลงช่องนั้น', /getElementById\('build-tag'\)/.test(html));
  chk('กล่อง ☁️ โชว์เลขรุ่นด้วย', /_ver\s*\+\s*_cfg/.test(html)); }

console.log('10) 🔴 ช่องในกล่อง ☁️ ต้องถอยไปใช้ลิงก์ของเว็บ — ไม่ปล่อยว่าง');
// 8 พ.ย. 69: เครื่องที่ปิดซิงค์ไว้เปิดกล่องมาเจอช่องว่าง เจ้าของกด ตกลง เพื่อปิดกล่อง
// กลายเป็นสั่ง 'ปิดการซิงค์' ซ้ำอีกรอบ (เว้นว่าง + OK = ปิด)
{ chk('ช่องมี fallback ไป window.SAISANG_SYNC_URL',
      html.indexOf("SYNC_URL || (typeof window!=='undefined' && window.SAISANG_SYNC_URL) || ''") >= 0);
  chk('บอกด้วยว่ากดตกลงแล้วจะเกิดอะไร', html.indexOf('กด ตกลง = เปิดซิงค์คืน')>=0); }

console.log(fails?('\n❌ ตก '+fails+' ข้อ'):'\n✅ ผ่านหมด');
process.exit(fails?1:0);
