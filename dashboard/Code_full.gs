// ==========================================================================
//  ไส้ย่างซอย๔ — Apps Script (ตัวเต็ม)
//  รวม: (1) Drive sync ให้ dashboard  +  (2) ดึงข้อมูล Easy POS อัตโนมัติ
//  วิธีใช้: เลือกโค้ดทั้งหมดในไฟล์นี้ -> ลบ -> วางทับ -> Save
//  ต้องเปิด Advanced Drive Service (Drive API v2) ที่ "บริการ" ด้วย
//
//  ── แก้ไข 2026-08-17 (หลังย้ายเซิร์ฟเวอร์บอทจาก E&M มาเป็นของร้าน) ──
//  ปัญหา: สรุปยอดไม่เข้า LINE หลายวัน แต่อีเมลเข้าปกติ
//  เหตุ:  pushOwner_ ใช้ host จาก SLIP_API_URL ซึ่งยังชี้บอทตัวเก่า (65gt)
//         และ error ถูกกลืน 2 ชั้น (catch ในตัวเอง + catch(e){} ตอนเรียก)
//         → LINE พังเงียบ ไม่ขึ้นเป็น failed execution ให้เห็นเลย
//  แก้:   1) เพิ่ม showSlipUrl() / fixSlipUrl() — ดู/เปลี่ยน host โดยคง token เดิม
//         2) pushOwner_ คืนข้อความ error ('' = สำเร็จ) + บอก HTTP code
//         3) sendDailyAlert/sendWeeklyAlert ฟ้องใน "หัวเรื่องอีเมล" ถ้า LINE ส่งไม่ออก
//         4) testPushOwner ทดสอบเฉพาะ LINE (ไม่ยิงอีเมลซ้ำ) + โชว์ผลชัดเจน
// ==========================================================================

const FOLDER_NAME = 'สรุปรายรับรายจ่าย';
const FILE_NAME   = 'saisang_data.json';

// host ของบอทตัวใหม่ (ร้าน) — ใช้โดย fixSlipUrl()/setSlipUrl()
const BOT_HOST = 'https://line-slip-bot-1-iq8e.onrender.com';

