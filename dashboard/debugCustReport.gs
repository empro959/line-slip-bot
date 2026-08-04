// ★ ดึงข้อความ OCR ของ 'รายงานแยกลูกค้า' 1 ไฟล์ล่าสุด → เอาไปเขียน parser 'ใครค้างชำระ' ให้ถูกฟอร์แมต
//   วางในโปรเจกต์เดียวกับ Code_full.gs แล้วรัน debugCustReport → ก๊อป log ส่งให้ผม
function debugCustReport(){
  var threads=GmailApp.search('has:attachment filename:pdf newer_than:20d',0,60);
  var cust={};
  threads.forEach(function(t){t.getMessages().forEach(function(msg){msg.getAttachments().forEach(function(at){
    var n=at.getName(), mm=n.match(/_(\d+)\.pdf$/i); if(!mm) return;
    if(/SalesSummaryReportByCustomer|แยกตามลูกค้า/i.test(n) && !cust[mm[1]]) cust[mm[1]]=at;
  });});});
  var sufs=Object.keys(cust).sort().reverse();
  if(!sufs.length){ Logger.log('❌ ไม่เจอไฟล์รายงานลูกค้าใน 20 วันล่าสุด'); return; }
  Logger.log('เจอ suffix: '+sufs.join(', ')+'  → OCR ตัวล่าสุด '+sufs[0]);
  try{
    var txt=pdfToText_(cust[sufs[0]].copyBlob());
    Logger.log('----- OCR TEXT (รายงานลูกค้า) -----\n'+txt.substring(0,2500));
  }catch(e){ Logger.log('OCR error (โควตายังไม่ฟื้น?): '+(e&&e.message?e.message:e)); }
}
