# gunicorn โหลดไฟล์นี้อัตโนมัติเมื่อรัน `gunicorn app:app`
# จำเป็นเพราะ Render รัน `gunicorn app:app` เฉยๆ (ไม่ได้ใช้ flags ใน Procfile)
# → ถ้าไม่มีไฟล์นี้จะได้ค่า default: sync worker, 1 thread, timeout 30s (ทำงานทีละ request + timeout สั้น)
import os

workers          = int(os.environ.get("WEB_CONCURRENCY", "1"))  # 1 process (state/thread แชร์กันได้)
threads          = int(os.environ.get("GUNICORN_THREADS", "8"))  # รับหลาย webhook พร้อมกัน (สลิป/จองรัวๆ)
timeout          = 120          # ต้อง >> เวลารอ storage ใน _db() (15s) กัน worker ถูกฆ่า
graceful_timeout = 30