function getFolder_() {
  const it = DriveApp.getFoldersByName(FOLDER_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
}
function getFile_() {
  const it = getFolder_().getFilesByName(FILE_NAME);
  return it.hasNext() ? it.next() : null;
}
function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ---- Web App: ให้ dashboard อ่าน/เขียนข้อมูล ----
function doGet(e) {
  const action = (e && e.parameter && e.parameter.action) || 'load';
  const file = getFile_();
  if (action === 'meta') {
    return json_({ ok: true, exists: !!file, updatedAt: file ? file.getLastUpdated().getTime() : 0 });
  }
  if (!file) return json_({ ok: true, exists: false, data: null, updatedAt: 0 });
  // ดึงยอด 'เฉพาะวันเดียว' (สด/โอน/บัตร/ยอดขาย) — payload เล็กมาก ไว้ให้บอทเทียบยอด (ไม่ต้องโหลดทั้งปีที่ใหญ่/ช้า/พัง)
  if (action === 'posday') {
    var qd = (e && e.parameter && e.parameter.date) || '';
    var data = JSON.parse(file.getBlob().getDataAsString('UTF-8') || 'null');
    var months = Array.isArray(data) ? data : [];
    for (var mi = 0; mi < months.length; mi++) {
      var pds = months[mi].payment_days || [];
      for (var di = 0; di < pds.length; di++) {
        if (pds[di].date === qd) {
          var p = pds[di].payments || {};
          var sales = 0, dts = months[mi].daily_totals || [];
          for (var si = 0; si < dts.length; si++) { if (dts[si].date === qd) { sales = dts[si].sales || 0; break; } }
          return json_({ ok: true, exists: true, day: { date: qd, cash: p['เงินสด'] || 0, transfer: p['เงินโอน'] || 0, card: p['บัตรเครดิต'] || 0, sales: sales } });
        }
      }
    }
    return json_({ ok: true, exists: true, day: null });
  }
  const content = file.getBlob().getDataAsString('UTF-8');
  return json_({ ok: true, exists: true, data: JSON.parse(content || 'null'), updatedAt: file.getLastUpdated().getTime() });
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const payload = JSON.stringify(body.data);
    let file = getFile_();
    if (file) file.setContent(payload);
    else file = getFolder_().createFile(FILE_NAME, payload, 'application/json');
    return json_({ ok: true, updatedAt: file.getLastUpdated().getTime() });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

// ==========================================================================
//  POS AUTO-IMPORT : Easy POS PDF (เมล) -> Drive
// ==========================================================================
var THAI_MONTHS = ['มกราคม','กุมภาพันธ์','มีนาคม','เมษายน','พฤษภาคม','มิถุนายน',
                   'กรกฎาคม','สิงหาคม','กันยายน','ตุลาคม','พฤศจิกายน','ธันวาคม'];

function num_(s){ s=String(s).replace(/,/g,'').trim(); if(s==='.00'||s==='.'||s==='')return 0; var v=parseFloat(s); return isNaN(v)?null:v; }

// แปลง PDF -> ข้อความ ผ่าน OCR ของ Google
function pdfToText_(blob){ return _ocrTry_(blob, 2); }  // ลองใหม่ได้ถึง 2 ครั้งถ้าชน rate limit
function _ocrTry_(blob, retriesLeft){
  try{
    var tmp=Drive.Files.insert({title:'TMP_'+Utilities.getUuid(),mimeType:'application/pdf'},blob,{ocr:true,ocrLanguage:'th'});
    var txt=DocumentApp.openById(tmp.id).getBody().getText();
    Drive.Files.remove(tmp.id);
    return txt;
  }catch(e){
    if(retriesLeft>0 && /rate limit/i.test(String(e))){
      Logger.log('   …ชน rate limit พัก 60 วิ แล้วลองใหม่ (เหลือ '+retriesLeft+' ครั้ง)');
      Utilities.sleep(60000);                 // พัก 60 วิ ให้ rate limit คลาย
      return _ocrTry_(blob, retriesLeft-1);
    }
    throw e;
  }
}

// หา PDF ล่าสุดจากเมล ตามคำในชื่อไฟล์
function findLatestPdf_(keyword){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:35d',0,40);
  var best=null,bestT=0,bestName='';
  for(var t=0;t<threads.length;t++){var msgs=threads[t].getMessages();
    for(var m=0;m<msgs.length;m++){var dt=msgs[m].getDate().getTime(),atts=msgs[m].getAttachments();
      for(var a=0;a<atts.length;a++){var n=atts[a].getName();
        if(new RegExp(keyword,'i').test(n)&&/\.pdf$/i.test(n)&&dt>bestT){best=atts[a];bestT=dt;bestName=n;}}}}
  return best?{blob:best.copyBlob(),name:bestName,time:bestT}:null;
}

// งวด เช่น "กรกฎาคม 2569" (แปลงปี ค.ศ.->พ.ศ.)
function periodFrom_(text){
  var m=text.match(new RegExp('('+THAI_MONTHS.join('|')+')\\s+(\\d{4})'));
  return m?(m[1]+' '+(parseInt(m[2],10)+543)):null;
}

// ---- แกะ SaleReport -> {period, cats:[{category,amount,exc,vat}]} ----
function parseSale_(text){
  var period=periodFrom_(text);
  // ตัดบรรทัดหัว/ท้ายกระดาษทิ้ง (กันเลขปลอม เช่น 50000, ปี ค.ศ. มาขวาง)
  var raw=text.split(/[\n\r]+/), lines=[];
  var junk=/บริษัท|^เลขที่|รายงานสรุป|^ตั้งแต่|^หมวดสินค้า|\d{4}\/\d{2}\/\d{2}/;
  for(var i=0;i<raw.length;i++){
    var ln=raw[i].trim();
    if(ln.indexOf('ยอดรวม(รวมภาษี)')>=0) ln=ln.replace(/^.*ยอดรวม\(รวมภาษี\)\s*/,'').trim(); // ตัดหัวตาราง เหลือส่วนที่เกาะท้าย
    if(!ln||junk.test(ln)) continue;
    lines.push(ln);
  }
  var toks=lines.join(' ').split(/\s+/).filter(function(x){return x!=='';});
  var isNum=function(s){return /^-?[\d,]+(\.\d+)?$/.test(s)||s==='.00';};
  var catHdr=/^\d{1,2}\.[^\d]/;
  var KNOWN=['อาหารญี่ปุ่น','ร้านขนมเส้น','ร้านก๋วยเตี๋ยว']; // หมวดที่ไม่มีเลขนำหน้า
  var headers=[]; for(var j=0;j<toks.length;j++) if(catHdr.test(toks[j])||KNOWN.indexOf(toks[j])>=0) headers.push(j);
  var sumIdx=-1;
  for(var q=0;q<toks.length;q++) if(toks[q].indexOf('สรุปยอดขาย')>=0){sumIdx=q;break;}
  var bset=headers.slice(); if(sumIdx>=0) bset.push(sumIdx);
  bset.sort(function(a,b){return a-b;});
  var cats=[];
  for(var k=0;k<headers.length;k++){
    var start=headers[k], end=toks.length;
    for(var b=0;b<bset.length;b++) if(bset[b]>start){end=bset[b];break;}
    var p=end-1;
    while(p>start&&!isNum(toks[p])) p--;                 // ข้ามเศษหัว/ท้ายกระดาษ
    if(end===sumIdx){                                    // หมวดสุดท้าย: ข้ามแถว "รวมทั้งหมด" (grand total)
      var g=0; while(p>start&&g<6&&isNum(toks[p])){p--;g++;}
      while(p>start&&!isNum(toks[p])) p--;
    }
    var nums=[];
    while(p>start&&nums.length<6&&isNum(toks[p])){nums.push(num_(toks[p]));p--;}
    if(nums.length===6){nums.reverse(); // qty,total,disc,exc,vat,inc
      cats.push({category:toks[start],amount:nums[5],exc:nums[3],vat:nums[4]});}
  }
  return {period:period,cats:cats};
}

// ---- แกะ SaleReport ระดับ "รายเมนู" -> [{category,name,qty,amount(inc)}] ----
function parseSaleItems_(text){
  var raw=text.split(/[\n\r]+/), lines=[];
  var junk=/บริษัท|^เลขที่|รายงานสรุป|^ตั้งแต่|^หมวดสินค้า|\d{4}\/\d{2}\/\d{2}/;
  for(var i=0;i<raw.length;i++){var ln=raw[i].trim();
    if(ln.indexOf('ยอดรวม(รวมภาษี)')>=0) ln=ln.replace(/^.*ยอดรวม\(รวมภาษี\)\s*/,'').trim();
    if(!ln||junk.test(ln)) continue; lines.push(ln);}
  var toks=lines.join(' ').split(/\s+/).filter(function(x){return x!=='';});
  var isNum=function(s){return /^-?[\d,]+(\.\d+)?$/.test(s)||s==='.00';};
  var isCode=function(s){return /^[A-Za-z]{2,}-?\d/.test(s);};
  var catHdr=/^\d{1,2}\.[^\d]/;
  var KNOWN=['อาหารญี่ปุ่น','ร้านขนมเส้น','ร้านก๋วยเตี๋ยว'];
  var headers=[]; for(var j=0;j<toks.length;j++) if(catHdr.test(toks[j])||KNOWN.indexOf(toks[j])>=0) headers.push(j);
  var sumIdx=-1; for(var q=0;q<toks.length;q++) if(toks[q].indexOf('สรุปยอดขาย')>=0){sumIdx=q;break;}
  var bset=headers.slice(); if(sumIdx>=0) bset.push(sumIdx); bset.sort(function(a,b){return a-b;});
  var items=[];
  for(var k=0;k<headers.length;k++){
    var start=headers[k], cat=toks[start], end=toks.length;
    for(var b=0;b<bset.length;b++) if(bset[b]>start){end=bset[b];break;}
    var p=end-1;
    while(p>start&&!isNum(toks[p])) p--;
    if(end===sumIdx){ var g=0; while(p>start&&g<6&&isNum(toks[p])){p--;g++;} while(p>start&&!isNum(toks[p]))p--; }
    var cnt=0; while(p>start&&cnt<6&&isNum(toks[p])){p--;cnt++;}
    var itemEnd=p+1;                                   // ตัด subtotal/grand total ออก
    var codes=[]; for(var i2=start+1;i2<itemEnd;i2++) if(isCode(toks[i2])) codes.push(i2);
    for(var ci=0;ci<codes.length;ci++){
      var cp=codes[ci], ce=(ci+1<codes.length)?codes[ci+1]:itemEnd;
      var span=toks.slice(cp+1,ce), nums=[];
      for(var x=0;x<span.length;x++) if(isNum(span[x])) nums.push({i:x,v:num_(span[x])});
      if(nums.length>=6){
        var inc=nums[nums.length-1].v, qn=nums[nums.length-6];
        var name=span.slice(0,qn.i).join(' ').trim();
        // กันเมนูเพี้ยน: ตัดชื่อที่จริงๆ เป็น "ข้อความหัว/สรุปท้ายรายงาน" (ไม่ใช่ชื่อเมนู) หรือยาวผิดปกติ
        //   เกิดตอนโค้ด+ตัวเลขของส่วนสรุปหลุดมาปนช่วงท้ายหมวดสุดท้าย → ได้ 'เมนู' ยอดมั่วก้อนโต
        var badNm=/Sales Report|Item Code|Item Description|Category|Total Order|Total Customer|Real Sale|Take Home|Point Redeem|Sale by|Count |Transaction|\d{1,2}:\d{2}|รายงานสรุป|ตั้งแต่|หมวดสินค้า/;
        if(name && name.length<=48 && !badNm.test(name))
          items.push({category:cat,name:name,qty:qn.v,amount:inc});
      }
    }
  }
  return items;
}

// ---- แกะส่วนสรุปท้าย SaleReport -> {cancelled,deleted,returns,discount} (ยกเลิก/ลบบิล/คืน/ส่วนลด) ----
function parseVoids_(text){
  var i=text.indexOf('มูลค่ารายการที่สั่ง'); if(i<0) return {};
  var seg=text.slice(i);
  var e=seg.indexOf('ห่อกลับบ้าน'); if(e<0) e=seg.indexOf('มูลค่าสินค้าแยก'); if(e>0) seg=seg.slice(0,e);
  var toks=seg.replace(/\s+/g,' ').split(' ').filter(function(x){return x!=='';});
  var isNum=function(s){return /^-?[\d,]+(\.\d+)?$/.test(s);};
  function val(label){ for(var k=0;k<toks.length;k++){ if(toks[k]===label){ var j=k+1; while(j<toks.length&&!isNum(toks[j]))j++; if(j<toks.length) return num_(toks[j]); } } return 0; }
  return { cancelled:val('มูลค่ารายการที่ยกเลิก'), deleted:val('มูลค่าของการลบการขาย'),
           returns:val('มูลค่าของการคืนสินค้า'), discount:val('ส่วนลด') };
}

// ---- แกะ PayoutReport -> {period, records:[...], cats:[...]} ----
function parsePayout_(text){
  var lines=text.split(/[\n\r]+/), seen={}, records=[];
  var re=/^(.*\([^)]*\))\s+([\d,]+\.\d{2})\s*$/;
  for(var i=0;i<lines.length;i++){
    var ln=lines[i].trim(), m=ln.match(re); if(!m) continue;
    var name=m[1].trim(), amt=num_(m[2]);
    if(!/[ก-๙]/.test(name)||amt==null) continue;
    if(/ยอดรวม|ยอดขาย|รายงาน|บริษัท/.test(name)) continue;
    if(seen[name]) continue; seen[name]=true;
    var cat=(name.match(/^([^(（【]+)/)||['',name])[1].trim();
    records.push({description:name,category:cat,amount:amt});
  }
  var map={}; records.forEach(function(r){map[r.category]=(map[r.category]||0)+r.amount;});
  var cats=Object.keys(map).map(function(c){return {category:c,amount:Math.round(map[c]*100)/100};})
                           .sort(function(a,b){return b.amount-a.amount;});
  return {period:periodFrom_(text),records:records,cats:cats};
}

// รวมเป็น object เดือน (โครงเดียวกับ dashboard)
function buildMonth_(sale,payout){
  var totE=payout.cats.reduce(function(a,c){return a+c.amount;},0);
  var totS=sale.cats.reduce(function(a,c){return a+c.amount;},0);
  return {period:sale.period||payout.period,
    total_sales:Math.round(totS*100)/100, total_expenses:Math.round(totE*100)/100,
    net_profit:Math.round((totS-totE)*100)/100,
    expense_categories:payout.cats, sales_categories:sale.cats, expense_records:payout.records};
}

// เขียน/อัปเดตเดือนลง saisang_data.json (แทนที่เดือนเดิมถ้ามี)
function writeMonth_(month){
  var f=getFile_(), arr=[];
  if(f){ try{arr=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){arr=[];} if(!Array.isArray(arr))arr=[]; }
  arr=arr.filter(function(m){return m.period!==month.period;});
  arr.push(month);
  if(f) f.setContent(JSON.stringify(arr));
  else getFolder_().createFile(FILE_NAME,JSON.stringify(arr),'application/json');
  return arr.length;
}

// ===== ทดสอบ: แกะแล้วโชว์ผล (ยังไม่เขียน Drive) — รันอันนี้ก่อน =====
function testParsePos(){
  var s=findLatestPdf_('SaleReport'), p=findLatestPdf_('PayoutReport');
  if(!s||!p){Logger.log('❌ ไม่พบไฟล์: '+(s?'':'SaleReport ')+(p?'':'PayoutReport'));return;}
  Logger.log('ไฟล์: '+s.name+' | '+p.name);
  var mo=buildMonth_(parseSale_(pdfToText_(s.blob)), parsePayout_(pdfToText_(p.blob)));
  Logger.log('งวด: '+mo.period);
  Logger.log('ยอดขายรวม: '+mo.total_sales.toLocaleString()+'  |  ค่าใช้จ่ายรวม: '+mo.total_expenses.toLocaleString()+'  |  กำไร: '+mo.net_profit.toLocaleString());
  Logger.log('— หมวดยอดขาย ('+mo.sales_categories.length+') —');
  mo.sales_categories.forEach(function(c){Logger.log('   '+c.category+' = '+c.amount.toLocaleString());});
  Logger.log('— หมวดค่าใช้จ่าย ('+mo.expense_categories.length+', รายการ '+mo.expense_records.length+') —');
  mo.expense_categories.forEach(function(c){Logger.log('   '+c.category+' = '+c.amount.toLocaleString());});
}

// ==========================================================================
//  ระบบสะสมยอดรายวัน -> รวมเป็นเดือน  (POS ส่งรายงานรายวัน)
// ==========================================================================
var DAILY_FILE='pos_daily.json';
function r2_(n){return Math.round(n*100)/100;}

// ---- วิธีชำระเงิน (BalanceCashDrawer) + เทียบสลิปจากบอท ----
// ลิงก์สลิปเก็บใน Script Properties (คีย์ 'SLIP_API_URL') — ไม่ฝัง token ในโค้ด
// ตั้งครั้งเดียวด้วยฟังก์ชัน setSlipUrl() ด้านล่าง (หรือ Project Settings > Script Properties)
var PAY_MAP={'Prompay':'เงินโอน','เงินสด':'เงินสด','บัตรเครดิต':'บัตรเครดิต','ค้างชำระ':'ค้างชำระ','อื่นๆ':'อื่นๆ'};

// ▼▼ ตั้งลิงก์สลิปครั้งเดียว: ใส่ URL+token ในบรรทัดล่าง รัน setSlipUrl 1 ครั้ง แล้วลบ token ออกจากบรรทัดนี้ได้ ▼▼
function setSlipUrl(){
  var url=BOT_HOST+'/api/slip_daily?token=ใส่รหัสที่นี่'; // <<< แก้ตรงนี้ (token = SLIP_API_TOKEN บน Render)
  PropertiesService.getScriptProperties().setProperty('SLIP_API_URL', url);
  Logger.log('✅ บันทึกลิงก์สลิปลง Script Properties แล้ว (รัน rebuildNow ต่อได้เลย)');
}

// ▼▼ [ใหม่ 17/08/26] ดู SLIP_API_URL ที่ตั้งไว้ — ปิด token ไม่ให้โชว์ค่าจริง ▼▼
function showSlipUrl(){
  var u=PropertiesService.getScriptProperties().getProperty('SLIP_API_URL');
  if(!u){ Logger.log('❌ ยังไม่ตั้ง SLIP_API_URL — รัน setSlipUrl() ก่อน'); return; }
  Logger.log('SLIP_API_URL = '+u.replace(/token=.*/,'token=***'));
  var host=(u.match(/^https:\/\/[^\/]+/)||[''])[0];
  Logger.log(host===BOT_HOST ? '✅ host ถูกต้อง (บอทตัวใหม่ของร้าน)'
                             : '🔴 host ยังเป็นตัวเก่า! → รัน fixSlipUrl() เพื่อเปลี่ยนเป็น '+BOT_HOST);
}

// ▼▼ [ใหม่ 17/08/26] เปลี่ยนแค่ host เป็นบอทตัวใหม่ — คง token เดิมไว้ ▼▼
function fixSlipUrl(){
  var p=PropertiesService.getScriptProperties();
  var u=p.getProperty('SLIP_API_URL')||'';
  if(!u){ Logger.log('❌ ยังไม่ตั้ง SLIP_API_URL — ใช้ setSlipUrl() ก่อน'); return; }
  var n=u.replace(/^https:\/\/[^\/]+/, BOT_HOST);
  p.setProperty('SLIP_API_URL', n);
  Logger.log('✅ เปลี่ยน host แล้ว → '+n.replace(/token=.*/,'token=***'));
  Logger.log('👉 ต่อไปรัน testPushOwner() เพื่อเช็คว่าเข้า LINE ได้จริง');
}

// แกะ BalanceCashDrawer เฉพาะช่วง "Net Sale" -> {เงินโอน,เงินสด,บัตรเครดิต,ค้างชำระ,อื่นๆ}
function parseBalance_(text){
  var i=text.indexOf('Net Sale'); if(i<0) return {};
  var seg=text.slice(i); var e=seg.indexOf('Total'); if(e>=0) seg=seg.slice(0,e);
  var toks=seg.replace(/[\n\r]+/g,' ').split(/\s+/).filter(function(x){return x!=='';});
  var isNum=function(s){return /^-?[\d,]+(\.\d+)?$/.test(s);};
  var out={};
  for(var k=0;k<toks.length;k++){
    if(PAY_MAP.hasOwnProperty(toks[k])){
      var j=k+1; while(j<toks.length && !isNum(toks[j])) j++;  // ข้ามคำ เช่น "ทชท"
      if(j<toks.length) out[PAY_MAP[toks[k]]]=num_(toks[j]);
    }
  }
  return out;
}

// แกะยอดขายแยกโซนจาก BalanceCashDrawer -> {โซน: ยอดรวม}
function parseBalanceZones_(text){
  text=text.split('Net Sale')[0];  // เอาเฉพาะส่วนแยกโซน (ก่อนสรุป)
  var re=/รวมของโซน\s+(.+?)\s+([\d,]+\.\d{2})/g, out={}, m;
  while((m=re.exec(text))!==null){
    var z=m[1].trim(); out[z]=(out[z]||0)+num_(m[2]);
  }
  return out;
}

// ดึงยอดสลิปรายวันจากบอท -> {'2026-07-01':{amount,count}}
function fetchSlipMap_(){
  var url=PropertiesService.getScriptProperties().getProperty('SLIP_API_URL');
  if(!url) return {};  // ยังไม่ได้ตั้งลิงก์ (รัน setSlipUrl ก่อน) → ข้ามการเทียบสลิป
  try{
    var r=UrlFetchApp.fetch(url,{muteHttpExceptions:true});
    var d=JSON.parse(r.getContentText());
    if(!d||!d.ok){ Logger.log('บอทตอบไม่ ok: '+r.getContentText().substring(0,120)); return {}; }
    return d.daily||{};
  }catch(e){ Logger.log('ดึงสลิปไม่ได้: '+e); return {}; }
}

// "กรกฎาคม 2569" -> "2026-07" (ไว้จับคู่วันสลิป)
function ymPrefix_(period){
  var m=period.match(new RegExp('('+THAI_MONTHS.join('|')+')\\s+(\\d{4})'));
  if(!m) return null;
  return (parseInt(m[2],10)-543)+'-'+('0'+(THAI_MONTHS.indexOf(m[1])+1)).slice(-2);
}

// ดึงวันทำการจาก "ตั้งแต่ DD <เดือนไทย> YYYY" -> {key:'2026-07-01', period:'กรกฎาคม 2569'}
function dailyDate_(text){
  var m=text.match(new RegExp('(?:ตั้งแต่|From)[:\\s]*([0-9]{1,2})\\s+('+THAI_MONTHS.join('|')+')\\s+([0-9]{4})'));
  if(!m) return null;
  var mi=THAI_MONTHS.indexOf(m[2])+1;
  var y=parseInt(m[3],10); if(y>=2500) y-=543;   // normalize พ.ศ.→ค.ศ. (บางใบพิมพ์/OCR เป็น 2569 → กัน key เพี้ยนโดนข้าม)
  return {key:y+'-'+('0'+mi).slice(-2)+'-'+('0'+m[1]).slice(-2), period:m[2]+' '+(y+543)};
}

function loadDaily_(){
  var folder=getFolder_(), it=folder.getFilesByName(DAILY_FILE);
  if(!it.hasNext()) return [];
  try{var a=JSON.parse(it.next().getBlob().getDataAsString('UTF-8'));return Array.isArray(a)?a:[];}catch(e){return [];}
}
function saveDaily_(arr){
  var folder=getFolder_(), it=folder.getFilesByName(DAILY_FILE);
  if(it.hasNext()) it.next().setContent(JSON.stringify(arr));
  else folder.createFile(DAILY_FILE,JSON.stringify(arr),'application/json');
}

// รวมข้อมูลรายวันทั้งหมด -> array ของเดือน (โครงเดียวกับ dashboard)
function rebuildMonths_(daily, slipMap){
  slipMap=slipMap||{};
  var byP={};
  daily.forEach(function(d){
    if(!byP[d.period]) byP[d.period]={sales:{},records:[],pay:{},days:[],menu:{},zone:{},credit:{},voids:{cancelled:0,deleted:0,returns:0,discount:0}};
    (d.sales||[]).forEach(function(c){
      var s=byP[d.period].sales[c.category]||{amount:0,exc:0,vat:0};
      s.amount+=c.amount||0; s.exc+=c.exc||0; s.vat+=c.vat||0;
      byP[d.period].sales[c.category]=s;
    });
    (d.expenses||[]).forEach(function(r){byP[d.period].records.push(r);});
    var pm=d.payments||{};
    Object.keys(pm).forEach(function(k){ byP[d.period].pay[k]=(byP[d.period].pay[k]||0)+(pm[k]||0); });
    var dS_=(d.sales||[]).reduce(function(a,c){return a+(c.amount||0);},0);      // ยอดขายรวมวันนั้น (Inc.VAT)
    var dE_=(d.expenses||[]).reduce(function(a,c){return a+(c.amount||0);},0);   // ค่าใช้จ่ายรวมวันนั้น
    var dFood_=(d.sales||[]).filter(function(c){return !/เครื่องดื่ม|อาหารญี่ปุ่น/.test(c.category);}).reduce(function(a,c){return a+(c.amount||0);},0); // ยอดขายอาหาร (ไม่รวมญี่ปุ่น/เครื่องดื่ม)
    var dRaw_=(d.expenses||[]).filter(function(r){return /วัตถุดิบ/.test(r.category);}).reduce(function(a,r){return a+(r.amount||0);},0); // ต้นทุนวัตถุดิบวันนั้น
    var dBev_=(d.sales||[]).filter(function(c){return /เครื่องดื่ม/.test(c.category);}).reduce(function(a,c){return a+(c.amount||0);},0);   // ยอดขายเครื่องดื่มวันนั้น
    var dBevC_=(d.expenses||[]).filter(function(r){return /เครื่องดื่ม/.test(r.category);}).reduce(function(a,r){return a+(r.amount||0);},0); // ต้นทุนเครื่องดื่มวันนั้น
    byP[d.period].days.push({date:d.date,payments:pm,sales:dS_,exp:dE_,food:dFood_,raw:dRaw_,bev:dBev_,bevc:dBevC_,bills:d.bills||0});  // + อาหาร/วัตถุดิบ + เครื่องดื่ม/ต้นทุน + จำนวนบิล
    (d.menu||[]).forEach(function(it){ var e=byP[d.period].menu[it.name]||{category:it.category,qty:0,amount:0};
      e.qty+=it.qty||0; e.amount+=it.amount||0; byP[d.period].menu[it.name]=e; });
    var zn=d.zones||{}; Object.keys(zn).forEach(function(z){ byP[d.period].zone[z]=(byP[d.period].zone[z]||0)+(zn[z]||0); });
    var vd=d.voids||{}, vv=byP[d.period].voids; vv.cancelled+=vd.cancelled||0; vv.deleted+=vd.deleted||0; vv.returns+=vd.returns||0; vv.discount+=vd.discount||0;
    // ค้างชำระรายลูกค้า (จากรายงานแยกลูกค้า — ชื่อที่มีคำว่า 'ค้างชำระ') → รวมยอดต่อคนในเดือน
    (d.credit||[]).forEach(function(c){ var e=byP[d.period].credit[c.name]||{id:c.id||'',amount:0,days:[]};
      e.amount+=(c.amount||0); e.days.push({date:d.date,amount:c.amount||0}); byP[d.period].credit[c.name]=e; });
  });
  var months=[];
  Object.keys(byP).forEach(function(p){
    var b=byP[p];
    var sCats=Object.keys(b.sales).map(function(k){var s=b.sales[k];return {category:k,amount:r2_(s.amount),exc:r2_(s.exc),vat:r2_(s.vat)};})
                                  .sort(function(a,b){return b.amount-a.amount;});
    var eMap={}; b.records.forEach(function(r){eMap[r.category]=(eMap[r.category]||0)+r.amount;});
    var eCats=Object.keys(eMap).map(function(c){return {category:c,amount:r2_(eMap[c])};}).sort(function(a,b){return b.amount-a.amount;});
    var totS=sCats.reduce(function(a,c){return a+c.amount;},0), totE=eCats.reduce(function(a,c){return a+c.amount;},0);
    // วิธีชำระเงิน
    var payCats=Object.keys(b.pay).map(function(k){return {method:k,amount:r2_(b.pay[k])};}).sort(function(a,b){return b.amount-a.amount;});
    // เทียบสลิป: รวมยอดสลิปของเดือนนี้ (ตามเดือนปฏิทิน)
    var ym=ymPrefix_(p), botSlip=0, botCnt=0;
    if(ym) Object.keys(slipMap).forEach(function(dt){ if(dt.indexOf(ym)===0){ botSlip+=(slipMap[dt].amount||0); botCnt+=(slipMap[dt].count||0);} });
    var posTransfer=b.pay['เงินโอน']||0;
    // breakdown รายวัน (แยกวันในเดือน) + สลิปของวันนั้น
    var payDays=b.days.map(function(dy){
      var sv=slipMap[dy.date]||{}; var t=(dy.payments||{})['เงินโอน']||0;
      return {date:dy.date,payments:dy.payments||{},slip_amount:r2_(sv.amount||0),slip_count:sv.count||0,diff:r2_(t-(sv.amount||0))};
    }).sort(function(a,b){return a.date<b.date?1:-1;}); // วันล่าสุดก่อน
    // ยอดรับ/จ่าย/กำไร รายวัน (เรียงวันน้อย→มาก 1→31 สำหรับกราฟ)
    var dailyTotals=b.days.map(function(dy){
      return {date:dy.date,sales:r2_(dy.sales||0),expenses:r2_(dy.exp||0),profit:r2_((dy.sales||0)-(dy.exp||0)),food:r2_(dy.food||0),raw:r2_(dy.raw||0),bev:r2_(dy.bev||0),bevc:r2_(dy.bevc||0),bills:dy.bills||0};
    }).sort(function(a,b){return a.date<b.date?-1:1;});
    var menuItems=Object.keys(b.menu).map(function(nm){var e=b.menu[nm];return {name:nm,category:e.category,qty:r2_(e.qty),amount:r2_(e.amount)};})
                                     .sort(function(a,b){return b.amount-a.amount;});
    var zoneSales=Object.keys(b.zone).map(function(z){return {zone:z,amount:r2_(b.zone[z])};}).sort(function(a,b){return b.amount-a.amount;});
    var creditCust=Object.keys(b.credit).map(function(nm){var e=b.credit[nm];
      return {name:nm,id:e.id,amount:r2_(e.amount),days:e.days.sort(function(a,b){return a.date<b.date?-1:1;})};})
      .sort(function(a,b){return b.amount-a.amount;});
    months.push({period:p,total_sales:r2_(totS),total_expenses:r2_(totE),net_profit:r2_(totS-totE),
      expense_categories:eCats,sales_categories:sCats,expense_records:b.records,
      payment_methods:payCats,payment_days:payDays,daily_totals:dailyTotals,menu_items:menuItems,zone_sales:zoneSales,credit_customers:creditCust,
      voids:{cancelled:r2_(b.voids.cancelled),deleted:r2_(b.voids.deleted),returns:r2_(b.voids.returns),discount:r2_(b.voids.discount)},
      recon:{pos_transfer:r2_(posTransfer),bot_slip:r2_(botSlip),bot_count:botCnt,diff:r2_(posTransfer-botSlip)}});
  });
  return months;
}

// เขียนเดือนที่สร้างใหม่ลง saisang_data.json (แทนที่งวดที่ POS คุม คงงวดที่อัปมือไว้)
function writeMonths_(months){
  var f=getFile_(), arr=[];
  if(f){try{arr=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){arr=[];} if(!Array.isArray(arr))arr=[];}
  var per={}; months.forEach(function(m){per[m.period]=true;});
  arr=arr.filter(function(m){return !per[m.period];}).concat(months);
  if(f) f.setContent(JSON.stringify(arr)); else getFolder_().createFile(FILE_NAME,JSON.stringify(arr),'application/json');
  return arr.length;
}

// อ่าน "จำนวนบิล" จากรายงานแยกตามลูกค้า (บรรทัด "รวมทั้งหมด" คอลัมน์จำนวน)
function parseBills_(text){
  if(!text) return 0;
  var m=text.match(/รวมทั้งหมด[^\d\-]*?([\d,]+)(?:\.\d+)?/);
  return m?parseInt(m[1].replace(/,/g,''),10)||0:0;
}

// แกะ "ลูกค้าที่ค้างชำระ" จากรายงานแยกลูกค้า → [{name,id,amount(รวมภาษี)}]
// ฟอร์แมต OCR ไทย: บล็อกละ 6 บรรทัด = รหัส / ชื่อ / จำนวน / ยอด(ไม่รวมภาษี) / ภาษี / ยอด(รวมภาษี)
// นับเฉพาะลูกค้าที่ชื่อมีคำว่า 'ค้างชำระ' (บัญชีเปิดบิลค้าง)
function parseCustomerCredit_(text){
  if(!text) return [];
  var L=text.split(/[\n\r]+/).map(function(x){return x.trim();}).filter(function(x){return x;});
  var num=function(s){s=String(s).replace(/,/g,''); return /^-?\d+(\.\d+)?$/.test(s)?parseFloat(s):null;};
  var isId=function(s){return /^\d{2,}$/.test(s);};   // รหัสลูกค้า = เลขล้วน (000000028 / 001)
  var out=[];
  for(var i=0;i+5<L.length;i++){
    var id=L[i], name=L[i+1], cnt=num(L[i+2]), exc=num(L[i+3]), tax=num(L[i+4]), inc=num(L[i+5]);
    if(isId(id) && !/^-?\d/.test(name) && /[ก-๙A-Za-z]/.test(name) && cnt!==null && exc!==null && tax!==null && inc!==null){
      if(name.indexOf('ค้างชำระ')>=0) out.push({name:name,id:id,amount:Math.round(inc*100)/100});
      i+=5;   // ข้ามทั้งบล็อก (ลูป i++ ต่อ → ไปบล็อกถัดไป)
    }
  }
  return out;
}

// เติม "ค้างชำระรายลูกค้า" ให้วันเก่าใน pos_daily (OCR แค่รายงานลูกค้า 1 ไฟล์/วัน) — รันซ้ำได้จนครบ
function backfillCredit(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80), cust={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i);
    if(mm && /SalesSummaryReportByCustomer|แยกตามลูกค้า/i.test(n) && !cust[mm[1]]) cust[mm[1]]=at;
  });});});
  var daily=loadDaily_(), byDate={}; daily.forEach(function(d){byDate[d.date]=d;});
  var sufs=Object.keys(cust).sort().reverse(), added=0, ocr=0;
  for(var i=0;i<sufs.length && added<15;i++){
    var txt; try{ txt=pdfToText_(cust[sufs[i]].copyBlob()); ocr++; }
    catch(e){ Logger.log('⏳ OCR หยุดที่ '+sufs[i]+' (โควตา?) — รอแล้วรันซ้ำ: '+(e&&e.message?e.message:e)); break; }
    var dd=dailyDate_(txt); if(!dd) continue;
    var day=byDate[dd.key];
    if(!day || Array.isArray(day.credit)) continue;   // ไม่มีวันนี้ / มี credit แล้ว (เช่นวันที่ 16 ใส่มือ) → ข้าม
    day.credit=parseCustomerCredit_(txt); added++;
    Logger.log('เก็บค้างชำระ '+dd.key+' — '+day.credit.length+' คน รวม '+day.credit.reduce(function(a,c){return a+c.amount;},0).toFixed(2));
  }
  saveDaily_(daily); writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ backfillCredit เพิ่ม '+added+' วัน (OCR '+ocr+' ครั้ง) — รันซ้ำได้ถ้ายังไม่ครบ');
}

