#!/usr/bin/env python3
import sys
import os
import time
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

# استخدام Session لإعادة استخدام اتصالات الشبكة والسرعة القصوى
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

cached_server = None

def get_best_gofile_server():
    global cached_server
    if cached_server:
        return cached_server
    
    try:
        res = session.get("https://api.gofile.io/servers", timeout=15)
        if res.status_code == 200 and res.json().get("status") == "ok":
            servers = res.json()["data"].get("servers", [])
            if servers:
                cached_server = servers[0]["name"]
                return cached_server
    except Exception as e:
        print(f"⚠️ فشل جلب خادم Gofile: {e}", flush=True)
    return None

def upload_worker(part_num, filepath, max_retries=3):
    """دالة رفع تعمل في الخلفية بشكل منفصل"""
    server_name = get_best_gofile_server()
    if not server_name:
        server_name = "store1" # خادم افتراضي في حال الفشل

    upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
    
    print(f"🚀 [خلفية] بدء رفع الجزء {part_num} على {server_name}...", flush=True)
    
    for attempt in range(1, max_retries + 1):
        try:
            with open(filepath, "rb") as f:
                files = {"file": f}
                u_res = session.post(upload_url, files=files, timeout=900)
            
            if u_res.status_code == 200:
                data = u_res.json()
                if data.get("status") == "ok":
                    fdata = data.get("data", {})
                    link = fdata.get("downloadPage") or (f"https://gofile.io/d/{fdata.get('code')}" if fdata.get('code') else None)
                    if link:
                        print(f"✅ [اكتمل] الجزء {part_num}: {link}", flush=True)
                        # حذف الملف المؤقت فور انتهاء الرفع لتوفير المساحة
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return part_num, link
            
            print(f"⚠️ محاولة {attempt} للجزء {part_num} لم تكتمل، إعادة المحاولة...", flush=True)
        except Exception as e:
            print(f"⚠️ خطأ أثناء رفع الجزء {part_num} (محاولة {attempt}): {e}", flush=True)
        
        time.sleep(2)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(f"⚡ بدء العملية بالسرعة القصوى (تنزيل ورفع بالتوازي) - بحجم {chunk_size_mb}MB...", flush=True)

    futures = []
    # السماح برفع جُزأين في الخلفية في نفس الوقت لزيادة تدفق البيانات
    executor = ThreadPoolExecutor(max_workers=3)

    part_num = 1
    current_size = 0

    try:
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            part_filename = f"part_{part_num:03d}.bin"
            part_path = os.path.join(TEMP_DIR, part_filename)
            part_file = open(part_path, "wb")

            try:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024): # زيادة حجم البافر لـ 4MB لسرعة أكبر
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    current_size += len(chunk)

                    if current_size >= CHUNK_SIZE:
                        part_file.close()
                        
                        # إرسال عملية الرفع إلى الخلفية والاستمرار فوراً في تنزيل الجزء التالي!
                        future = executor.submit(upload_worker, part_num, part_path)
                        futures.append(future)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    future = executor.submit(upload_worker, part_num, part_path)
                    futures.append(future)
                else:
                    if os.path.exists(part_path):
                        os.remove(part_path)

            finally:
                if not part_file.closed:
                    part_file.close()

    except Exception as e:
        print(f"❌ خطأ أثناء تنزيل الملف الأصلي: {e}", flush=True)
        executor.shutdown(wait=False)
        sys.exit(1)

    # انتظار انتهاء جميع المهام في الخلفية وتجميع الروابط
    results = []
    for future in as_completed(futures):
        res = future.result()
        if res and res[1]:
            results.append(res)
        else:
            print("❌ فشلت إحدى عمليات الرفع في الخلفية!", flush=True)

    executor.shutdown(wait=True)

    # ترتيب الروابط حسب رقم الجزء
    results.sort(key=lambda x: x[0])

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in results:
            f.write(f"الجزء {num}: {link}\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print("🏁 انتهت جميع العمليات بنجاح وبأقصى سرعة!", flush=True)

if __name__ == "__main__":
    main()
