// ★ ล้าง "เมนูเพี้ยน" ที่เกิดจากไฟล์ re-export ฟอร์แมตอังกฤษ (parser เมนูอ่านไม่ได้ → เอาหัวรายงานมาเป็นชื่อเมนู)
//   ลบเฉพาะเมนูเพี้ยน (ไม่แตะยอดขาย/รายจ่าย) + รายงานว่าวันไหน "หมวดขาย" ก็เพี้ยน (= ต้อง import วันนั้นใหม่)
//   วางในโปรเจกต์เดียวกับ Code_full.gs แล้วรัน cleanBadMenu ครั้งเดียว
function cleanBadMenu(){
  var bad=/Sales Report|Category|Item Code|Item Description|From .*To|Total Order|Real Sale|08:30/;
  var daily=loadDaily_(), rmMenu=0, rep=[];
  daily.forEach(function(d){
    var b=(d.menu||[]).length;
    d.menu=(d.menu||[]).filter(function(it){var n=(it.name||'');return !bad.test(n) && n.length<60;});
    rmMenu+=b-d.menu.length;
    var sbad=(d.sales||[]).filter(function(c){var n=(c.category||'');return bad.test(n)||n.length>40;});
    var tS=(d.sales||[]).reduce(function(a,c){return a+(c.amount||0);},0);
    if(b!==d.menu.length||sbad.length)
      rep.push(d.date+' — ลบเมนูเพี้ยน '+(b-d.menu.length)+(sbad.length?(' | ⚠️ หมวดขายเพี้ยน '+sbad.length+' รายการ (ยอดวันนี้='+tS.toFixed(0)+' → ควร import ใหม่)'):''));
  });
  saveDaily_(daily);
  writeMonths_(rebuildMonths_(daily, fetchSlipMap_()));
  Logger.log('✅ ล้างเมนูเพี้ยนรวม '+rmMenu+' รายการ\n'+(rep.join('\n')||'— ไม่พบ'));
}