// เก็บ 1 วัน (รายงานล่าสุด) แล้ว rebuild — ให้ trigger เรียกตัวนี้ทุกวัน
function importPosReports(){
  var s=findLatestPdf_('SaleReport'), p=findLatestPdf_('PayoutReport');
  if(!s||!p){Logger.log('ไม่พบไฟล์รายงาน ข้ามรอบนี้');return;}
  // กันทำงานซ้ำ/เปลือง OCR: ถ้าอีเมล SaleReport ฉบับล่าสุดถูก import ไปแล้ว (จาก trigger รอบก่อนคืนเดียวกัน) → ข้ามก่อน OCR
  var props=PropertiesService.getScriptProperties();
  var lastMs=Number(props.getProperty('LAST_IMPORTED_EMAIL_MS')||0);
  if(s.time && s.time<=lastMs){ Logger.log('อีเมลรอบนี้ import ไปแล้ว ('+new Date(s.time)+') — ข้าม'); return; }
  var b=findLatestPdf_('BalanceCashDrawer');
  var st=pdfToText_(s.blob), pt=pdfToText_(p.blob);
  var dd=dailyDate_(st)||dailyDate_(pt);
  if(!dd){Logger.log('อ่านวันที่ไม่ได้ ข้าม');return;}
  var balText=b?pdfToText_(b.blob):'';
  var pay=balText?parseBalance_(balText):{}, zones=balText?parseBalanceZones_(balText):{};
  var c=findLatestPdf_('SalesSummaryReportByCustomer'); var bills=0, credit=[];
  try{ if(c){ var ct=pdfToText_(c.blob); bills=parseBills_(ct); credit=parseCustomerCredit_(ct); } }catch(e){}
  var day={date:dd.key,period:dd.period,sales:parseSale_(st).cats,expenses:parsePayout_(pt).records,payments:pay,menu:parseSaleItems_(st),zones:zones,voids:parseVoids_(st),bills:bills,credit:credit};
  var daily=loadDaily_().filter(function(x){return x.date!==dd.key;}); // กันซ้ำ
  daily.push(day); saveDaily_(daily);
  writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  props.setProperty('LAST_IMPORTED_EMAIL_MS', String(s.time||0));   // จำว่าอีเมลฉบับนี้ import แล้ว (กัน trigger สำรองทำ OCR ซ้ำคืนเดียวกัน)
  var dayS=day.sales.reduce(function(a,c){return a+c.amount;},0);
  Logger.log('✅ เก็บวันที่ '+dd.key+' ('+dd.period+') ยอดขายวันนั้น '+dayS.toLocaleString()+' | สะสม '+daily.length+' วัน');
  // ส่งแจ้งเตือนเฉพาะถ้า "วันนี้ยังไม่เคยแจ้ง" — จำวันที่แจ้งล่าสุดใน Script Properties
  // (ใช้ค่านี้ ไม่ใช่ "มีใน pos_daily" เพราะ backfill ก็เก็บวันโดยไม่แจ้ง → จะพลาดการส่ง)
  if(props.getProperty('LAST_ALERT_DATE')===dd.key){ Logger.log('⏸️ แจ้งเตือน '+dd.key+' ส่งไปแล้ว — ไม่ส่งซ้ำ'); return; }
  sendDailyAlert();                                   // ส่งสรุป+เตือนเข้า LINE เจ้าของ
  props.setProperty('LAST_ALERT_DATE', dd.key);       // จำว่าแจ้งวันนี้แล้ว
}

