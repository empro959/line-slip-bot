web: gunicorn app:app --bind 0.0.0.0:$PORT
# ตั้งค่า worker/threads/timeout/max_requests อยู่ใน gunicorn.conf.py (threads=3 กัน RAM ชน 512MB · max_requests=300 · timeout=120)
# ไม่ใส่ flag ที่นี่ เพื่อไม่ให้ทับค่าใน gunicorn.conf.py (เดิม --threads 8 ทับค่า 3 ที่ตั้งใจลด → เสี่ยง OOM)