// เก็บย้อนหลัง (รันครั้งเดียวตอนติดตั้ง เพื่อสะสมเดือนปัจจุบัน) — จับคู่ Sale/Payout จากเลขท้ายชื่อไฟล์
function backfillPos(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80);
  var sale={}, pay={}, bal={}, cust={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i); if(!mm) return;
    if(/SaleReport/i.test(n)&&!sale[mm[1]]) sale[mm[1]]=at;
    if(/PayoutReport/i.test(n)&&!pay[mm[1]]) pay[mm[1]]=at;
    if(/BalanceCashDrawer/i.test(n)&&!bal[mm[1]]) bal[mm[1]]=at;
    if(/SalesSummaryReportByCustomer|แยกตามลูกค้า/i.test(n)&&!cust[mm[1]]) cust[mm[1]]=at;
  });});});
  var daily=loadDaily_(), haveFull={}, haveSuf={}, allowYM={};
  daily.forEach(function(d){
    if(d.payments&&Object.keys(d.payments).length&&d.menu&&d.menu.length) haveFull[d.date]=true;
    var p=(d.date||'').split('-');   // ชื่อไฟล์ = วันเนื้อหา +1 (พิมพ์เช้าวันรุ่งขึ้น) → suffix ที่คาด = (วันที่เก็บ +1)
    if(p.length===3){ allowYM[p[0]+'-'+p[1]]=true;
      var fn=new Date(+p[0], +p[1]-1, +p[2]+1);
      haveSuf[''+fn.getFullYear()+(fn.getMonth()+1)+fn.getDate()]=true; }
  });
  // อนุญาตเฉพาะเดือนที่ POS ดูแลอยู่ (มีใน pos_daily) + เดือนปัจจุบัน — ไม่แตะเดือนที่ปลดไปใช้ Excel (เช่น June)
  var now=new Date(); allowYM[now.getFullYear()+'-'+('0'+(now.getMonth()+1)).slice(-2)]=true;
  var allowSuf={};
  Object.keys(allowYM).forEach(function(ym){ var a=ym.split('-'),y=parseInt(a[0],10),mo=parseInt(a[1],10);
    for(var dd2=1;dd2<=31;dd2++) allowSuf[''+y+mo+dd2]=true; });
  var sufs=Object.keys(sale).filter(function(k){return pay[k];}).sort().reverse(); // ใหม่ก่อน
  var added=0, ocrDone=0;
  for(var i=0;i<sufs.length && added<12;i++){       // จำกัด 12 วัน/รอบ (3 OCR/วัน) กัน rate limit
    if(!allowSuf[sufs[i]] || haveSuf[sufs[i]]) continue;   // ★ ข้ามเดือน Excel + วันที่เก็บแล้ว → ไม่ OCR (ประหยัดโควตา + ไม่ทับ June)
    var st,pt,bt,dd;
    try{
      st=pdfToText_(sale[sufs[i]].copyBlob()); ocrDone++;
      dd=dailyDate_(st);
      var okM = dd && allowYM[(dd.key||'').slice(0,7)];   // เดือนของเนื้อหาต้องเป็นเดือนที่ POS ดูแล (กัน June หลุดเข้ามา)
      var ok = okM && !haveFull[dd.key];
      Logger.log('🔎 OCR suf='+sufs[i]+' → วันที่='+(dd?dd.key:'อ่านไม่ออก(null)')+' → '+(ok?'เก็บ ✅':(!dd?'ข้าม':(!okM?'ข้าม(เดือน Excel)':'ข้าม(มีแล้ว)'))));
      if(!ok) continue;
      pt=pdfToText_(pay[sufs[i]].copyBlob());
      bt=bal[sufs[i]]?pdfToText_(bal[sufs[i]].copyBlob()):'';
    }catch(e){ Logger.log('⏳ หยุดที่ suffix '+sufs[i]+' — error จริง: '+(e&&e.message?e.message:e)); break; }
    var pm=bt?parseBalance_(bt):{}, zn=bt?parseBalanceZones_(bt):{};
    var bills=0, credit=[]; try{ if(cust[sufs[i]]){ var ct=pdfToText_(cust[sufs[i]].copyBlob()); bills=parseBills_(ct); credit=parseCustomerCredit_(ct); } }catch(e2){}  // จำนวนบิล + ค้างชำระรายลูกค้า
    daily=daily.filter(function(x){return x.date!==dd.key;});
    daily.push({date:dd.key,period:dd.period,sales:parseSale_(st).cats,expenses:parsePayout_(pt).records,payments:pm,menu:parseSaleItems_(st),zones:zn,voids:parseVoids_(st),bills:bills,credit:credit});
    haveFull[dd.key]=true; added++; Logger.log('เก็บ '+dd.key);
  }
  saveDaily_(daily); writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ backfill เพิ่ม '+added+' วัน | สะสมทั้งหมด '+daily.length+' วัน | ใช้ OCR ไป '+ocrDone+' ครั้ง (ข้ามวันเก่าโดยไม่ OCR แล้ว)');
}

// ===== เติม "ยอดขายแยกโซน" ให้วันเก่า (OCR แค่ BalanceCashDrawer = เบา 1 ไฟล์/วัน) — รันครั้งเดียว =====
function backfillZones(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80), bal={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i);
    if(mm && /BalanceCashDrawer/i.test(n) && !bal[mm[1]]) bal[mm[1]]=at;
  });});});
  var daily=loadDaily_(), added=0, sufs=Object.keys(bal).sort().reverse();
  for(var i=0;i<sufs.length && added<20;i++){
    var bt; try{ bt=pdfToText_(bal[sufs[i]].copyBlob()); }catch(e){ Logger.log('⏳ ชน OCR limit หยุดไว้ก่อน — รอ ~5 นาทีแล้วรัน backfillZones ซ้ำ'); break; }
    var dd=dailyDate_(bt); if(!dd) continue;
    var day=null; for(var k=0;k<daily.length;k++) if(daily[k].date===dd.key){day=daily[k];break;}
    if(!day || (day.zones&&Object.keys(day.zones).length)) continue;  // ไม่มีวันนี้ หรือมีโซนแล้ว → ข้าม
    day.zones=parseBalanceZones_(bt); added++; Logger.log('เก็บโซน '+dd.key);
  }
  saveDaily_(daily); writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ เพิ่มโซน '+added+' วัน (รันซ้ำได้ถ้าไม่ครบ)');
}

// ===== เติม "ยอดยกเลิก/ลบบิล" ให้วันเก่า (OCR แค่ SaleReport = 1 ไฟล์/วัน) — รันครั้งเดียว =====
function backfillVoids(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80), sale={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i);
    if(mm && /SaleReport/i.test(n) && !sale[mm[1]]) sale[mm[1]]=at;
  });});});
  var daily=loadDaily_(), added=0, sufs=Object.keys(sale).sort().reverse();
  for(var i=0;i<sufs.length && added<20;i++){
    var st; try{ st=pdfToText_(sale[sufs[i]].copyBlob()); }catch(e){ Logger.log('⏳ ชน OCR limit หยุดไว้ก่อน — รอ ~5 นาทีแล้วรัน backfillVoids ซ้ำ'); break; }
    var dd=dailyDate_(st); if(!dd) continue;
    var day=null; for(var k=0;k<daily.length;k++) if(daily[k].date===dd.key){day=daily[k];break;}
    if(!day || (day.voids&&Object.keys(day.voids).length)) continue;
    day.voids=parseVoids_(st); added++; Logger.log('เก็บยอดยกเลิก '+dd.key);
  }
  saveDaily_(daily); writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ เพิ่มยอดยกเลิก '+added+' วัน (รันซ้ำได้)');
}

// ===== ลบเดือนที่ backfill เผลอเก็บมา เก็บไว้แค่เดือนล่าสุด (เดือนปัจจุบัน) — รันครั้งเดียว =====
function cleanupKeepCurrentMonth(){
  var daily=loadDaily_();
  if(!daily.length){Logger.log('ไม่มีข้อมูลรายวัน');return;}
  var latest=daily.reduce(function(a,b){return b.date>a.date?b:a;}).period; // เดือนของวันที่ล่าสุด
  var kept=daily.filter(function(d){return d.period===latest;});
  var removed={}; daily.forEach(function(d){if(d.period!==latest)removed[d.period]=true;});
  saveDaily_(kept);
  // ลบเดือน POS ที่ไม่เอา ออกจาก saisang_data.json (คงเดือนที่อัปมือไว้)
  var f=getFile_();
  if(f){ var arr=[]; try{arr=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){}
    if(!Array.isArray(arr))arr=[];
    f.setContent(JSON.stringify(arr.filter(function(m){return !removed[m.period];}))); }
  writeMonths_(rebuildMonths_(kept, fetchSlipMap_())); // ใส่เดือนปัจจุบันกลับ
  Logger.log('✅ เก็บเฉพาะ '+latest+' | ลบออก: '+(Object.keys(removed).join(', ')||'(ไม่มี)'));
}

// ===== สร้างข้อมูลเดือนใหม่จากที่เก็บไว้ (ไม่ OCR) — รันหลังอัปเดตโค้ด/ใส่ SLIP_API_URL =====
function rebuildNow(){
  var n=writeMonths_(rebuildMonths_(loadDaily_(), fetchSlipMap_()));
  Logger.log('✅ สร้างข้อมูลเดือนใหม่เรียบร้อย ('+n+' เดือนในไฟล์)');
}

// ===== ให้ POS "เลิกคุม" เดือนที่ระบุ (เพื่ออัป Excel เองแล้วไม่โดน POS ทับ) =====
function dropPosMonth_(period){
  var daily=loadDaily_().filter(function(d){return d.period!==period;});  // เอาวันของเดือนนี้ออกจาก pos_daily.json
  saveDaily_(daily);
  var f=getFile_(), arr=[];
  if(f){try{arr=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){}}
  var before=arr.length; arr=arr.filter(function(m){return m.period!==period;});  // เอาเดือนนี้ออกจาก saisang_data.json (รอ Excel อัปใหม่)
  if(f) f.setContent(JSON.stringify(arr));
  Logger.log('✅ '+period+': POS เลิกคุมแล้ว (pos_daily เหลือ '+daily.length+' วัน · saisang_data เหลือ '+arr.length+' เดือน · ลบทิ้ง '+(before-arr.length)+')');
  Logger.log('👉 ไปที่ dashboard → อัปไฟล์ Excel เดือน '+period+' ใหม่ได้เลย · POS จะไม่ทับอีก');
}
// 👉 รันตัวนี้: ให้ "มิถุนายน 2569" ใช้ Excel (POS เลิกคุม)
function juneUseExcel(){ dropPosMonth_('มิถุนายน 2569'); }

// ===== พยากรณ์กระแสเงินสดเดือนถัดไป + จุดควรลดต้นทุน =====
function forecastCashflow(){
  var f=getFile_(), months=[]; if(f){try{months=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){}}
  if(!months.length){ Logger.log('ไม่มีข้อมูลเดือน'); return; }
  function ord(p){ var a=(p||'').split(' '); return (parseInt(a[1],10)||0)*12 + THAI_MONTHS.indexOf(a[0]); }
  months.sort(function(x,y){return ord(x.period)-ord(y.period);});
  // ตัดเดือนปัจจุบัน (ยังไม่จบ = ไม่เต็มเดือน) ออกจากการคำนวณเทรนด์
  var now=new Date(), curP=THAI_MONTHS[now.getMonth()]+' '+(now.getFullYear()+543);
  var full=months.filter(function(m){return m.period!==curP && (m.total_sales||0)>0;});
  if(full.length<2){ Logger.log('ข้อมูลเดือนเต็มยังน้อยเกินไป (มี '+full.length+' เดือน) — เก็บเพิ่มก่อน'); return; }
  var win=full.slice(-Math.min(4,full.length));   // ใช้ 4 เดือนเต็มล่าสุด
  var avgS=_avg_(win.map(function(m){return m.total_sales||0;}));
  var avgE=_avg_(win.map(function(m){return m.total_expenses||0;}));
  var trend=win.length>1?((win[win.length-1].total_sales-win[0].total_sales)/(win[0].total_sales||1)*100):0;
  var fSales=avgS, fExp=avgE, fCash=fSales-fExp;
  Logger.log('🔮 พยากรณ์เดือนถัดไป (เฉลี่ยจาก '+win.length+' เดือนเต็มล่าสุด: '+win.map(function(m){return m.period;}).join(', ')+')');
  Logger.log('  📈 ยอดขายคาด: ฿'+fmtT_(fSales)+' (แนวโน้มช่วงนี้ '+(trend>=0?'+':'')+trend.toFixed(0)+'%)');
  Logger.log('  💸 ค่าใช้จ่ายคาด: ฿'+fmtT_(fExp)+' (cost ratio ~'+(fSales>0?(fExp/fSales*100).toFixed(0):0)+'%)');
  Logger.log('  💵 กระแสเงินสดสุทธิคาด: '+(fCash>=0?'+':'-')+'฿'+fmtT_(Math.abs(fCash))+(fCash<0?'  ⚠️ ติดลบ!':' ✅'));
  // รวมค่าใช้จ่ายรายหมวด (เฉลี่ยจากหน้าต่างเดียวกัน) → จัดอันดับหาจุดลดต้นทุน
  var cat={}; win.forEach(function(m){ (m.expense_categories||[]).forEach(function(c){ cat[c.category]=(cat[c.category]||0)+c.amount; }); });
  var rows=Object.keys(cat).map(function(k){return {c:k, avg:cat[k]/win.length};}).sort(function(a,b){return b.avg-a.avg;});
  Logger.log('─────────────');
  Logger.log('💡 จุดที่ควรพิจารณาลดต้นทุน (เฉลี่ย/เดือน · % ของยอดขาย):');
  rows.slice(0,7).forEach(function(r,i){
    var pct=fSales>0?r.avg/fSales*100:0, tip=_costTip_(r.c,pct);
    Logger.log('  '+(i+1)+'. '+r.c+' → ฿'+fmtT_(r.avg)+'/เดือน ('+pct.toFixed(1)+'%)'+(tip?' — '+tip:''));
  });
}
function _costTip_(name,pct){
  if(/วัตถุดิบ|ของสด|อาหาร/.test(name)) return pct>38?'สูง! เจรจาซัพพลายเออร์/ลดของเสีย/เช็กสต๊อก':'คุมได้ดี';
  if(/เงินเดือน|พนักงาน|พนง|แรงงาน|ค่าจ้าง/.test(name)) return pct>30?'สูง! จัดกะตามชั่วโมงพีค ลดคนวันเงียบ':'โอเค';
  if(/ค่าเช่า|เช่า/.test(name)) return 'คงที่ ลดยาก — โฟกัสเพิ่มยอดต่อ ตร.ม.';
  if(/ไฟ|น้ำ|ไฟฟ้า|ประปา|utility/i.test(name)) return 'เช็กแอร์/ตู้แช่/หลอดไฟ ประหยัดพลังงาน';
  if(/การตลาด|โฆษณา|marketing|ยิงแอด/i.test(name)) return 'วัด ROI ต่อแคมเปญ ตัดตัวที่ไม่คุ้ม';
  if(/สุรา|เหล้า|เบียร์|เครื่องดื่ม/.test(name)) return 'ต้นทุนบาร์ — คุมการริน/ของแถม/ของหาย';
  return pct>10?'ก้อนใหญ่ ควรลงรายละเอียดว่าจ่ายอะไรบ้าง':'';
}

// ===== ติดตามกำไรเครื่องดื่มรายเดือน (เป้า ≥60%) + โอกาสถ้าถึงเป้า =====
function beverageWatch(){
  var f=getFile_(), months=[]; if(f){try{months=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){}}
  if(!months.length){ Logger.log('ไม่มีข้อมูลเดือน'); return; }
  function ord(p){ var a=(p||'').split(' '); return (parseInt(a[1],10)||0)*12 + THAI_MONTHS.indexOf(a[0]); }
  months.sort(function(x,y){return ord(x.period)-ord(y.period);});
  function bev(m){ return {
    s:(m.sales_categories||[]).filter(function(c){return /เครื่องดื่ม/.test(c.category);}).reduce(function(a,c){return a+c.amount;},0),
    c:(m.expense_categories||[]).filter(function(c){return /เครื่องดื่ม/.test(c.category);}).reduce(function(a,c){return a+c.amount;},0) }; }
  var TARGET=60, last=null;
  Logger.log('🍺 กำไรเครื่องดื่มรายเดือน (เป้า ≥'+TARGET+'%)'); Logger.log('─────────────');
  months.forEach(function(m){ var b=bev(m); if(b.s<=0) return;
    var mg=(b.s-b.c)/b.s*100, flag=mg>=TARGET?'✅':(mg>=50?'⚠️':'🔴');
    var line='  '+flag+' '+m.period+' → กำไร '+mg.toFixed(0)+'%  (ขาย ฿'+fmtT_(b.s)+' · ทุน ฿'+fmtT_(b.c)+')';
    if(last!==null) line+='  '+(mg>=last?'▲+':'▼')+(mg-last).toFixed(0)+'%';
    Logger.log(line); last=mg;
  });
  for(var i=months.length-1;i>=0;i--){ var b=bev(months[i]); if(b.s<=0) continue;
    var mg=(b.s-b.c)/b.s*100, gap=(TARGET/100*b.s)-(b.s-b.c);
    Logger.log('─────────────'); Logger.log('📌 เดือนล่าสุด ('+months[i].period+'): กำไรเครื่องดื่ม '+mg.toFixed(0)+'%');
    Logger.log(mg<TARGET?('   💰 ถ้าดันถึงเป้า '+TARGET+'% = ได้เพิ่ม ฿'+fmtT_(gap)+'/เดือน'):'   ✅ ถึงเป้าแล้ว!');
    break;
  }
}

// ===== เติม "จำนวนบิล" ให้วันที่เก็บไว้แล้ว (OCR เฉพาะรายงานลูกค้า) — รันครั้งเดียว รันซ้ำได้ =====
function backfillBills(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80), cust={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i); if(!mm) return;
    if(/SalesSummaryReportByCustomer|แยกตามลูกค้า/i.test(n)&&!cust[mm[1]]) cust[mm[1]]=at;
  });});});
  var daily=loadDaily_(), added=0, ocr=0;
  for(var i=0;i<daily.length && ocr<15;i++){ var d=daily[i];
    if(d.bills) continue;                                   // มีแล้วข้าม
    var p=(d.date||'').split('-'); if(p.length<3) continue;
    var fn=new Date(+p[0],+p[1]-1,+p[2]+1), suf=''+fn.getFullYear()+(fn.getMonth()+1)+fn.getDate();  // ชื่อไฟล์ = วัน +1
    if(!cust[suf]) continue;
    try{ d.bills=parseBills_(pdfToText_(cust[suf].copyBlob())); ocr++; if(d.bills)added++; }
    catch(e){ Logger.log('⏳ ชน rate limit หยุดไว้ (เติมได้ '+added+' วัน) — พักแล้วรันซ้ำได้'); break; }
  }
  saveDaily_(daily); writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ เติมจำนวนบิล: '+added+' วัน | ใช้ OCR '+ocr+' ครั้ง (รันซ้ำได้ถ้ายังไม่ครบ)');
}

// ===== กำไร/ขาดทุน เฉลี่ยต่อวัน แยกตามวันในสัปดาห์ (+ยอดต่อบิล) =====
function profitByWeekday(){
  var daily=loadDaily_(); if(!daily.length){ Logger.log('ไม่มีข้อมูลรายวัน'); return; }
  var WD=['อาทิตย์','จันทร์','อังคาร','พุธ','พฤหัสบดี','ศุกร์','เสาร์'], agg={};
  daily.forEach(function(d){
    var p=(d.date||'').split('-'); if(p.length<3) return;
    var w=new Date(+p[0],+p[1]-1,+p[2]).getDay(), sm=daySum_(d);
    var a=agg[w]||(agg[w]={s:0,e:0,cnt:0,bills:0,bcnt:0});
    a.s+=sm.sales; a.e+=sm.exp; a.cnt++; if(d.bills){ a.bills+=d.bills; a.bcnt++; }
  });
  var rows=[];
  for(var w=0;w<7;w++){ var a=agg[w]; if(!a) continue;
    rows.push({name:WD[w], sales:a.s/a.cnt, exp:a.e/a.cnt, prof:(a.s-a.e)/a.cnt, cnt:a.cnt,
      tik:a.bcnt?(a.s/a.cnt)/(a.bills/a.bcnt):0}); }
  rows.sort(function(x,y){return x.prof-y.prof;});   // ขาดทุนสุดขึ้นก่อน
  Logger.log('📅 กำไร/ขาดทุน เฉลี่ยต่อวัน แยกวันในสัปดาห์ (จาก '+daily.length+' วัน):');
  rows.forEach(function(r){
    Logger.log('  '+(r.prof>=0?'✅':'🔴')+' '+r.name+' → '+(r.prof>=0?'กำไร +':'ขาดทุน -')+'฿'+fmtT_(Math.abs(r.prof))+'/วัน  (ขาย ฿'+fmtT_(r.sales)+' · จ่าย ฿'+fmtT_(r.exp)+' · '+r.cnt+' วัน'+(r.tik?' · ยอด/บิล ฿'+fmtT_(r.tik):'')+')');
  });
  var loss=rows.filter(function(r){return r.prof<0;});
  Logger.log(loss.length?('💡 วันขาดทุน: '+loss.map(function(r){return r.name;}).join(', ')+' → ลดต้นทุน/พิจารณาปิดคืนเหล่านี้'):'✅ ทุกวันกำไรเฉลี่ยเป็นบวก');
}

// ===== วิเคราะห์ "ต้นทุนแฝง" (เงินรั่ว: ลบบิล+ยกเลิก+คืน+ส่วนลด) แยกตามวันในสัปดาห์ =====
function analyzeWeekday(){
  var daily=loadDaily_();
  if(!daily.length){ Logger.log('ไม่มีข้อมูลรายวัน (ต้องมีเดือนที่ POS ดูแล)'); return; }
  var WD=['อาทิตย์','จันทร์','อังคาร','พุธ','พฤหัสบดี','ศุกร์','เสาร์'], agg={};
  daily.forEach(function(d){
    var p=(d.date||'').split('-'); if(p.length<3) return;
    var w=new Date(+p[0],+p[1]-1,+p[2]).getDay();
    var v=d.voids||{}, del=v.deleted||0, can=v.cancelled||0, ret=v.returns||0, dis=v.discount||0, leak=del+can+ret+dis;
    var sales=(d.sales||[]).reduce(function(a,c){return a+(c.amount||0);},0);
    var a=agg[w]||(agg[w]={leak:0,sales:0,cnt:0,del:0,can:0,ret:0,dis:0});
    a.leak+=leak; a.sales+=sales; a.cnt++; a.del+=del; a.can+=can; a.ret+=ret; a.dis+=dis;
  });
  var rows=[];
  for(var w=0;w<7;w++){ var a=agg[w]; if(!a) continue;
    rows.push({name:WD[w], avg:a.leak/a.cnt, pct:a.sales>0?a.leak/a.sales*100:0, cnt:a.cnt,
      del:a.del/a.cnt, can:a.can/a.cnt, dis:a.dis/a.cnt}); }
  rows.sort(function(x,y){return y.avg-x.avg;});
  Logger.log('📊 ต้นทุนแฝง (เงินรั่ว) เฉลี่ยต่อวัน แยกตามวันในสัปดาห์ — จากข้อมูล '+daily.length+' วัน:');
  rows.forEach(function(r,i){ Logger.log((i+1)+'. '+r.name+' → ฿'+Math.round(r.avg).toLocaleString()+'/วัน ('+r.pct.toFixed(1)+'% ของยอดขาย) · '+r.cnt+' วัน · [ลบบิล ฿'+Math.round(r.del).toLocaleString()+' | ยกเลิก ฿'+Math.round(r.can).toLocaleString()+' | ส่วนลด ฿'+Math.round(r.dis).toLocaleString()+']'); });
  if(rows.length) Logger.log('🔴 สูงสุด = วัน'+rows[0].name+' (฿'+Math.round(rows[0].avg).toLocaleString()+'/วัน · '+rows[0].pct.toFixed(1)+'%)');
}

// ==========================================================================
//  [ใหม่ 17/08/26] กู้ยอดของ "วันที่ระบุ" — ใช้เมื่อรายงานถูกส่งซ้ำภายหลัง
//  ปัญหาที่แก้: importPosReports()/backfillPos() จับไฟล์ด้วย "เลขท้ายชื่อไฟล์" = วันที่พิมพ์
//    เคสจริง: รายงานของ 11 + 12 ส.ค. ถูกส่งใหม่พร้อมกันวันที่ 13 ส.ค.
//    → ไฟล์ทั้งสองวันลงท้าย _2026813.pdf เหมือนกัน → คีย์ชนกัน ตัวหนึ่งถูกทับ
//    → วันที่ 11 ไม่เคยถูก import และ backfillPos ก็หาไม่เจอ (มองแต่ชื่อไฟล์)
//  ตัวนี้เลือกอีเมลจาก "วันที่ในหัวเรื่อง" (รองรับทั้ง พ.ศ. 2569 และ ค.ศ. 2026)
//  แล้วอ่านวันที่จริงจากเนื้อหา PDF ยืนยันอีกชั้น
//  วิธีใช้: แก้วันที่ในบรรทัดล่างสุดของบล็อกนี้ แล้วรัน importPosByDate
// ==========================================================================

// ดึงวันที่จากหัวเรื่องอีเมล "รายงานยอดขายวันSYS4 11/08/2569" -> '2026-08-11'
function _subjDateIso_(subject){
  var m=String(subject||'').match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if(!m) return null;
  var d=parseInt(m[1],10), mo=parseInt(m[2],10), y=parseInt(m[3],10);
  if(y>=2500) y-=543;                       // พ.ศ. -> ค.ศ.
  if(!(mo>=1&&mo<=12&&d>=1&&d<=31)) return null;
  return y+'-'+('0'+mo).slice(-2)+'-'+('0'+d).slice(-2);
}

function _importPosOneDate_(iso){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:60d',0,100), hit=null;
  threads.forEach(function(t){ t.getMessages().forEach(function(msg){
    if(!hit && _subjDateIso_(msg.getSubject())===iso && msg.getAttachments().length) hit=msg;
  });});
  if(!hit){ Logger.log('❌ ไม่พบอีเมลรายงานของ '+iso+' — หัวเรื่องต้องมีวันที่แบบ dd/mm/yyyy (พ.ศ. หรือ ค.ศ. ก็ได้)'); return; }
  Logger.log('📧 ใช้อีเมล: "'+hit.getSubject()+'"  (เข้ามา '+hit.getDate()+')');
  var att={};
  hit.getAttachments().forEach(function(a){ var n=a.getName();
    if(!/\.pdf$/i.test(n)) return;
    if(/SaleReport/i.test(n)) att.sale=att.sale||a;
    else if(/PayoutReport/i.test(n)) att.pay=att.pay||a;
    else if(/BalanceCashDrawer/i.test(n)) att.bal=att.bal||a;
    else if(/SalesSummaryReportByCustomer|แยกตามลูกค้า/i.test(n)) att.cust=att.cust||a;
  });
  if(!att.sale){ Logger.log('❌ อีเมลนี้ไม่มีไฟล์ SaleReport — กู้ไม่ได้'); return; }
  var st=pdfToText_(att.sale.copyBlob());
  var dd=dailyDate_(st);
  if(!dd || dd.key!==iso){                  // เนื้อหาอ่านไม่ออก/ไม่ตรง → เชื่อวันที่ที่ระบุ (มาจากหัวเรื่อง)
    Logger.log('⚠️ วันที่ในเนื้อหา PDF = '+(dd?dd.key:'อ่านไม่ออก')+' ไม่ตรงกับที่ขอ ('+iso+') → ใช้ '+iso+' ตามหัวเรื่อง');
    var p=iso.split('-');
    dd={key:iso, period:THAI_MONTHS[parseInt(p[1],10)-1]+' '+(parseInt(p[0],10)+543)};
  }
  var pt=att.pay?pdfToText_(att.pay.copyBlob()):'';
  var bt=att.bal?pdfToText_(att.bal.copyBlob()):'';
  // บิล/ลูกหนี้ มาจากไฟล์ 'แยกตามลูกค้า' — ของเดิมเป็น catch(e){} เปล่าๆ + ไม่เตือนตอนไฟล์หาย
  // ทำให้ 'บิล 0' แยกไม่ออกว่า (ก) ไม่มีบิลจริง (ข) ไม่มีไฟล์ในเมล (ค) OCR พัง → ต้องบอกให้รู้
  var bills=0, credit=[], custWarn='';
  try{
    if(att.cust){ var ct=pdfToText_(att.cust.copyBlob()); bills=parseBills_(ct); credit=parseCustomerCredit_(ct); }
    else custWarn='  ⚠️ ไม่มีไฟล์ SalesSummaryReportByCustomer → บิล/ลูกหนี้ = 0 (ไม่ใช่ว่าไม่มีจริง)';
  }catch(e){ custWarn='  ⚠️ อ่าน SalesSummaryReportByCustomer ไม่ได้ ('+e+') → บิล/ลูกหนี้ = 0'; }
  var day={date:dd.key, period:dd.period,
    sales:parseSale_(st).cats, expenses:pt?parsePayout_(pt).records:[],
    payments:bt?parseBalance_(bt):{}, menu:parseSaleItems_(st),
    zones:bt?parseBalanceZones_(bt):{}, voids:parseVoids_(st), bills:bills, credit:credit};
  var daily=loadDaily_().filter(function(x){return x.date!==dd.key;});   // ทับวันเดิมถ้ามี
  daily.push(day); saveDaily_(daily);
  writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));                  // rebuild ให้ dashboard เลย
  var totS=day.sales.reduce(function(a,c){return a+c.amount;},0);
  var totE=day.expenses.reduce(function(a,c){return a+c.amount;},0);
  Logger.log('✅ กู้วันที่ '+dd.key+' ('+dd.period+') สำเร็จ — ยอดขาย '+totS.toLocaleString()+
             ' · ค่าใช้จ่าย '+totE.toLocaleString()+' · เมนู '+day.menu.length+' รายการ · บิล '+bills+
             (pt?'':'  ⚠️ ไม่มี PayoutReport ในอีเมลนี้ → ค่าใช้จ่าย = 0')+
             (bt?'':'  ⚠️ ไม่มี BalanceCashDrawer → ไม่มีวิธีชำระ/โซน')+custWarn);
  Logger.log('👉 เปิด dashboard เช็คได้เลย (rebuild ให้แล้ว) · ถ้าจะกู้วันอื่นแก้วันที่ใน importPosByDate แล้วรันซ้ำ');
}

// 👉 ใส่วันที่ที่ต้องการกู้ตรงนี้ — ใส่กี่วันก็ได้ คั่นด้วยจุลภาค แล้วรันฟังก์ชันนี้ครั้งเดียว
// ใช้ OCR ~4 ครั้ง/วัน · ชนลิมิต (⏳) ให้พัก ~5 นาทีแล้วรันซ้ำได้เลย — วันที่กู้สำเร็จแล้วจะถูกทับด้วยข้อมูลเดิม ไม่เสียหาย
function importPosByDate(){
  var DATES = ['2026-08-11'];   // ← เปลี่ยนเป็นวันที่ที่ขาดจริง (ดูจาก debugDaily)
  DATES.forEach(function(iso, i){
    Logger.log('───── ' + (i + 1) + '/' + DATES.length + '  กู้วันที่ ' + iso + ' ─────');
    try { _importPosOneDate_(iso); }
    catch(e){ Logger.log('❌ ' + iso + ' พัง: ' + e); }   // วันหนึ่งพังต้องไม่หยุดวันที่เหลือ
  });
  Logger.log('🏁 จบ ' + DATES.length + ' วัน — เลื่อนอ่าน log ข้างบน หา ✅/❌ ของแต่ละวัน');
}

// ===== ดูว่ามีไฟล์รายงาน POS วันไหนในเมลบ้าง (ไม่ OCR เลย = ไม่กินโควตา) =====
function debugReports(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:40d',0,80);
  var sale={},pay={},rows=[];
  threads.forEach(function(t){t.getMessages().forEach(function(msg){
    var md=Utilities.formatDate(msg.getDate(),'GMT+7','MM-dd HH:mm');
    msg.getAttachments().forEach(function(at){
      var n=at.getName(); if(!/\.pdf$/i.test(n)) return;
      var mm=n.match(/_(\d+)\.pdf$/i); if(!mm) return;
      var dm=n.match(/(\d{6})_\d{4,6}\.pdf$/i);
      var type=/SaleReport|ยอดขาย/i.test(n)?'SALE':(/PayoutReport/i.test(n)?'PAY':(/BalanceCashDrawer/i.test(n)?'BAL':'อื่น'));
      if(type==='SALE') sale[mm[1]]=1; if(type==='PAY') pay[mm[1]]=1;
      if(type!=='อื่น') rows.push('  ['+md+'] '+type+' date='+(dm?dm[1]:'?')+' suf='+mm[1]);
    });
  });});
  Logger.log('📄 ไฟล์รายงานในเมล (40 วันล่าสุด):');
  rows.sort().reverse().forEach(function(r){Logger.log(r);});
  var paired=Object.keys(sale).filter(function(k){return pay[k];});
  Logger.log('— มีคู่ SALE+PAY '+paired.length+' ชุด (suffix: '+paired.sort().reverse().join(', ')+')');
}

// วันที่ "ร้านหยุด" — ไม่มีรายงาน POS โดยธรรมชาติ ไม่ใช่ข้อมูลหาย
// มีไว้กัน debugDaily รายงานว่า "ขาด" ซ้ำทุกครั้ง แล้วมีคนไปไล่กู้เปล่าๆ (เสียโควตา OCR ฟรีๆ
// และเคยหลุดไปอยู่ในลิสต์งานค้างจริงมาแล้ว — 29 ก.ค. 2026)
// 👉 ร้านหยุดวันไหนเพิ่ม ใส่เพิ่มตรงนี้ได้เลย
var CLOSED_DAYS = ['2026-07-29'];

// ===== ตรวจสอบว่าเดือนไหนมี "ข้อมูลรายวัน (daily_totals)" — รันแล้วก๊อป log มาดู =====
function debugDaily(){
  var daily=loadDaily_();
  var byP={}; daily.forEach(function(d){byP[d.period]=(byP[d.period]||0)+1;});
  Logger.log('📁 pos_daily.json มี '+daily.length+' วัน แยกตามเดือน:');
  Object.keys(byP).forEach(function(p){Logger.log('   • '+p+' = '+byP[p]+' วัน');});
  // [ใหม่ 17/08/26] โชว์ "วันที่ขาด" ของเดือนล่าสุด — ไว้เช็กว่าวันไหนหาย (เช่น 11 ส.ค.)
  if(daily.length){
    var latest=daily.reduce(function(a,b){return b.date>a.date?b:a;});
    var ym=(latest.date||'').slice(0,7), have={};
    daily.forEach(function(d){ if((d.date||'').indexOf(ym)===0) have[d.date]=true; });
    var y=parseInt(ym.slice(0,4),10), mo=parseInt(ym.slice(5,7),10);
    var lastDay=new Date(y,mo,0).getDate(), today=new Date(), miss=[];
    // รายงาน POS ของวัน D เข้าเมลตอน ~00:3x ของวัน D+1 → 'วันนี้' ยังไม่มีข้อมูลเป็นเรื่องปกติ
    // ไม่หักออก = ขึ้น 'ขาดวันนี้' ทุกครั้งที่รัน (เตือนหลอกถาวร) และเผลอใส่ CLOSED_DAYS ก็ผิด เพราะร้านไม่ได้หยุด
    var maxD=(today.getFullYear()===y && (today.getMonth()+1)===mo)?today.getDate()-1:lastDay;
    var closed=[];
    for(var d2=1;d2<=maxD;d2++){ var k=ym+'-'+('0'+d2).slice(-2);
      if(have[k]) continue;
      if(CLOSED_DAYS.indexOf(k)>=0) closed.push(k);   // ร้านหยุด = ไม่มียอดถูกแล้ว ไม่ต้องไปกู้
      else miss.push(k);
    }
    Logger.log('🔎 เดือนล่าสุด '+ym+': มี '+Object.keys(have).length+' วัน · ขาด '+miss.length+' วัน'+(miss.length?' → '+miss.join(', '):' ✅ ครบ')+
               (closed.length?'  (ไม่นับวันร้านหยุด '+closed.length+' วัน: '+closed.join(', ')+')':''));
    if(miss.length) Logger.log('   👉 กู้ด้วย backfillPos() (รันซ้ำได้จนครบ) แล้วปิดท้าย rebuildNow()'+
                               '\n   ℹ️ ถ้าวันไหนในลิสต์คือวันร้านหยุด ให้ใส่ใน CLOSED_DAYS แทนการไปไล่กู้');
  }
  var f=getFile_(), arr=[]; if(f){try{arr=JSON.parse(f.getBlob().getDataAsString('UTF-8'))||[];}catch(e){}}
  Logger.log('📊 saisang_data.json มี '+arr.length+' เดือน — เช็ก daily_totals:');
  arr.forEach(function(m){
    var dt=m.daily_totals; var pd=m.payment_days;
    Logger.log('   • '+m.period+' → daily_totals: '+(Array.isArray(dt)?(dt.length+' วัน'):'❌ ไม่มีฟิลด์')+
               '  | payment_days: '+(Array.isArray(pd)?(pd.length+' วัน'):'ไม่มี')+
               '  | ยอดขาย: '+Math.round(m.total_sales||0).toLocaleString());
  });
}

// ==========================================================================
//  แจ้งเตือน LINE เจ้าของ (ส่งผ่าน bot /api/push_owner — reuse token จาก SLIP_API_URL)
// ==========================================================================
function fmtT_(n){ return Math.round(n||0).toLocaleString('en-US'); }

// [แก้ 17/08/26] คืนข้อความ error ('' = สำเร็จ) เพื่อให้ผู้เรียกรู้ว่า LINE ส่งไม่ออก
// ของเดิม: กลืน error ไว้เงียบๆ → สรุปไม่เข้า LINE หลายวันโดยไม่มีใครรู้ (เห็นแต่อีเมล)
function pushOwner_(msg){
  var s=PropertiesService.getScriptProperties().getProperty('SLIP_API_URL');
  if(!s||!msg){ var e0='ยังไม่ตั้ง SLIP_API_URL หรือข้อความว่าง'; Logger.log(e0); return e0; }
  var url=s.replace('/api/slip_daily','/api/push_owner');
  try{
    var r=UrlFetchApp.fetch(url,{method:'post',contentType:'application/json',payload:JSON.stringify({message:msg}),muteHttpExceptions:true});
    var code=r.getResponseCode(), body=r.getContentText(), d={};
    try{ d=JSON.parse(body)||{}; }catch(e2){}   // ถ้า host ผิดจะได้ HTML 404 → parse ไม่ได้ ก็ยังรายงาน code ได้
    if(!d.ok){
      var e1='HTTP '+code+' → '+String(body).substring(0,150);
      Logger.log('push ไม่สำเร็จ: '+e1);
      if(code===401) Logger.log('   ↳ token ไม่ตรง: เทียบ SLIP_API_TOKEN บน Render กับ token ใน SLIP_API_URL (รัน showSlipUrl)');
      if(code===403) Logger.log('   ↳ บอทยังไม่ตั้ง env SLIP_API_TOKEN/DASHBOARD_PASSWORD');
      if(code===400) Logger.log('   ↳ บอทยังไม่ตั้ง env LINE_ADMIN_USER_ID');
      if(code===404) Logger.log('   ↳ host ผิด/บอทตัวเก่า: รัน showSlipUrl() แล้ว fixSlipUrl()');
      return e1;
    }
    Logger.log('✅ ส่ง LINE เจ้าของแล้ว');
    return '';
  }catch(e){ var e3=String(e); Logger.log('push error: '+e3+'  ↳ host เข้าไม่ได้? รัน showSlipUrl()'); return e3; }
}

function daySum_(d){ return {
  sales:(d.sales||[]).reduce(function(a,c){return a+c.amount;},0),
  exp:(d.expenses||[]).reduce(function(a,c){return a+c.amount;},0) }; }

function _avg_(arr){ return arr.length? arr.reduce(function(a,b){return a+b;},0)/arr.length : 0; }
function _leakOf_(x){ var v=x.voids||{}; return (v.deleted||0)+(v.cancelled||0)+(v.returns||0)+(v.discount||0); }

// สรุปวันล่าสุด + เตือนอัจฉริยะ (เทียบกับค่าเฉลี่ยของร้านเอง)
function buildDailyMsg_(){
  var daily=loadDaily_(); if(!daily.length) return null;
  var d=daily.reduce(function(a,b){return b.date>a.date?b:a;});
  var hist=daily.filter(function(x){return x.date!==d.date;});   // ค่าฐานจากวันก่อนๆ
  var s=daySum_(d), pm=d.payments||{}, v=d.voids||{}, prof=s.sales-s.exp, leak=_leakOf_(d);
  var lines=['📊 สรุปยอดขาย '+d.date,'─────────────',
    '💰 ยอดขาย: ฿'+fmtT_(s.sales),'💸 ค่าใช้จ่าย: ฿'+fmtT_(s.exp),
    (prof>=0?'✅ กำไร: +฿':'🔻 ขาดทุน: -฿')+fmtT_(Math.abs(prof)),
    '💳 โอน '+fmtT_(pm['เงินโอน']||0)+' · สด '+fmtT_(pm['เงินสด']||0)+' · บัตร '+fmtT_(pm['บัตรเครดิต']||0)];
  if((d.bills||0)>0) lines.push('🧾 ยอดต่อบิล: ฿'+fmtT_(s.sales/d.bills)+' ('+d.bills+' บิล)');
  var smart=hist.length>=3;   // มีข้อมูลพอค่อยเทียบค่าเฉลี่ย (กันเตือนมั่วช่วงข้อมูลน้อย)
  var avgS=_avg_(hist.map(function(x){return daySum_(x).sales;}));
  var avgCan=_avg_(hist.map(function(x){return (x.voids||{}).cancelled||0;}));
  var avgLeak=_avg_(hist.map(_leakOf_));
  if(smart && avgS>0){ var dS=(s.sales-avgS)/avgS*100;
    lines.push('📊 เทียบค่าเฉลี่ยร้าน: ยอดขาย '+(dS>=0?'+':'')+dS.toFixed(0)+'%'+(dS>=25?' 🔥':(dS<=-25?' ⚠️':''))); }
  var al=[];
  if((v.deleted||0)>0) al.push('🗑️ ลบบิลหลังขาย ฿'+fmtT_(v.deleted)+' — ควรตรวจ (เสี่ยงทุจริต)');
  if(smart && avgCan>500 && (v.cancelled||0)>avgCan*2 && (v.cancelled||0)>1500)
    al.push('❌ ยกเลิกสูงผิดปกติ ฿'+fmtT_(v.cancelled)+' (~'+((v.cancelled||0)/avgCan).toFixed(1)+'× ค่าเฉลี่ย)');
  else if((v.cancelled||0)>3000) al.push('❌ ยกเลิกสูง ฿'+fmtT_(v.cancelled));
  if(smart && avgS>0 && s.sales<avgS*0.65) al.push('📉 ยอดขายต่ำกว่าปกติมาก (ต่ำกว่าเฉลี่ย '+(100-s.sales/avgS*100).toFixed(0)+'%)');
  if(prof<0) al.push('🔻 วันนี้ขาดทุน ฿'+fmtT_(Math.abs(prof)));
  if(smart && avgLeak>500 && leak>avgLeak*2 && leak>2000)
    al.push('💧 เงินรั่วรวม (ยกเลิก+ลบบิล+ส่วนลด) สูงผิดปกติ ฿'+fmtT_(leak)+' ('+(s.sales>0?(leak/s.sales*100).toFixed(1):0)+'% ของยอดขาย)');
  if(al.length){ lines.push('─────────────','⚠️ ข้อควรระวัง:'); al.forEach(function(a){lines.push('  '+a);}); }
  else { lines.push('─────────────','✅ ทุกอย่างอยู่ในเกณฑ์ปกติ'); }
  return lines.join('\n');
}

// สรุป 7 วันล่าสุด
function buildWeeklyMsg_(){
  var daily=loadDaily_(); if(!daily.length) return null;
  var keep={}; daily.map(function(d){return d.date;}).sort().reverse().slice(0,7).forEach(function(x){keep[x]=true;});
  var wk=daily.filter(function(d){return keep[d.date];});
  var sales=0,exp=0,del=0,best=null;
  wk.forEach(function(d){ var s=daySum_(d); sales+=s.sales; exp+=s.exp; del+=(d.voids||{}).deleted||0;
    if(!best||s.sales>best.s) best={date:d.date,s:s.sales}; });
  var prof=sales-exp;
  var lines=['📅 สรุปรายสัปดาห์ ('+wk.length+' วันล่าสุด)','─────────────',
    '💰 ยอดขายรวม: ฿'+fmtT_(sales),'💸 ค่าใช้จ่ายรวม: ฿'+fmtT_(exp),
    (prof>=0?'✅ กำไร: +฿':'🔻 ขาดทุน: -฿')+fmtT_(Math.abs(prof)),
    '📈 เฉลี่ย/วัน: ฿'+fmtT_(sales/(wk.length||1))];
  if(best) lines.push('🏆 วันขายดีสุด: '+best.date+' (฿'+fmtT_(best.s)+')');
  if(del>0) lines.push('🗑️ ลบบิลรวมสัปดาห์: ฿'+fmtT_(del)+' — ตรวจสอบ');
  return lines.join('\n');
}

// ส่งอีเมลสรุปให้เจ้าของ (ฟรี ไม่กินโควตา LINE) — ตั้งอีเมลด้วย setOwnerEmails()
function emailOwners_(subject, body){
  var to=PropertiesService.getScriptProperties().getProperty('OWNER_EMAILS');
  if(!to){ try{ to=Session.getEffectiveUser().getEmail(); }catch(e){} }   // ไม่ตั้ง = เมลเจ้าของ Apps Script
  if(!to){ Logger.log('ยังไม่ตั้ง OWNER_EMAILS'); return; }
  try{ MailApp.sendEmail(to, subject, body); Logger.log('✉️ อีเมลสรุปส่งแล้ว → '+to); }
  catch(e){ Logger.log('อีเมลพลาด: '+e); }
}
// ตั้งอีเมลผู้รับ (คั่นด้วยคอมมา) — แก้แล้วรันครั้งเดียว
function setOwnerEmails(){
  PropertiesService.getScriptProperties().setProperty('OWNER_EMAILS','ใส่อีเมล1@gmail.com,อีเมล2@gmail.com'); // <<< แก้ตรงนี้
  Logger.log('✅ ตั้งอีเมลเจ้าของแล้ว: '+PropertiesService.getScriptProperties().getProperty('OWNER_EMAILS'));
}

// [แก้ 17/08/26] ถ้า LINE ส่งไม่ออก → ฟ้องใน "หัวเรื่องอีเมล" + ต่อท้ายเนื้อหา
// (ของเดิม try{}catch(e){} กลืนเงียบ → ได้แต่อีเมล ไม่รู้ว่าไลน์พัง)
function sendDailyAlert(){ var m=buildDailyMsg_(); if(!m) return;
  var err=''; try{ err=pushOwner_(m)||''; }catch(e){ err=String(e); }
  var subj='📊 สรุปยอดขายเมื่อวาน — ไส้ย่างซอย๔'+(err?' ⚠️ (LINE ส่งไม่ออก)':'');
  emailOwners_(subj, m+(err?('\n\n─────────────\n⚠️ ส่งเข้า LINE ไม่สำเร็จ: '+err+
    '\nวิธีเช็ก: เปิด Apps Script → รัน showSlipUrl() ดู host → ถ้าเป็นบอทตัวเก่าให้รัน fixSlipUrl()'):''));
}
function sendWeeklyAlert(){ var m=buildWeeklyMsg_(); if(!m) return;
  var err=''; try{ err=pushOwner_(m)||''; }catch(e){ err=String(e); }
  var subj='📅 สรุปรายสัปดาห์ — ไส้ย่างซอย๔'+(err?' ⚠️ (LINE ส่งไม่ออก)':'');
  emailOwners_(subj, m+(err?('\n\n─────────────\n⚠️ ส่งเข้า LINE ไม่สำเร็จ: '+err):''));
}

// [แก้ 17/08/26] ทดสอบเฉพาะ LINE (ไม่ยิงอีเมลซ้ำ) + บอกผลชัดเจน
function testPushOwner(){
  var m=buildDailyMsg_();
  if(!m){ Logger.log('❌ ไม่มีข้อมูลรายวัน — รัน backfillPos() ก่อน'); return; }
  Logger.log(m);
  Logger.log('─────────────');
  var err=pushOwner_(m);
  Logger.log(err ? ('🔴 LINE ไม่เข้า: '+err) : '🎉 LINE เข้าเรียบร้อย — จบ');
}

// ===== ตั้งเวลาอัตโนมัติ ตี 1:00 ทุกวัน (หลัง POS ส่งเมล 00:30 เผื่อ 30 นาที) — รันครั้งเดียว =====
function setupDailyTrigger(){
  ScriptApp.getProjectTriggers().forEach(function(t){var h=t.getHandlerFunction();
    if(h==='importPosReports'||h==='sendWeeklyAlert') ScriptApp.deleteTrigger(t);});
  // ดึง POS ตี 1 + ลองซ้ำ ตี 2/3/4/5 — เผื่อรอบตี 1 พลาดด้วยเหตุใดก็ตาม (ไม่ยิง / สะดุดชั่วคราว / อีเมลเข้าช้า)
  // importPosReports กันทำซ้ำเอง (เช็ค LAST_IMPORTED_EMAIL_MS) → รอบที่ import แล้วจะข้ามก่อน OCR ไม่เปลืองโควต้า
  [1,2,3,4,5].forEach(function(hr){
    ScriptApp.newTrigger('importPosReports').timeBased().atHour(hr).nearMinute(0).everyDays(1).create();
  });
  ScriptApp.newTrigger('sendWeeklyAlert').timeBased().onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(9).nearMinute(0).create(); // สรุปรายสัปดาห์ จันทร์ 9 โมง
  Logger.log('✅ ตั้งเวลา: ดึง POS ตี 1–5 (ลองซ้ำจนสำเร็จ) · สรุปรายสัปดาห์ จันทร์ 9 โมง');
}
function testFetch() {
  var r = UrlFetchApp.fetch('https://www.google.com', {muteHttpExceptions:true});
  Logger.log('external_request OK: ' + r.getResponseCode());
}
